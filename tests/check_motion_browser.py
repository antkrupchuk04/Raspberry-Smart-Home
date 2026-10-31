"""Optional browser checks: playwright + a local Chrome; no MQTT/DB writes."""
import copy
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-09-28T16:00:00Z"
PAYLOAD = {
    "configured": True, "device": {"id": "test", "name": "Tuya TS0202", "room": "Living room"},
    "sensor": {"occupancy": True, "battery": 100, "battery_low": False, "linkquality": 149,
               "last_motion_at": STAMP, "last_seen": STAMP, "state_reported_at": STAMP},
    "events": [{"id": 1, "room": "Living room", "occupancy": True, "recorded_at": STAMP}],
    "today_count": 1, "day_timezone": "Europe/Kyiv", "generated_at": STAMP,
}


CSI_PAYLOAD = {
    "device_id": "esp32-room1", "status": "live", "state": "ready", "motion": False,
    "raw_motion": False, "filter": {"score_multiplier":1.25,"confirm_reports":3,"clear_reports":3,"pending_reports":0,"threshold_on":0.0465875,"threshold_off":0.0366875},
    "score": 0.0268, "threshold_on": 0.03727, "threshold_off": 0.02935,
    "calibration": 30, "samples": 52, "rssi": -61, "dropped": 0,
    "last_seen": STAMP, "last_motion_at": None, "fresh_for_seconds": 5,
    "history_available": True,
    "events": [
        {"id":"csi-clear-1","motion":False,"recorded_at":STAMP,"room":"Room 1"},
        {"id":"csi-detect-1","motion":True,"recorded_at":STAMP,"room":"Room 1"},
    ],
}


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.getenv("CHROME_PATH", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        fixture = {"data": copy.deepcopy(PAYLOAD), "status": 200, "calls": 0, "csi": copy.deepcopy(CSI_PAYLOAD), "csi_status": 200}

        def handle(route):
            path = route.request.url.split("motion.test", 1)[1].split("?", 1)[0]
            if path == "/api/motion/csi":
                route.fulfill(status=fixture["csi_status"], content_type="application/json", body=json.dumps(fixture["csi"]))
            elif path == "/api/motion":
                fixture["calls"] += 1
                route.fulfill(status=fixture["status"], content_type="application/json", body=json.dumps(fixture["data"]))
            elif path == "/api/sensors/history":
                route.fulfill(content_type="application/json", body=json.dumps([
                    {"recorded_at": "2026-09-28T15:55:00Z", "temperature": 22, "humidity": 40, "pressure": 1010},
                    {"recorded_at": STAMP, "temperature": 23, "humidity": 45, "pressure": 1011}]))
            elif path.startswith("/api/"):
                route.fulfill(status=503, content_type="application/json", body='{"detail":"Fixture unavailable"}')
            else:
                file = ROOT / ("static/index.html" if path == "/" else path.lstrip("/"))
                route.fulfill(path=str(file))

        page.route("http://motion.test/**", handle)
        page.add_init_script("window.testIntervals=[]; const originalInterval=window.setInterval; window.setInterval=(f,ms)=>{window.testIntervals.push(ms); return originalInterval(f,ms)};")
        page.goto("http://motion.test/")
        page.wait_for_function("document.querySelector('#motion-state').textContent === 'MOTION DETECTED'")
        page.wait_for_function("document.querySelector('#csi-state').textContent === 'CLEAR'")
        assert page.locator("#csi-score").inner_text() == "0.0268"
        assert page.locator("#csi-dropped").inner_text() == "0"
        assert page.locator('#csi-events li').count() == 2
        assert page.locator('#csi-events li').first.locator('time').get_attribute('datetime') == STAMP
        assert 'Clear' in page.locator('#csi-events li').first.inner_text()
        assert 'Motion detected' in page.locator('#csi-events li').nth(1).inner_text()
        page.evaluate("window.originalCsiRow=document.querySelector('#csi-events li')")
        page.evaluate('loadCsi()')
        assert page.evaluate("originalCsiRow === document.querySelector('#csi-events li')")
        assert page.locator("#motion-count").inner_text() == "1"
        assert page.locator("#motion-battery").inner_text() == "100 %"
        assert "Living room" in page.locator("#motion .card-subtitle").inner_text()
        assert page.locator("#motion-events li").count() == 1
        assert sorted(page.evaluate("window.testIntervals")) == [1000, 5000, 5000, 5000, 10000, 10000, 30000, 600000]
        assert page.locator('#topology').count() == 0
        assert page.locator('.top-nav a[href="/topology"]').inner_text() == 'Smart Home Map'
        page.evaluate("window.originalMotionRow=document.querySelector('#motion-events li')")
        page.evaluate("loadMotion()")
        assert page.evaluate("window.originalMotionRow===document.querySelector('#motion-events li')")

        for width in (1440, 768, 390, 320):
            page.set_viewport_size({"width": width, "height": 1000})
            for theme in ("dark", "light"):
                page.evaluate("theme=>setTheme(theme)", theme)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, theme, "overflow")
                expected_color = "rgb(210, 153, 34)" if theme == "dark" else "rgb(154, 103, 0)"
                assert page.locator("#motion-state").evaluate("e=>getComputedStyle(e).color") == expected_color
                screenshot_dir = os.getenv("MOTION_SCREENSHOT_DIR")
                if screenshot_dir and width in (1440, 390):
                    page.locator('.site-header').evaluate("e => e.style.visibility = 'hidden'")
                    page.locator("#motion").screenshot(path=str(Path(screenshot_dir) / f"motion-{width}-{theme}.png"), animations="disabled")
                    page.locator('.site-header').evaluate("e => e.style.visibility = ''")

        fixture["data"]["sensor"].update(occupancy=False, battery=0, linkquality=0)
        page.evaluate("loadMotion()")
        assert page.locator("#motion-state").inner_text() == "CLEAR"
        assert page.locator("#motion-battery").inner_text() == "0 %"
        assert page.locator("#motion-signal").inner_text() == "0 / 255"
        fixture["status"] = 503
        page.evaluate("loadMotion()")
        assert page.locator("#motion-badge").inner_text() == "UNAVAILABLE"
        assert page.locator("#motion-state").inner_text() == "UNKNOWN"
        fixture["status"] = 200
        fixture["data"]["sensor"] = None
        fixture["data"]["events"] = []
        fixture["data"]["today_count"] = 0
        page.evaluate("loadMotion()")
        assert page.locator("#motion-state").inner_text() == "WAITING FOR REPORT"
        assert page.locator("#motion-empty").is_visible()
        fixture["data"]["configured"] = False
        page.evaluate("loadMotion()")
        assert page.locator("#motion-state").inner_text() == "NOT CONFIGURED"
        fixture["data"] = copy.deepcopy(PAYLOAD)
        fixture["data"]["events"][0]["room"] = '<img src=x onerror="alert(1)">'
        page.evaluate("loadMotion()")
        assert page.locator("#motion-events img").count() == 0
        assert "<img" in page.locator("#motion-events").inner_text()
        # Raw activity is diagnostic; only confirmed motion changes the main state.
        fixture['csi'].update(raw_motion=True, motion=False)
        fixture['csi']['filter']['pending_reports'] = 2
        page.evaluate('loadCsi()')
        assert page.locator('#csi-state').inner_text() == 'CLEAR'
        assert page.locator('#csi-raw-state').inner_text() == 'MOTION'
        assert 'Confirming 2/3' in page.locator('#csi-message').inner_text()
        assert page.locator('#csi-thresholds').inner_text() == '0.0466 / 0.0367'
        fixture['csi']['filter']['pending_reports'] = 0
        # CSI is independent of PIR, and unknown/stale data must never say CLEAR.
        fixture["csi"]["motion"] = True
        page.evaluate("loadCsi()")
        assert page.locator("#csi-state").inner_text() == "MOTION DETECTED"
        fixture["csi"].update(state="calibrating", motion=None, calibration=12, score=0, threshold_on=None, threshold_off=None)
        page.evaluate("loadCsi()")
        assert page.locator("#csi-state").inner_text() == "CALIBRATING"
        assert page.locator("#csi-score").inner_text() == "0.0000"
        assert page.locator("#csi-calibration").inner_text() == "12 windows"
        for state, label in (("settling", "SETTLING"), ("no_data", "NO DATA")):
            fixture["csi"].update(state=state, motion=None, score=None)
            page.evaluate("loadCsi()")
            assert page.locator("#csi-state").inner_text() == label
        fixture["csi"] = copy.deepcopy(CSI_PAYLOAD)
        page.evaluate("loadCsi()")
        page.evaluate("csiReceivedAt = performance.now() - 6000; updateCsiState()")
        assert page.locator("#csi-state").inner_text() == "STALE"
        assert page.locator("#csi-score").inner_text() == "—"
        for status, label in (("mqtt_offline", "MQTT OFFLINE"), ("waiting", "WAITING FOR REPORT"), ("stale", "STALE")):
            fixture["csi"].update(status=status, motion=None, fresh_for_seconds=0)
            page.evaluate("loadCsi()")
            assert page.locator("#csi-state").inner_text() == label
        fixture['csi']['history_available'] = False
        page.evaluate('loadCsi()')
        assert page.locator('#csi-state').inner_text() != 'API UNAVAILABLE'
        assert 'temporarily unavailable' in page.locator('#csi-events-empty').inner_text()
        assert page.locator('#csi-events li').count() == 2
        fixture['csi']['history_available'] = True
        fixture['csi']['events'] = []
        page.evaluate('loadCsi()')
        assert 'No confirmed' in page.locator('#csi-events-empty').inner_text()
        fixture['csi']['events'] = [{"id":"safe-text","motion":True,"recorded_at":STAMP,"room":'<img src=x onerror="alert(1)">'}]
        page.evaluate('loadCsi()')
        assert page.locator('#csi-events img').count() == 0
        assert '<img' in page.locator('#csi-events').inner_text()
        fixture["csi_status"] = 503
        page.evaluate("loadCsi()")
        assert page.locator("#csi-state").inner_text() == "API UNAVAILABLE"
        assert page.locator("#motion-state").inner_text() == "MOTION DETECTED"
        fixture["csi_status"] = 200
        fixture["csi"] = copy.deepcopy(CSI_PAYLOAD)
        page.evaluate("loadCsi()")
        assert page.locator("#csi-state").inner_text() == "CLEAR"
        page.locator('[data-metric="humidity"]').click()
        assert page.locator("#chart-title").inner_text() == "Humidity History"
        page.locator('[data-hours="6"]').click()
        page.wait_for_function("document.querySelector('#history-status').textContent.includes('Last 6H')")
        page.locator("#theme-toggle").click()
        assert not errors, errors
        print("Browser checks passed: 4 widths, both themes, motion/clear, zero values, API errors/recovery, empty/disabled states, safe text rendering, unchanged PIR timeline/polling, existing chart controls, five-second CSI updates with raw/filtered state separation, live CSI states, calibration, local freshness expiry and independent failure/recovery, separate timestamped CSI history, stable event nodes, history errors and safe rendering.")
        browser.close()


if __name__ == "__main__":
    main()
