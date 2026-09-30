import copy
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import psycopg2
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from motion_service import MotionConfig
from topology_service import TopologyService, climate_node, hub_node, motion_node, connection

NOW = datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc)
CLIMATE = {"id": "esp32-room1", "name": "ESP32 Room 1", "room": "Room 1"}
MOTION = {"id": "motion-room1", "config": MotionConfig("zigbee2mqtt/test", "test-ieee", "Tuya TS0202", "Living room")}
SYSTEM = {"overall_status": "healthy", "host": {"hostname": "raspberry", "uptime": "1d 2h"},
          "cpu": {"usage_percent": 2.3, "temperature_c": 42.1}, "memory": {"usage_percent": 18}, "disk": {"usage_percent": 15},
          "services": {"api": {"status": "running"}, "mqtt": {"status": "online"}, "database": {"status": "connected"}, "internet": {"status": "online"},
                       "zigbee": {"configured": True, "device_present": True, "status": "device_present"}}}


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Mock()
        self.monitor.get_system_status.return_value = copy.deepcopy(SYSTEM)
        self.service = TopologyService({"password": "private-test-value"}, self.monitor, (MOTION,))

    def test_hub_status_mapping_and_missing_metrics(self):
        for overall, status in (("healthy", "online"), ("warning", "warning"), ("degraded", "degraded")):
            self.assertEqual(hub_node({**SYSTEM, "overall_status": overall})["status"], status)
        node = hub_node(None)
        self.assertEqual(node["status"], "unknown")
        self.assertTrue(all(field["value"] is None for field in node["metrics"]))
        self.assertIn("Not monitored", [field["value"] for field in hub_node(SYSTEM)["details"]])

    def test_esp32_uses_existing_freshness_boundaries(self):
        for age, status in ((0, "online"), (14.9, "online"), (15, "offline"), (300, "offline"), (-1, "unknown")):
            node = climate_node(CLIMATE, {"recorded_at": NOW - timedelta(seconds=age)}, True, NOW)
            self.assertEqual(node["status"], status)
            if status == "online":
                self.assertAlmostEqual(node["online_for_seconds"], 15 - age)
        self.assertEqual(climate_node(CLIMATE, None, True, NOW)["status"], "offline")
        self.assertEqual(climate_node(CLIMATE, None, False, NOW)["status"], "unknown")

    def test_climate_zero_null_and_invalid_metrics(self):
        node = climate_node(CLIMATE, {"temperature": 0, "humidity": None, "pressure": float('nan')}, True, NOW)
        self.assertEqual([field["value"] for field in node["metrics"]], [0, None, None])
        self.assertNotIn("rssi", json.dumps(node).lower().replace(node["note"].lower(), ""))
        json.dumps(node, allow_nan=False)

    def test_motion_states_are_not_device_availability(self):
        for occupancy, activity in ((True, "detected"), (False, "clear"), (None, "unknown")):
            node = motion_node(MOTION, {"occupancy": occupancy, "battery": 0, "linkquality": 0,
                                       "last_seen": NOW - timedelta(days=2)}, True, NOW)
            self.assertEqual(node["activity"], activity)
            self.assertEqual(node["status"], "unknown")
            self.assertEqual(node["device_id"], "test-ieee")
            self.assertEqual(node["metrics"][1]["value"], 0)
            self.assertEqual(node["metrics"][2]["value"], 0)
            self.assertEqual(node["age_seconds"], 172800)

    def test_motion_missing_data_and_low_battery(self):
        self.assertEqual(motion_node(MOTION, None, False, NOW)["data_status"], "unavailable")
        self.assertEqual(motion_node(MOTION, None, True, NOW)["data_status"], "waiting")
        node = motion_node(MOTION, {"battery_low": True, "last_seen": NOW + timedelta(seconds=5)}, True, NOW)
        self.assertEqual(node["metrics"][1]["state"], "warning")
        self.assertIsNone(node["age_seconds"])

    def test_links_require_evidence(self):
        node = climate_node(CLIMATE, {"recorded_at": NOW}, True, NOW)
        self.assertEqual(connection(node, SYSTEM, "wifi")["status"], "online")
        self.assertEqual(connection(node, {}, "wifi")["status"], "unknown")
        self.assertEqual(connection(node, SYSTEM, "zigbee")["status"], "unknown")
        self.assertEqual(connection(node, {"services": {"api": {"status": "running"}, "mqtt": {"status": "offline"}}}, "wifi")["status"], "offline")
        self.assertEqual(connection(node, {"services": {"zigbee": {"status": "offline"}}}, "zigbee")["status"], "offline")
        node["status"] = "offline"
        self.assertEqual(connection(node, SYSTEM, "wifi")["status"], "stale")

    def test_response_structure_and_private_config_not_exposed(self):
        with patch.object(self.service, "_query", return_value={}):
            data = self.service.get_topology()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["system"], {"status": "healthy"})
        self.assertEqual(next(field["value"] for field in data["nodes"][0]["details"] if field["key"] == "api"), "Running")
        self.assertEqual(len(data["nodes"]), 3)
        self.assertEqual(len(data["links"]), 2)
        self.assertEqual(data["refresh_seconds"], 10)
        ids = {node["id"] for node in data["nodes"]}
        self.assertEqual(ids, {"raspberry", "esp32-room1", "motion-room1"})
        self.assertTrue(all(edge["source"] in ids and edge["target"] in ids for edge in data["links"]))
        self.assertNotIn("private-test-value", json.dumps(data, allow_nan=False))

    def test_isolated_climate_query_failure_preserves_motion(self):
        report = {"test-ieee": {"occupancy": True, "last_seen": NOW}}
        with patch.object(self.service, "_query", side_effect=[psycopg2.OperationalError("private-test-value"), report]), \
             self.assertLogs("topology_service", level="WARNING") as logs:
            data = self.service.get_topology()
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["nodes"][0]["status"], "online")
        self.assertEqual(data["nodes"][1]["data_status"], "unavailable")
        self.assertEqual(data["nodes"][2]["activity"], "detected")
        self.assertNotIn("private-test-value", str(logs.output))

    def test_system_failure_does_not_hide_fresh_climate(self):
        self.monitor.get_system_status.side_effect = RuntimeError('private-test-value')
        with patch.object(self.service, "_query", side_effect=[{"esp32-room1": {"recorded_at": datetime.now(timezone.utc)}}, {}]):
            data = self.service.get_topology()
        self.assertEqual(data["nodes"][0]["status"], "unknown")
        self.assertEqual(data["system"], {"status": "unknown"})
        self.assertEqual(data["nodes"][1]["status"], "online")
        self.assertEqual(data["links"][0]["status"], "unknown")
        self.assertEqual(data["status"], "partial")

    def test_all_queries_fail_without_leaking_errors(self):
        with patch.object(self.service, "_query", side_effect=psycopg2.OperationalError('private-test-value')):
            data = self.service.get_topology()
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["nodes"][0]["status"], "online")
        self.assertTrue(all(node["data_status"] == "unavailable" for node in data["nodes"][1:]))
        self.assertNotIn("private-test-value", json.dumps(data))

    def test_cached_reads_expire_and_results_are_independent(self):
        with patch.object(self.service, "_query", return_value={}) as query, patch("topology_service.time.monotonic", return_value=100):
            self.service.get_topology()["nodes"].clear()
            self.assertEqual(len(self.service.get_topology()["nodes"]), 3)
            self.assertEqual(query.call_count, 2)
            with patch("topology_service.time.monotonic", return_value=106):
                self.service.get_topology()
            self.assertEqual(query.call_count, 4)

    def test_failures_are_cached_and_log_once_until_recovery(self):
        with patch.object(self.service, "_query", side_effect=psycopg2.OperationalError), \
             patch("topology_service.LOGGER.warning") as warning, patch("topology_service.time.monotonic", return_value=100):
            self.service.get_topology()
            self.service.get_topology()
            with patch("topology_service.time.monotonic", return_value=110):
                self.service.get_topology()
            self.assertEqual(warning.call_count, 2)

    def test_multiple_devices_and_unconfigured_motion(self):
        more = {**CLIMATE, "id": "esp32-room2", "name": "ESP32 Room 2"}
        disabled = {"id": "disabled", "config": MotionConfig()}
        service = TopologyService({}, self.monitor, (MOTION, disabled), (CLIMATE, more))
        with patch.object(service, "_query", return_value={}) as query:
            data = service.get_topology()
        self.assertEqual(len(data["nodes"]), 4)
        self.assertEqual(len(data["links"]), 3)
        self.assertEqual(query.call_args_list[0].args[1], ["esp32-room1", "esp32-room2"])
        self.assertEqual(query.call_args_list[1].args[1], ["test-ieee"])

    def test_duplicate_node_id_rejected(self):
        with self.assertRaises(ValueError):
            TopologyService({}, self.monitor, climate_devices=(CLIMATE, CLIMATE))

    def test_query_closes_connection_and_parameterizes_device_ids(self):
        connection_mock = Mock()
        cursor = Mock()
        connection_mock.cursor.return_value.__enter__ = Mock(return_value=cursor)
        connection_mock.cursor.return_value.__exit__ = Mock(return_value=False)
        cursor.fetchall.return_value = [{"device_id": "test", "temperature": 0}]
        with patch("topology_service.psycopg2.connect", return_value=connection_mock) as connect:
            data = self.service._query("SELECT device_id WHERE device_id = ANY(%s)", ["test"])
        self.assertEqual(data["test"]["temperature"], 0)
        cursor.execute.assert_called_once_with("SELECT device_id WHERE device_id = ANY(%s)", (["test"],))
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 2)
        self.assertIn("statement_timeout=1000", connect.call_args.kwargs["options"])
        connection_mock.close.assert_called_once()


class TopologyAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("topology_test_api", Path(__file__).resolve().parents[1] / "api.py")
        cls.api = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"DB_NAME": "test", "DB_USER": "test", "DB_PASSWORD": "test", "MOTION_SENSOR_TOPIC": ""}), \
             patch("fastapi.staticfiles.StaticFiles", side_effect=lambda **kw: StaticFiles(**kw, check_dir=False)):
            spec.loader.exec_module(cls.api)
        cls.client = TestClient(cls.api.app)

    def test_topology_route_preserves_existing_routes(self):
        self.assertTrue({"/", "/topology", "/api/topology", "/api/motion", "/api/system/status", "/api/sensors/latest", "/api/sensors/history", "/api/analytics/room", "/api/weather/current"}.issubset({route.path for route in self.api.app.routes}))

    def test_topology_page_serves_dedicated_html(self):
        page = Path(__file__).resolve().parents[1] / "static/topology.html"
        with patch.object(self.api, "FileResponse", return_value=FileResponse(page)) as file_response:
            response = self.client.get("/topology")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("/static/topology-page.js", response.text)
        self.assertNotIn("/static/app.js", response.text)
        file_response.assert_called_once_with("/home/anton/smarthome/static/topology.html")

    def test_partial_result_is_http_200_and_not_cached(self):
        service = TopologyService({}, Mock(), (MOTION,))
        service.system_monitor.get_system_status.return_value = copy.deepcopy(SYSTEM)
        with patch.object(service, "_query", side_effect=psycopg2.OperationalError), patch.object(self.api, "topology_service", service):
            response = self.client.get("/api/topology")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["status"], "partial")
        self.assertEqual(len(response.json()["nodes"]), 3)


if __name__ == "__main__":
    unittest.main()
