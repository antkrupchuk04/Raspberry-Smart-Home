"""CSI parser, lifecycle and freshness; no broker required or messages published."""
import copy
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from csi_service import CsiMonitor, CsiMotionFilter, parse_report

REPORT = {"device_id": "esp32-room1", "state": "ready", "motion": True,
          "score": .05, "threshold_on": .04, "threshold_off": .03, "calibration": 30,
          "samples": 52, "accepted": 500, "rejected": 4, "dropped": 0,
          "ping_replies": 490, "rssi": -61, "uptime_ms": 100000}


class CsiTests(unittest.TestCase):
    def setUp(self):
        self.monitor = CsiMonitor()
        self.uptime = REPORT["uptime_ms"]
        self.client = Mock()
        self.monitor._on_connect(self.client, None, None, 0, None)

    def receive(self, report=None, uptime_ms=None, **extra):
        self.uptime += 1000
        body = {**(report or REPORT), "uptime_ms": uptime_ms if uptime_ms is not None else self.uptime}
        message = SimpleNamespace(topic=self.monitor.topic, retain=False, payload=json.dumps(body).encode())
        for key, value in extra.items():
            setattr(message, key, value)
        self.monitor._on_message(self.client, None, message)

    def test_ready_and_zero_values_serialize(self):
        report = {**REPORT, "score": 0, "motion": False, "samples": 0, "rssi": 0}
        self.receive(report)
        result = self.monitor.snapshot()
        self.assertEqual(result["status"], "live")
        self.assertIs(result["motion"], False)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["dropped"], 0)
        json.dumps(result, allow_nan=False)

    def test_freshness_boundary_and_motion_expiry(self):
        with patch('csi_service.time.monotonic', return_value=100):
            self.receive()
            self.assertEqual(self.monitor.snapshot()["fresh_for_seconds"], 5)
        for now, status in ((104.99, "live"), (105, "stale"), (110, "stale")):
            with patch('csi_service.time.monotonic', return_value=now):
                result = self.monitor.snapshot()
            self.assertEqual(result["status"], status)
            if status == "stale":
                self.assertIsNone(result["motion"])
                self.assertIsNone(result["score"])
                self.assertIsNotNone(result["last_seen"])

    def test_non_ready_states_never_claim_clear(self):
        for state in ('settling', 'calibrating', 'no_data'):
            self.receive({**REPORT, "state": state, "motion": None, "score": None})
            result = self.monitor.snapshot()
            self.assertEqual(result["state"], state)
            self.assertIsNone(result["motion"])
            self.assertIsNone(result["threshold_on"])

    def test_disconnect_and_reconnect_require_new_live_data(self):
        self.receive()
        self.monitor._on_disconnect(self.client, None, None, 1, None)
        self.assertEqual(self.monitor.snapshot()["status"], "mqtt_offline")
        self.assertIsNone(self.monitor.snapshot()["motion"])
        self.monitor._on_connect(self.client, None, None, 0, None)
        self.assertEqual(self.monitor.snapshot()["status"], "waiting")
        self.receive()
        self.assertEqual(self.monitor.snapshot()["status"], "live")
        self.client.subscribe.assert_called_with("home/motion/room1/csi", qos=0)

    def test_failed_connect_is_offline(self):
        self.monitor._on_connect(self.client, None, None, 5, None)
        self.assertEqual(self.monitor.snapshot()["status"], "mqtt_offline")
        self.receive()
        self.assertIsNone(self.monitor.snapshot()["last_seen"])

    def test_retained_or_other_topic_ignored(self):
        self.receive(retain=True)
        self.receive(topic='home/motion/room1/csi/calibrate')
        self.assertEqual(self.monitor.snapshot()["status"], "waiting")

    def test_invalid_fields_rejected_and_do_not_refresh(self):
        for change in ({'motion': 'false'}, {'state': []}, {'device_id': 'other'}, {'score': float('nan')},
                       {'score': float('inf')}, {'samples': True}, {'samples': -1}, {'rssi': -200},
                       {'threshold_off': .5}, {'state': 'calibrating', 'motion': False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_report(json.dumps({**REPORT, **change}).encode(), 'esp32-room1')
        self.receive(payload=b'{"broken":')
        self.assertEqual(self.monitor.snapshot()["status"], "waiting")
        for payload in (b'[]', b'null', b'x' * 2049):
            with self.assertRaises(ValueError):
                parse_report(payload, 'esp32-room1')

    def test_detection_time_records_confirmed_transitions_not_repeats(self):
        for _ in range(2):
            self.receive()
            self.assertIsNone(self.monitor.snapshot()['last_motion_at'])
            self.assertIs(self.monitor.snapshot()['motion'], False)
            self.assertIs(self.monitor.snapshot()['raw_motion'], True)
        self.receive()
        first = self.monitor.snapshot()['last_motion_at']
        self.assertIsNotNone(first)
        self.assertIs(self.monitor.snapshot()['motion'], True)
        self.receive()
        self.assertEqual(self.monitor.snapshot()['last_motion_at'], first)
        for _ in range(3):
            self.receive({**REPORT, 'motion': False})
        for _ in range(3):
            self.receive()
        self.assertNotEqual(self.monitor.snapshot()['last_motion_at'], first)

    def test_duplicate_report_does_not_refresh_age_or_confirm_motion(self):
        with patch('csi_service.time.monotonic', return_value=100):
            self.receive(uptime_ms=50000)
        for now in (101, 102, 103, 104):
            with patch('csi_service.time.monotonic', return_value=now):
                self.receive(uptime_ms=50000)
                self.assertIs(self.monitor.snapshot()['motion'], False)
        with patch('csi_service.time.monotonic', return_value=105):
            self.assertEqual(self.monitor.snapshot()['status'], 'stale')
        self.assertIsNone(self.monitor.snapshot()['last_motion_at'])

    def test_only_confirmed_transitions_emit_history_events(self):
        sink = Mock()
        self.monitor.on_event = sink
        self.receive(); self.receive()
        sink.assert_not_called()
        self.receive()
        for _ in range(5): self.receive()
        self.receive({**REPORT, 'motion': False})
        self.receive({**REPORT, 'motion': False})
        self.assertEqual(sink.call_count, 1)
        self.receive({**REPORT, 'motion': False})
        events = [call.args[0] for call in sink.call_args_list]
        self.assertEqual([event['motion'] for event in events], [True, False])
        self.assertNotEqual(events[0]['id'], events[1]['id'])
        self.assertTrue(all(event['recorded_at'].endswith('+00:00') for event in events))

    def test_resets_and_missing_data_never_emit_false_clear_events(self):
        self.monitor.on_event = sink = Mock()
        for _ in range(3): self.receive()
        self.receive({**REPORT, 'state': 'no_data', 'motion': None, 'score': None})
        for _ in range(3): self.receive({**REPORT, 'motion': False})
        self.assertEqual(sink.call_count, 1)
        for _ in range(3): self.receive()
        self.receive(uptime_ms=1)
        self.monitor._on_disconnect(self.client, None, None, 1, None)
        self.assertEqual([call.args[0]['motion'] for call in sink.call_args_list], [True, True])

    def test_history_sink_failure_does_not_break_live_monitoring(self):
        self.monitor.on_event = Mock(side_effect=RuntimeError('private-test-value'))
        with self.assertLogs('csi_service', level='WARNING') as logs:
            for _ in range(3): self.receive()
        self.assertIs(self.monitor.snapshot()['motion'], True)
        self.assertNotIn('private-test-value', str(logs.output))

    def test_client_lifecycle_is_async_and_never_publishes(self):
        with patch('csi_service.mqtt.Client') as factory:
            client = factory.return_value
            self.monitor.start()
            self.monitor.start()
            factory.assert_called_once()
            client.connect_async.assert_called_once_with('localhost', 1883, keepalive=15)
            client.loop_start.assert_called_once()
            client.publish.assert_not_called()
            self.monitor.stop()
            self.monitor.stop()
            client.disconnect.assert_called_once()
            client.loop_stop.assert_called_once()

    def test_wildcard_subscription_is_rejected(self):
        for topic in ('', 'home/#', 'home/+/csi'):
            with self.assertRaises(ValueError):
                CsiMonitor(topic=topic)


class CsiFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = CsiMotionFilter()
        self.uptime = 100000
        self.now = 0

    def report(self, score=.08, motion=True, **extra):
        self.uptime += 1000
        self.now += 1
        value = {**REPORT, 'score': score, 'motion': motion, 'uptime_ms': self.uptime, **extra}
        self.filter.update(value, self.now)
        return self.filter.motion

    def test_weak_raw_motion_never_confirms(self):
        for _ in range(20):
            self.assertFalse(self.report(score=.045))

    def test_three_consecutive_strong_reports_required(self):
        self.assertFalse(self.report())
        self.assertFalse(self.report())
        self.assertTrue(self.report())

    def test_isolated_high_scores_do_not_accumulate(self):
        for _ in range(10):
            self.assertFalse(self.report())
            self.assertFalse(self.report(score=.02))
        self.assertFalse(self.report())
        self.assertFalse(self.report())
        self.assertTrue(self.report())

    def test_three_low_scores_clear_even_when_raw_flag_stays_true(self):
        for _ in range(3): self.report()
        self.assertTrue(self.report(score=.035))
        self.assertTrue(self.report(score=.035))
        self.assertFalse(self.report(score=.035))

    def test_short_low_dip_does_not_chatter(self):
        for _ in range(3): self.report()
        self.assertTrue(self.report(score=.02))
        self.assertTrue(self.report())
        self.assertTrue(self.report(score=.02))

    def test_gap_or_reboot_restarts_confirmation(self):
        for kind in ('receipt_gap', 'source_gap', 'reboot'):
            with self.subTest(kind=kind):
                self.filter.reset()
                self.report(); self.report()
                if kind == 'receipt_gap': self.now += 3
                if kind == 'source_gap': self.uptime += 3000
                if kind == 'reboot': self.uptime = 0
                self.assertFalse(self.report())
                self.assertEqual(self.filter.pending, 1)

    def test_threshold_change_or_recalibration_restarts_confirmation(self):
        self.report(); self.report()
        self.assertFalse(self.report(threshold_on=.05))
        self.assertEqual(self.filter.pending, 1)
        self.report(state='calibrating', motion=None, score=None)
        self.assertFalse(self.report())
        self.assertEqual(self.filter.pending, 1)


class CsiApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('csi_test_api', Path(__file__).resolve().parents[1] / 'api.py')
        cls.api = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'DB_NAME':'test','DB_USER':'test','DB_PASSWORD':'test'}), \
             patch('fastapi.staticfiles.StaticFiles', side_effect=lambda **kw: StaticFiles(**kw, check_dir=False)):
            spec.loader.exec_module(cls.api)

    def test_endpoint_isolated_http_200_no_cache_and_lifecycle(self):
        monitor = CsiMonitor()
        with patch.object(self.api, 'csi_monitor', monitor), patch.object(monitor, 'start') as start, patch.object(monitor, 'stop') as stop, patch.object(self.api, 'csi_event_store') as store:
            store.read.return_value = {'events': [], 'history_available': False, 'history_last_motion_at': None, 'hours': 24, 'limit': 20}
            with TestClient(self.api.app) as client:
                response = client.get('/api/motion/csi')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertEqual(response.json()['status'], 'mqtt_offline')
                self.assertIsNone(response.json()['motion'])
                start.assert_called_once()
            stop.assert_called_once()
            store.start.assert_called_once()
            store.stop.assert_called_once()

    def test_history_limits_are_validated_without_querying_database(self):
        client = TestClient(self.api.app)
        with patch.object(self.api, 'csi_event_store') as store:
            for params in ('hours=0','hours=169','limit=0','limit=101','hours=bad'):
                self.assertEqual(client.get('/api/motion/csi?'+params).status_code,422)
            store.read.assert_not_called()
