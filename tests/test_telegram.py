import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
import urllib.error

import psycopg2

from motion_service import MotionConfig, collect_motion_message
from telegram_notifications import TelegramConfig, TelegramNotifier, format_motion_message

# Deliberately invalid test credential; all Telegram network operations are mocked.
TOKEN = "123456:test-token-not-a-real-credential"
DEVICE = MotionConfig("zigbee2mqtt/test", "test-sensor", "Tuya TS0202", "Living room")


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = TelegramConfig(True, TOKEN, "123", 60, Path(self.directory.name) / "state.json")
        self.now = datetime.now(timezone.utc)

    def test_disabled_or_missing_configuration_never_sends(self):
        for config in (TelegramConfig(), TelegramConfig(True), TelegramConfig(True, TOKEN, "@someone")):
            notifier = TelegramNotifier(config)
            with patch("telegram_notifications.urllib.request.urlopen") as request:
                notifier.start()
                self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now))
                self.assertIsNone(notifier._thread)
                request.assert_not_called()

    def test_config_validation_and_secret_repr(self):
        with patch.dict(os.environ, {"TELEGRAM_COOLDOWN_SECONDS": "NaN", "TELEGRAM_ENABLED": "true",
                                     "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "123",
                                     "TELEGRAM_SENSOR_LABEL": "motion-room1",
                                     "TELEGRAM_LOCATION_LABEL": "Living Room"}):
            config = TelegramConfig.from_env()
        self.assertEqual(config.cooldown_seconds, 60)
        self.assertTrue(config.ready)
        self.assertNotIn(TOKEN, repr(config))
        self.assertEqual(config.sensor_label, "motion-room1")
        self.assertEqual(config.location_label, "Living Room")

    def test_unmatched_clear_invalid_and_stale_events_are_ignored(self):
        notifier = TelegramNotifier(self.config)
        self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": False}, self.now))
        self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now - timedelta(minutes=5)))
        self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now + timedelta(minutes=5)))
        for occupancy in (None, 1, 0, "false"):
            self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": occupancy}, self.now))
        self.assertTrue(notifier._queue.empty())

    def test_per_device_cooldown(self):
        notifier = TelegramNotifier(self.config)
        epoch = self.now.timestamp()
        with patch("telegram_notifications.time.time", return_value=epoch):
            self.assertTrue(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now))
            self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now))
        with patch("telegram_notifications.time.time", return_value=epoch + 60):
            self.assertTrue(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now + timedelta(seconds=60)))

    def test_post_payload_and_persistent_private_cooldown(self):
        notifier = TelegramNotifier(self.config)
        response = io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
        with patch("telegram_notifications.urllib.request.urlopen", return_value=response) as request:
            notifier._deliver(DEVICE.device_id, "Motion detected", self.now)
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(json.loads(sent.data), {"chat_id": "123", "text": "Motion detected", "parse_mode": "HTML"})
        self.assertEqual(request.call_args.kwargs["timeout"], 5)
        self.assertNotIn(TOKEN, self.config.state_path.read_text())
        self.assertEqual(self.config.state_path.stat().st_mode & 0o777, 0o600)
        restarted = TelegramNotifier(self.config)
        self.assertFalse(restarted.submit_motion(DEVICE, {"occupancy": True}, self.now))
        self.assertTrue(restarted.submit_motion(DEVICE, {"occupancy": False}, self.now))

    def test_clear_bypasses_cooldown_and_follows_pending_detection(self):
        notifier = TelegramNotifier(replace(self.config, sensor_label="motion-room1", location_label="Living Room"))
        with patch("telegram_notifications.urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(b'{"ok":true}')) as request:
            self.assertTrue(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now))
            # Clear can arrive while detection is queued or in flight.
            self.assertTrue(notifier.submit_motion(DEVICE, {"occupancy": False}, self.now))
            self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": False}, self.now))
            notifier._deliver(*notifier._queue.get_nowait())
            notifier._deliver(*notifier._queue.get_nowait())
        messages = [json.loads(call.args[0].data)["text"] for call in request.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertIn("🚨 <b>Motion detected</b>", messages[0])
        self.assertIn("✅ <b>Motion cleared</b>", messages[1])
        self.assertIn("📍 Location: Living Room", messages[1])
        self.assertIn("📡 Sensor: motion-room1", messages[1])
        self.assertFalse(TelegramNotifier(self.config).submit_motion(DEVICE, {"occupancy": False}, self.now))

    def test_suppressed_detection_does_not_generate_extra_clear(self):
        notifier = TelegramNotifier(self.config)
        with patch("telegram_notifications.urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(b'{"ok":true}')) as request:
            for occupancy in (True, False):
                self.assertTrue(notifier.submit_motion(DEVICE, {"occupancy": occupancy}, self.now))
                notifier._deliver(*notifier._queue.get_nowait())
            self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now))
            self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": False}, self.now))
        self.assertEqual(request.call_count, 2)

    def test_clear_does_not_reset_detection_cooldown(self):
        notifier = TelegramNotifier(self.config)
        with patch("telegram_notifications.urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(b'{"ok":true}')):
            for seconds, occupancy in ((0, True), (50, False), (60, True), (61, False)):
                event_time = self.now + timedelta(seconds=seconds)
                with patch("telegram_notifications.time.time", return_value=event_time.timestamp()):
                    self.assertTrue(notifier.submit_motion(DEVICE, {"occupancy": occupancy}, event_time))
                    notifier._deliver(*notifier._queue.get_nowait())

    def test_failed_detection_does_not_send_queued_clear(self):
        notifier = TelegramNotifier(self.config)
        notifier.submit_motion(DEVICE, {"occupancy": True}, self.now)
        notifier.submit_motion(DEVICE, {"occupancy": False}, self.now)
        with patch("telegram_notifications.urllib.request.urlopen", side_effect=OSError) as request:
            notifier._deliver(*notifier._queue.get_nowait())
            notifier._deliver(*notifier._queue.get_nowait())
        self.assertEqual(request.call_count, 1)
        self.assertFalse(TelegramNotifier(self.config).submit_motion(DEVICE, {"occupancy": False}, self.now))

    def test_rejected_detection_does_not_open_alert(self):
        notifier = TelegramNotifier(self.config)
        with patch("telegram_notifications.urllib.request.urlopen", return_value=io.BytesIO(b'{"ok":false}')):
            notifier._deliver(DEVICE.device_id, "Motion detected", self.now)
        self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": False}, self.now))

    def test_clear_failure_is_not_retried_after_restart(self):
        notifier = TelegramNotifier(self.config)
        with patch("telegram_notifications.urllib.request.urlopen", return_value=io.BytesIO(b'{"ok":true}')):
            notifier._deliver(DEVICE.device_id, "Motion detected", self.now)
        with patch("telegram_notifications.urllib.request.urlopen", side_effect=OSError):
            notifier._deliver(DEVICE.device_id, "Motion cleared", self.now, False)
        restarted = TelegramNotifier(self.config)
        self.assertFalse(restarted.submit_motion(DEVICE, {"occupancy": False}, self.now))

    def test_old_cooldown_state_loads_without_open_alerts(self):
        notifier = TelegramNotifier(self.config)
        self.config.state_path.write_text(json.dumps({"scope": notifier._scope,
            "attempted_at": {DEVICE.device_id: self.now.timestamp()}, "retry_until": 0}))
        restarted = TelegramNotifier(self.config)
        self.assertFalse(restarted.submit_motion(DEVICE, {"occupancy": True}, self.now))
        self.assertFalse(restarted.submit_motion(DEVICE, {"occupancy": False}, self.now))

    def test_different_chat_does_not_inherit_open_alert(self):
        notifier = TelegramNotifier(self.config)
        with patch("telegram_notifications.urllib.request.urlopen", return_value=io.BytesIO(b'{"ok":true}')):
            notifier._deliver(DEVICE.device_id, "Motion detected", self.now)
        other_chat = TelegramNotifier(replace(self.config, chat_id="456"))
        self.assertFalse(other_chat.submit_motion(DEVICE, {"occupancy": False}, self.now))

    def test_stale_clear_does_not_send(self):
        notifier = TelegramNotifier(self.config)
        notifier.submit_motion(DEVICE, {"occupancy": True}, self.now)
        self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": False}, self.now - timedelta(minutes=5)))

    def test_network_failure_does_not_log_token_or_retry(self):
        notifier = TelegramNotifier(self.config)
        with patch("telegram_notifications.urllib.request.urlopen", side_effect=OSError(TOKEN)) as request, \
             self.assertLogs("telegram_notifications", level="WARNING") as logs:
            notifier._deliver(DEVICE.device_id, "Motion detected", self.now)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn(TOKEN, " ".join(logs.output))
        self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now))

    def test_telegram_429_backoff_survives_restart(self):
        notifier = TelegramNotifier(self.config)
        error = urllib.error.HTTPError("https://api.telegram.org/bot" + TOKEN, 429, TOKEN, {},
                                       io.BytesIO(b'{"parameters":{"retry_after":300}}'))
        with patch("telegram_notifications.urllib.request.urlopen", side_effect=error), \
             self.assertLogs("telegram_notifications", level="WARNING") as logs:
            notifier._deliver(DEVICE.device_id, "Motion detected", self.now)
        self.assertNotIn(TOKEN, " ".join(logs.output))
        restarted = TelegramNotifier(self.config)
        later = self.now + timedelta(seconds=70)
        with patch("telegram_notifications.time.time", return_value=later.timestamp()):
            self.assertFalse(restarted.submit_motion(DEVICE, {"occupancy": True}, later))

    def test_unwritable_state_prevents_untracked_send(self):
        notifier = TelegramNotifier(self.config)
        with patch.object(notifier, "_save_state", side_effect=OSError), \
             patch("telegram_notifications.urllib.request.urlopen") as request, self.assertRaises(OSError):
            notifier._deliver(DEVICE.device_id, "Motion detected", self.now)
        request.assert_not_called()

    def test_slow_delivery_does_not_block_submit(self):
        notifier = TelegramNotifier(self.config)
        started, release = threading.Event(), threading.Event()
        def blocked_delivery(*_):
            started.set()
            release.wait(2)
        with patch.object(notifier, "_deliver", side_effect=blocked_delivery):
            notifier.start()
            try:
                self.assertTrue(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now))
                self.assertTrue(started.wait(1))
                # The sending worker is blocked; submitting still returns immediately.
                self.assertFalse(notifier.submit_motion(DEVICE, {"occupancy": True}, self.now))
            finally:
                release.set()
                notifier.close()

    def test_exact_message_format_for_both_states(self):
        local_time = datetime(2026, 9, 29, 20, 26).astimezone()
        for occupancy, icon, status in ((True, "🚨", "Motion detected"), (False, "✅", "Motion cleared")):
            message = format_motion_message(DEVICE, {"occupancy": occupancy, "battery": 0}, local_time,
                                           sensor_label="motion-room1", location_label="Living Room")
            self.assertEqual(message, f"{icon} <b>{status}</b>\n\n"
                "📍 Location: Living Room\n📡 Sensor: motion-room1\n"
                f"🕒 Time: 29.09.2026, 20:26\n⚡ Status: {status}")

    def test_labels_fall_back_to_metadata_and_escape_html(self):
        device = replace(DEVICE, name="Sensor <one>", room="Living & Dining")
        message = format_motion_message(device, {"occupancy": True}, self.now)
        self.assertIn("📍 Location: Living &amp; Dining", message)
        self.assertIn("📡 Sensor: Sensor &lt;one&gt;", message)
        message = format_motion_message(device, {"occupancy": False}, self.now,
                                       sensor_label="<b>sensor</b>", location_label="Room & hall")
        self.assertIn("📍 Location: Room &amp; hall", message)
        self.assertIn("📡 Sensor: &lt;b&gt;sensor&lt;/b&gt;", message)


class MotionNotificationHookTests(unittest.TestCase):
    def message(self, occupancy=True, retain=False):
        return SimpleNamespace(topic=DEVICE.topic, retain=retain,
                               payload=json.dumps({"occupancy": occupancy}).encode())

    def test_notify_only_after_committed_new_motion(self):
        actions = []
        with patch("motion_service.store_report", side_effect=lambda *_: actions.append("stored") or True):
            collect_motion_message({}, DEVICE, self.message(), lambda *_: actions.append("notified"))
        self.assertEqual(actions, ["stored", "notified"])

    def test_repeated_and_retained_reports_never_notify(self):
        notify = Mock()
        with patch("motion_service.store_report", return_value=False):
            collect_motion_message({}, DEVICE, self.message(), notify)
            collect_motion_message({}, DEVICE, self.message(False), notify)
        with patch("motion_service.store_report", return_value=True):
            collect_motion_message({}, DEVICE, self.message(retain=True), notify)
            collect_motion_message({}, DEVICE, self.message(False, retain=True), notify)
        notify.assert_not_called()

    def test_clear_transition_notifies_after_commit(self):
        actions = []
        with patch("motion_service.store_report", side_effect=lambda *_: actions.append("stored") or True):
            collect_motion_message({}, DEVICE, self.message(False),
                                   lambda config, report, when: actions.append(report["occupancy"]))
        self.assertEqual(actions, ["stored", False])

    def test_failed_database_write_never_notifies(self):
        notify = Mock()
        with patch("motion_service.store_report", side_effect=psycopg2.OperationalError), self.assertRaises(psycopg2.Error):
            collect_motion_message({}, DEVICE, self.message(), notify)
        notify.assert_not_called()

    def test_notification_error_does_not_undo_recorded_motion(self):
        with patch("motion_service.store_report", return_value=True), patch("motion_service.LOGGER.error"):
            self.assertTrue(collect_motion_message({}, DEVICE, self.message(), Mock(side_effect=RuntimeError)))


if __name__ == "__main__":
    unittest.main()
