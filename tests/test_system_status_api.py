import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from system_status import SystemStatusMonitor


class SystemStatusAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("system_status_test_api", Path(__file__).resolve().parents[1] / "api.py")
        cls.api = importlib.util.module_from_spec(spec)
        # No deployment directory, credentials, or network access required.
        with patch.dict(os.environ, {"DB_NAME":"test", "DB_USER":"test", "DB_PASSWORD":"test"}), \
             patch("fastapi.staticfiles.StaticFiles", side_effect=lambda **kw: StaticFiles(**kw, check_dir=False)):
            spec.loader.exec_module(cls.api)

    def test_existing_endpoints_preserved(self):
        paths = {route.path for route in self.api.app.routes}
        self.assertTrue({"/", "/api/sensors/latest", "/api/sensors/history", "/api/weather/current", "/api/analytics/room", "/api/system/status"} <= paths)

    def test_expected_probe_failures_return_200(self):
        monitor = SystemStatusMonitor({})
        with patch.object(monitor, "_cpu_usage", side_effect=OSError), \
             patch("system_status.read_cpu_temperature", return_value=None), \
             patch.object(monitor,"_memory",side_effect=OSError), \
             patch.object(monitor,"_disk",side_effect=OSError), \
             patch.object(monitor,"_host",side_effect=OSError), \
             patch.object(monitor,"_mqtt",return_value={"status":"offline"}), \
             patch.object(monitor,"_database",return_value={"status":"error","query_ok":False,"last_seen":None,"telemetry_query_ok":False}), \
             patch.object(monitor,"_internet",return_value={"status":"offline"}), \
             patch.object(self.api,"system_monitor",monitor):
            response = TestClient(self.api.app).get("/api/system/status")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers["cache-control"],"no-store")
        data = response.json()
        self.assertIsNone(data["cpu"]["temperature_c"])
        self.assertEqual(data["services"]["database"]["status"],"error")
        self.assertEqual(data["services"]["esp32"]["status"],"unknown")
        self.assertEqual(data["services"]["api"]["status"],"running")
        self.assertEqual(data["overall_status"],"degraded")


if __name__ == "__main__":
    unittest.main()
