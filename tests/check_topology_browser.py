"""Optional Playwright/Chrome checks; all requests use local fixtures, never MQTT."""
import copy
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / 'tests/fixtures/topology.json').read_text())


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.getenv('CHROME_PATH', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'))
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        fixture = {'data': copy.deepcopy(BASE), 'status': 200, 'calls': 0, 'api_paths': []}

        def handle(route):
            path = route.request.url.split('topology.test', 1)[1].split('?', 1)[0]
            if path.startswith('/api/'):
                fixture['api_paths'].append(path)
            if path == '/api/topology':
                fixture['calls'] += 1
                route.fulfill(status=fixture['status'], content_type='application/json', body=json.dumps(fixture['data']))
            elif path.startswith('/api/'):
                route.fulfill(status=503, content_type='application/json', body='{"detail":"Fixture unavailable"}')
            else:
                pages = {'/': 'static/index.html', '/topology': 'static/topology.html'}
                route.fulfill(path=str(ROOT / pages.get(path, path.lstrip('/'))))

        page.route('http://topology.test/**', handle)
        page.add_init_script('window.testIntervals=[]; const originalInterval=setInterval; window.setInterval=(f,ms)=>{testIntervals.push(ms); return originalInterval(f,ms)};')
        page.goto('http://topology.test/', wait_until='networkidle')
        assert page.locator('#topology').count() == 0
        assert fixture['calls'] == 0
        fixture['api_paths'].clear()
        page.locator('.top-nav a[href="/topology"]').click()
        page.wait_for_url('http://topology.test/topology')
        page.wait_for_function("document.querySelector('#topology-summary').textContent === 'System healthy'")
        node = lambda identifier: page.locator(f'.map-node[data-node-id="{identifier}"]')
        panel = page.locator('#map-details')
        assert page.locator('.map-node').count() == 3
        assert page.locator('.map-group').count() == 2
        assert page.locator('.topology-path-group path').count() == 2
        assert sorted(page.evaluate('window.testIntervals')) == [1000, 10000]
        assert set(fixture['api_paths']) == {'/api/topology'}
        assert page.locator('.top-nav [aria-current="page"]').inner_text() == 'Smart Home Map'
        assert page.locator('#system-uptime').inner_text() == '1d 2h'
        assert page.locator('#overall-status-text').inner_text() == 'ONLINE'
        assert node('esp32-room1').locator('[data-field="status"]').inner_text() == 'Online'
        assert node('motion-room1').locator('[data-field="status"]').inner_text() == 'Availability unknown'
        assert 'Motion detected' in node('motion-room1').inner_text()
        assert '20.8' not in node('esp32-room1').inner_text()
        assert not panel.is_visible()
        assert page.locator('.topology-path-group path[data-link-state="online"]').count() == 1
        assert page.locator('.topology-path-group path[data-link-state="unknown"]').count() == 1

        def geometry():
            page.evaluate('topologyBoard.layout()')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal overflow'
            # Sample SVG branches/trunks: none crosses another device card.
            assert page.evaluate('''() => {
                const box = document.querySelector('.topology-board').getBoundingClientRect();
                const cards = [...document.querySelectorAll('.map-node')].map(e => e.getBoundingClientRect());
                return [...document.querySelectorAll('.topology-paths path')].every(path => {
                    if (!path.getAttribute('d')) return false;
                    for (let n = 0; n <= 80; n++) {
                        const point = path.getPointAtLength(path.getTotalLength() * n / 80);
                        const x = point.x + box.left, y = point.y + box.top;
                        if (cards.some(r => x > r.left + 1 && x < r.right - 1 && y > r.top + 1 && y < r.bottom - 1)) return false;
                    }
                    return true;
                });
            }'''), 'connection crosses a device'

        for width in (1440, 1024, 1000, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            for theme in ('dark', 'light'):
                page.evaluate('theme => setTheme(theme)', theme)
                geometry()
                hub = node('raspberry').bounding_box()
                esp = node('esp32-room1').bounding_box()
                motion = node('motion-room1').bounding_box()
                assert hub['y'] + hub['height'] < min(esp['y'], motion['y'])
                if width >= 768:
                    assert esp['x'] + esp['width'] < motion['x']
                else:
                    assert esp['y'] + esp['height'] < motion['y']
                node('motion-room1').click()
                assert panel.is_visible()
                assert panel.locator('[data-key="occupancy"] dd').inner_text() == 'Motion detected'
                expected = 'rgb(210, 153, 34)' if theme == 'dark' else 'rgb(154, 103, 0)'
                assert panel.locator('[data-key="occupancy"] dd').evaluate('e => getComputedStyle(e).color') == expected
                detail_box = panel.bounding_box()
                board = page.locator('.topology-board').bounding_box()
                if width >= 900:
                    assert detail_box['x'] >= board['x'] + board['width']
                else:
                    assert detail_box['y'] >= board['y'] + board['height']
                geometry()
                output = os.getenv('TOPOLOGY_SCREENSHOT_DIR')
                if output and width in (1440, 390):
                    Path(output).mkdir(parents=True, exist_ok=True)
                    page.locator('.site-header').evaluate("e => e.style.visibility = 'hidden'")
                    page.locator('#topology').screenshot(path=str(Path(output) / f'map-{width}-{theme}-details.png'), animations='disabled')
                    page.locator('.site-header').evaluate("e => e.style.visibility = ''")
                page.locator('#map-detail-close').click()
                assert not panel.is_visible()
                assert node('motion-room1').evaluate('e => e === document.activeElement')

        # Keyboard selection, focus restoration, one panel, preserved node identity.
        page.set_viewport_size({'width': 1440, 'height': 1000})
        node('esp32-room1').focus()
        page.keyboard.press('Enter')
        assert page.locator('#map-detail-title').inner_text() == 'ESP32 Room 1'
        assert '20.8 °C' in panel.inner_text()
        assert page.locator('#map-detail-title').evaluate('e => e === document.activeElement')
        page.evaluate("window.originalMapNode = document.querySelector('[data-node-id=esp32-room1]')")
        fixture['data']['nodes'][1]['metrics'][0]['value'] = 21.2
        page.evaluate('topologyBoard.load()')
        assert '21.2 °C' in panel.inner_text()
        assert page.evaluate("originalMapNode === document.querySelector('[data-node-id=esp32-room1]')")
        assert node('esp32-room1').get_attribute('aria-expanded') == 'true'
        page.keyboard.press('Escape')
        assert not panel.is_visible()
        assert node('esp32-room1').evaluate('e => e === document.activeElement')
        node('raspberry').focus()
        page.keyboard.press('Space')
        assert 'FastAPI' in panel.inner_text() and 'Not monitored' in panel.inner_text()
        node('motion-room1').click()
        assert page.locator('.map-node[aria-expanded="true"]').count() == 1
        assert page.locator('#map-detail-title').inner_text() == 'Motion Sensor'

        motion = fixture['data']['nodes'][2]
        motion['activity'] = 'clear'
        motion['metrics'][0].update(value='Clear / no motion', state='online')
        motion['metrics'][1]['value'] = 0
        motion['metrics'][2]['value'] = 0
        page.evaluate('topologyBoard.load()')
        assert 'No motion' in node('motion-room1').inner_text()
        assert 'Clear / no motion' in panel.inner_text()
        assert '0 %' in panel.inner_text() and '0 / 255' in panel.inner_text()
        motion['activity'] = 'detected'
        motion['activity_at'] = '2026-09-30T22:01:01Z'
        motion['metrics'][0].update(value='Motion detected', state='warning')
        page.evaluate('topologyBoard.load()')
        page.wait_for_function("document.querySelector('[data-node-id=motion-room1]').classList.contains('map-pulse')")
        page.wait_for_function("!document.querySelector('[data-node-id=motion-room1]').classList.contains('map-pulse')")
        page.evaluate('topologyBoard.load()')
        assert not node('motion-room1').evaluate("e => e.classList.contains('map-pulse')")
        page.emulate_media(reduced_motion='reduce')
        motion['activity_at'] = '2026-09-30T22:01:02Z'
        page.evaluate('topologyBoard.load()')
        assert not node('motion-room1').evaluate("e => e.classList.contains('map-pulse')")
        page.emulate_media(reduced_motion='no-preference')

        # Backend lease controls expiry; health remains separate from PIR state.
        page.evaluate('topologyBoard.receivedAt = performance.now() - 16000; topologyBoard.tick()')
        assert node('esp32-room1').locator('[data-field="status"]').inner_text() == 'Offline'
        assert page.locator('#topology-summary').inner_text() == 'Awaiting update'
        assert page.locator('.topology-path-group path[data-link-state="online"]').count() == 0
        page.evaluate('topologyBoard.load()')
        fixture['data']['system']['status'] = 'degraded'
        page.evaluate('topologyBoard.load()')
        assert page.locator('#topology-summary').inner_text() == 'Attention required'
        assert page.locator('#topology-summary').get_attribute('data-state') == 'error'
        fixture['status'] = 503
        page.evaluate('topologyBoard.load()')
        assert page.locator('#topology-summary').inner_text() == 'Map unavailable'
        assert page.locator('#connection-badge').inner_text() == 'API: unavailable'
        assert page.locator('#system-uptime').inner_text() == '—'
        assert page.locator('.map-node[data-health="stale"]').count() == 3
        assert 'outdated' in panel.inner_text()
        assert page.locator('.topology-path-group path[data-link-state="online"]').count() == 0
        fixture['status'] = 200
        fixture['data']['status'] = 'partial'
        fixture['data']['nodes'][1].update(status='unknown', data_status='unavailable')
        page.evaluate('topologyBoard.load()')
        assert page.locator('#topology-summary').inner_text() == 'Partial data'
        assert node('esp32-room1').locator('[data-field="status"]').inner_text() == 'Data unavailable'
        assert node('raspberry').locator('[data-field="status"]').inner_text() == 'Online'

        # Extra/unknown types need data only; no individual coordinates or HTML.
        fixture['data'] = copy.deepcopy(BASE)
        extra = copy.deepcopy(BASE['nodes'][1])
        extra.update(id='future-sensor', type='future_type', name='New Sensor', device_id='future-sensor')
        fixture['data']['nodes'].append(extra)
        edge = copy.deepcopy(BASE['links'][0])
        edge.update(id='future-link', source='future-sensor')
        fixture['data']['links'].append(edge)
        page.evaluate('topologyBoard.load()')
        assert page.locator('.map-node').count() == 4
        assert node('future-sensor').locator('use').get_attribute('href') == '#map-icon-sensor'
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            geometry()
        fixture['data']['nodes'][1]['name'] = '<img src=x onerror="alert(1)">'
        page.evaluate('topologyBoard.load()')
        node('esp32-room1').click()
        assert page.locator('#topology img').count() == 0
        assert '<img' in page.locator('#map-detail-title').inner_text()
        fixture['data'] = copy.deepcopy(BASE)
        fixture['data']['links'][0]['source'] = 'missing-node'
        page.evaluate('topologyBoard.load()')
        assert page.locator('#topology-summary').inner_text() == 'Map unavailable'
        fixture['data'] = copy.deepcopy(BASE)
        fixture['data']['nodes'] = fixture['data']['nodes'][:1]
        fixture['data']['links'] = []
        page.evaluate('topologyBoard.load()')
        assert not panel.is_visible()
        assert page.locator('.map-group').count() == 0
        assert page.locator('.map-empty').is_visible()
        fixture['data']['nodes'] = []
        fixture['data']['system']['status'] = 'unknown'
        page.evaluate('topologyBoard.load()')
        assert 'No device information' in page.locator('.map-empty').inner_text()
        assert page.locator('#topology-summary').inner_text() == 'Status unknown'

        # First-load failure and recovery; theme, navigation and legacy anchors.
        fixture['status'] = 503
        page.reload()
        page.wait_for_function("document.querySelector('#topology-summary').textContent === 'Map unavailable'")
        assert page.locator('.map-node').count() == 0
        fixture['status'] = 200
        fixture['data'] = copy.deepcopy(BASE)
        page.evaluate('topologyBoard.load()')
        assert page.locator('.map-node').count() == 3
        assert page.locator('#connection-badge').inner_text() == 'API: connected'
        page.evaluate("setTheme('dark')")
        page.locator('#theme-toggle').click()
        assert page.evaluate('document.documentElement.dataset.theme') == 'light'
        calls = fixture['calls']
        page.locator('.top-nav a[href="/"]').click()
        page.wait_for_url('http://topology.test/')
        page.wait_for_load_state('networkidle')
        assert page.evaluate('document.documentElement.dataset.theme') == 'light'
        assert page.locator('#topology').count() == 0
        assert fixture['calls'] == calls
        page.goto('http://topology.test/#topology')
        page.wait_for_url('http://topology.test/topology')
        page.wait_for_function("document.querySelector('#topology-summary').textContent === 'System healthy'")
        assert page.evaluate('document.documentElement.dataset.theme') == 'light'
        assert not errors, errors
        print('Smart Home Map browser checks passed: navigation, isolated polling, six widths, two themes, SVG geometry, keyboard/focus, live details, node reuse, motion/clear, reduced motion, freshness, failures/recovery, empty/extra nodes and safe rendering.')
        browser.close()


if __name__ == '__main__':
    main()
