import importlib.util
import json
import os
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

import psycopg2
from psycopg2 import sql
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import motion_service as motion

CONFIG = motion.MotionConfig("zigbee2mqtt/test-motion", "test-motion", "Test sensor", "Living room")


class MotionTests(unittest.TestCase):
    def test_false_zero_and_partial_reports(self):
        self.assertEqual(motion.parse_motion_report(b'{"occupancy":false,"battery":0,"battery_low":false,"linkquality":0}'),
                         {"occupancy": False, "battery": 0, "battery_low": False, "linkquality": 0})
        self.assertEqual(motion.parse_motion_report(b'{"battery":88}'), {"battery": 88})

    def test_malformed_reports(self):
        for payload in (b"bad", b"[]", b"null", b'{"occupancy":"false"}', b'{"occupancy":1}',
                        b'{"battery":true}', b'{"battery":101}', b'{"linkquality":-1}',
                        b'{"battery":NaN}', b'{"voltage":Infinity}', b'{}', b'\xff', b' ' * 8193):
            with self.subTest(payload=payload[:50]), self.assertRaises((ValueError, UnicodeDecodeError)):
                motion.parse_motion_report(payload)

    def test_retained_and_other_topics_never_write_events(self):
        with patch.object(motion, "store_report") as store:
            for topic, retain in ((CONFIG.topic, True), ("zigbee2mqtt/other", False), ("zigbee2mqtt/bridge/state", True)):
                self.assertFalse(motion.collect_motion_message({}, CONFIG,
                    SimpleNamespace(topic=topic, retain=retain, payload=b'{"occupancy":true}')))
            store.assert_not_called()

    def test_real_report_reaches_storage(self):
        with patch.object(motion, "store_report", return_value=True) as store:
            self.assertTrue(motion.collect_motion_message({}, CONFIG,
                SimpleNamespace(topic=CONFIG.topic, retain=False, payload=b'{"occupancy":true}')))
            store.assert_called_once_with({}, CONFIG, {"occupancy": True}, ANY)

    def test_disabled_does_not_query_database(self):
        with patch.object(motion, "connect") as connect:
            data = motion.get_motion_status({}, motion.MotionConfig())
            self.assertFalse(data["configured"])
            self.assertIsNone(data["today_count"])
            connect.assert_not_called()

    def test_config_exact_topic_and_id_fallback(self):
        with patch.dict(os.environ, {"MOTION_SENSOR_TOPIC": CONFIG.topic, "MOTION_SENSOR_ID": ""}):
            self.assertEqual(motion.MotionConfig.from_env().device_id, "test-motion")
        with patch.dict(os.environ, {"MOTION_SENSOR_TOPIC": "zigbee2mqtt/#"}), self.assertRaises(ValueError):
            motion.MotionConfig.from_env()


class MotionAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("motion_test_api", Path(__file__).resolve().parents[1] / "api.py")
        cls.api = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"DB_NAME":"test", "DB_USER":"test", "DB_PASSWORD":"test", "MOTION_SENSOR_TOPIC":""}), \
             patch("fastapi.staticfiles.StaticFiles", side_effect=lambda **kw: StaticFiles(**kw, check_dir=False)):
            spec.loader.exec_module(cls.api)
        cls.client = TestClient(cls.api.app)

    def test_response_serializes_timestamps_and_disables_cache(self):
        payload = motion.get_motion_status({}, motion.MotionConfig())
        payload["sensor"] = {"occupancy": False, "last_seen": datetime.now(timezone.utc)}
        with patch.object(self.api, "get_motion_status", return_value=payload):
            response = self.client.get("/api/motion")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIs(response.json()["sensor"]["occupancy"], False)
        self.assertIsInstance(response.json()["sensor"]["last_seen"], str)

    def test_limits_are_validated(self):
        for query in ("hours=0", "hours=169", "limit=0", "limit=101", "hours=bad"):
            self.assertEqual(self.client.get("/api/motion?" + query).status_code, 422)

    def test_db_failure_is_safe_503(self):
        with patch.object(self.api, "get_motion_status", side_effect=psycopg2.OperationalError("private-db-detail")), \
             patch.object(self.api.LOGGER, "exception"):
            response = self.client.get("/api/motion")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-db-detail", response.text)


class CollectorRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("motion_test_collector", Path(__file__).resolve().parents[1] / "mqtt_collector.py")
        cls.collector = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"DB_NAME":"test", "DB_USER":"test", "DB_PASSWORD":"test"}):
            spec.loader.exec_module(cls.collector)

    def test_motion_json_is_not_parsed_as_climate_number(self):
        with patch.object(self.collector, "MOTION_CONFIG", CONFIG), \
             patch.object(self.collector, "collect_motion_message") as collect, \
             patch.object(self.collector, "schedule_save") as save:
            message = SimpleNamespace(topic=CONFIG.topic, payload=b'{"occupancy":true}', retain=False)
            self.collector.on_message(None, None, message)
            collect.assert_called_once()
            save.assert_not_called()

    def test_climate_batching_still_receives_humidity(self):
        with patch.object(self.collector, "schedule_save") as save:
            self.collector.on_message(None, None,
                SimpleNamespace(topic=self.collector.TOPIC_HUMIDITY, payload=b"53.0", retain=False))
            self.assertEqual(self.collector.humidity, 53.0)
            save.assert_called_once()


@unittest.skipUnless(os.getenv("RUN_MOTION_DB_TESTS") == "1", "Opt-in PostgreSQL tests use an isolated temporary schema")
class MotionPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "motion_test_" + uuid.uuid4().hex
        cls.base_config = {"host": os.getenv("DB_HOST", "localhost"), "port": os.getenv("DB_PORT", "5432"),
                           "database": os.environ["DB_NAME"], "user": os.environ["DB_USER"], "password": os.environ["DB_PASSWORD"]}
        with closing(psycopg2.connect(**cls.base_config)) as conn, conn, conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.config = dict(cls.base_config, options=f"-c search_path={cls.schema} -c timezone=Europe/Kyiv")
        cls.addClassCleanup(cls.remove_schema)
        motion.initialize_schema(cls.config)

    @classmethod
    def remove_schema(cls):
        with closing(psycopg2.connect(**cls.base_config)) as conn, conn, conn.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.device = motion.MotionConfig(CONFIG.topic, uuid.uuid4().hex, CONFIG.name, CONFIG.room)

    def test_migration_is_repeatable_and_preserves_rows(self):
        motion.store_report(self.config, self.device, {"occupancy": True})
        motion.initialize_schema(self.config)
        self.assertEqual(len(motion.get_motion_status(self.config, self.device)["events"]), 1)

    def test_dedup_transitions_and_restart_persistence(self):
        for value, expected in ((True, True), (True, False), (False, True), (False, False), (True, True)):
            self.assertEqual(motion.store_report(self.config, self.device, {"occupancy": value}), expected)
        # Every store/read opened a new connection, so no process-local baseline is needed.
        data = motion.get_motion_status(self.config, self.device)
        self.assertEqual([e["occupancy"] for e in data["events"]], [True, False, True])
        self.assertEqual(data["today_count"], 2)
        self.assertTrue(data["sensor"]["occupancy"])
        self.assertEqual(data["device"]["room"], "Living room")

    def test_partial_report_preserves_occupancy_and_last_motion(self):
        moment = datetime.now(timezone.utc) - timedelta(minutes=1)
        motion.store_report(self.config, self.device, {"occupancy": True, "battery": 75}, moment)
        motion.store_report(self.config, self.device, {"battery": 0, "battery_low": True, "linkquality": 0})
        data = motion.get_motion_status(self.config, self.device)
        self.assertTrue(data["sensor"]["occupancy"])
        self.assertEqual(data["sensor"]["battery"], 0)
        self.assertEqual(data["sensor"]["state_reported_at"], moment)
        self.assertEqual(data["sensor"]["last_motion_at"], moment)
        self.assertEqual(len(data["events"]), 1)

    def test_initial_clear_is_not_counted_as_motion(self):
        motion.store_report(self.config, self.device, {"occupancy": False})
        data = motion.get_motion_status(self.config, self.device)
        self.assertEqual(data["today_count"], 0)
        self.assertIsNone(data["sensor"]["last_motion_at"])

    def test_today_count_uses_local_midnight_and_device_filter(self):
        with closing(motion.connect(self.config)) as conn, conn, conn.cursor() as cur:
            cur.execute("SELECT date_trunc('day', NOW())")
            midnight = cur.fetchone()[0]
        motion.store_report(self.config, self.device, {"occupancy": True}, midnight - timedelta(seconds=1))
        motion.store_report(self.config, self.device, {"occupancy": False}, midnight)
        motion.store_report(self.config, self.device, {"occupancy": True}, midnight + timedelta(seconds=1))
        motion.store_report(self.config, CONFIG, {"occupancy": True})
        data = motion.get_motion_status(self.config, self.device, hours=168, limit=2)
        self.assertEqual(data["today_count"], 1)
        self.assertEqual(data["day_timezone"], "Europe/Kyiv")
        self.assertEqual(len(data["events"]), 2)
        self.assertTrue(all(e["device_id"] == self.device.device_id for e in data["events"]))

    def test_waiting_state_has_no_invented_motion(self):
        data = motion.get_motion_status(self.config, self.device)
        self.assertIsNone(data["sensor"])
        self.assertEqual(data["today_count"], 0)
        self.assertEqual(data["events"], [])


if __name__ == "__main__":
    unittest.main()
