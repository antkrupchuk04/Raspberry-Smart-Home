import copy
import json
import threading
import unittest
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg2

import system_status as status


class SystemStatusTests(unittest.TestCase):
    def setUp(self):
        self.monitor = status.SystemStatusMonitor({"database": "test", "password": "secret-test"})
        self.now = datetime.now(timezone.utc)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        values = {
            "_cpu_usage": 12.3,
            "_memory": {"used_gb": 1.3, "total_gb": 7.9, "usage_percent": 16.5},
            "_disk": {"used_gb": 5.2, "total_gb": 57.0, "usage_percent": 9.1},
            "_host": {"hostname": "test-pi", "boot_time": self.now.timestamp() - 83622},
            "_mqtt": {"status": "online", "host": "localhost", "port": 1883},
            "_database": {"status": "connected", "query_ok": True,
                          "last_seen": self.now - timedelta(seconds=3), "telemetry_query_ok": True},
            "_internet": {"status": "online"},
            "_zigbee": {"status": "not_configured", "configured": False, "device_present": False},
        }
        self.probes = {key: self.stack.enter_context(patch.object(self.monitor, key, return_value=value))
                       for key, value in values.items()}
        self.temperature = self.stack.enter_context(patch.object(status, "read_cpu_temperature", return_value=42.7))

    def test_cpu_ram_disk_serialization_and_security(self):
        result = self.monitor.get_system_status()
        self.assertEqual(result["cpu"], {"usage_percent": 12.3, "temperature_c": 42.7})
        self.assertEqual(result["memory"]["used_gb"], 1.3)
        self.assertEqual(result["disk"]["usage_percent"], 9.1)
        encoded = json.dumps(result, allow_nan=False)
        for secret in ("secret-test", "password", "boot_time", "last_seen_query", "traceback"):
            self.assertNotIn(secret, encoded)

    def test_psutil_memory_and_disk_units(self):
        monitor = status.SystemStatusMonitor({})
        with patch.object(status.psutil, "virtual_memory", return_value=SimpleNamespace(total=8*status.GB, available=6*status.GB, percent=25)), \
             patch.object(status.psutil, "disk_usage", return_value=SimpleNamespace(total=64*status.GB, used=8*status.GB, percent=12.5)) as disk:
            self.assertEqual(monitor._memory(), {"used_gb": 2.0, "total_gb": 8.0, "usage_percent": 25})
            self.assertEqual(monitor._disk()["used_gb"], 8)
            disk.assert_called_once_with("/")

    def test_cpu_sampling_is_nonblocking_and_excludes_guest_iowait(self):
        monitor = status.SystemStatusMonitor({})
        counters = namedtuple("Counters", "user nice system idle iowait guest guest_nice")
        with patch.object(status.psutil, "cpu_times", side_effect=[counters(0,0,0,0,0,0,0), counters(20,0,10,60,10,5,0)]), \
             patch.object(status.psutil, "cpu_percent") as percent:
            self.assertIsNone(monitor._cpu_usage())
            self.assertEqual(monitor._cpu_usage(), 30.0)
            percent.assert_not_called()

    def test_cpu_counter_reset_does_not_produce_invalid_usage(self):
        monitor = status.SystemStatusMonitor({})
        counter = namedtuple("Counter", "user idle")
        with patch.object(status.psutil, "cpu_times", side_effect=[counter(100,200), counter(0,0)]):
            monitor._cpu_usage()
            self.assertIsNone(monitor._cpu_usage())

    def test_temperature_parsing_and_missing_invalid_data(self):
        # Use the real function rather than this test fixture's patched probe.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "temp"
            for value, expected in [("42700\n",42.7),("bad",None),("nan",None),("inf",None)]:
                path.write_text(value)
                self.assertEqual(REAL_TEMPERATURE(path), expected)
            self.assertIsNone(REAL_TEMPERATURE(path.parent / "missing"))

    def test_uptime_formatting(self):
        for value, expected in [(0,"0m"),(59,"0m"),(60,"1m"),(3660,"1h 1m"),(187980,"2d 4h 13m"),(None,None)]:
            self.assertEqual(status.format_uptime(value), expected)

    def test_uptime_uses_real_hostname_and_boot_time(self):
        monitor = status.SystemStatusMonitor({})
        with patch.object(status.socket, "gethostname", return_value="pi-test") as host, \
             patch.object(status.psutil, "boot_time", return_value=12345):
            self.assertEqual(monitor._host(), {"hostname": "pi-test", "boot_time": 12345})
            host.assert_called_once()

    def test_mqtt_reachable(self):
        with patch.object(status.socket, "create_connection") as connect:
            result = status.SystemStatusMonitor({})._mqtt()
            self.assertEqual(result["status"], "online")
            connect.assert_called_once_with(("localhost",1883), timeout=status.SOCKET_TIMEOUT_SECONDS)
            connect.return_value.__exit__.assert_called_once()

    def test_mqtt_unavailable(self):
        with patch.object(status.socket, "create_connection", side_effect=ConnectionRefusedError), \
             patch.object(status.LOGGER, "exception") as log:
            self.assertEqual(status.SystemStatusMonitor({})._mqtt()["status"], "offline")
            log.assert_not_called()

    def test_database_select_one_and_latest_timestamp_close_connection(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [(1,), (self.now,)]
        config = {"host":"localhost", "database":"smarthome", "password":"secret", "options":"-c application_name=health"}
        with patch.object(status.psycopg2, "connect", return_value=connection) as connect:
            result = status.SystemStatusMonitor(config)._database()
            self.assertEqual(result["status"], "connected")
            self.assertTrue(result["query_ok"])
            self.assertEqual(result["last_seen"], self.now)
            self.assertEqual(cursor.execute.call_args_list[0].args, ("SELECT 1",))
            self.assertIn("ORDER BY recorded_at DESC LIMIT 1", cursor.execute.call_args_list[1].args[0])
            connection.close.assert_called_once()
            self.assertEqual(connect.call_args.kwargs["connect_timeout"],2)
            self.assertIn("statement_timeout=1000",connect.call_args.kwargs["options"])
            self.assertEqual(config["options"],"-c application_name=health")

    def test_database_connection_failure(self):
        with patch.object(status.psycopg2,"connect",side_effect=psycopg2.OperationalError("password secret")), \
             patch.object(status.LOGGER,"exception") as log:
            result = status.SystemStatusMonitor({})._database()
            self.assertEqual(result["status"],"error")
            self.assertFalse(result["query_ok"])
            self.assertNotIn("secret",json.dumps(result))
            log.assert_not_called()

    def test_database_query_failure_closes_connection(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.execute.side_effect = psycopg2.OperationalError("timeout")
        with patch.object(status.psycopg2,"connect",return_value=connection):
            self.assertFalse(status.SystemStatusMonitor({})._database()["query_ok"])
            connection.close.assert_called_once()

    def test_telemetry_query_failure_does_not_hide_select_one(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = [None, psycopg2.ProgrammingError("missing table")]
        cursor.fetchone.return_value = (1,)
        with patch.object(status.psycopg2,"connect",return_value=connection):
            result = status.SystemStatusMonitor({})._database()
            self.assertTrue(result["query_ok"])
            self.assertFalse(result["telemetry_query_ok"])

    def test_internet_reachable_and_offline(self):
        for error, expected in [(None,"online"),(TimeoutError(),"offline")]:
            with patch.object(status.socket,"create_connection",side_effect=error) as connect:
                self.assertEqual(status.SystemStatusMonitor({})._internet()["status"], expected)
                connect.assert_called_once_with(status.INTERNET_TARGET, timeout=status.SOCKET_TIMEOUT_SECONDS)

    def test_esp32_fresh(self):
        result = status.esp32_status(self.now-timedelta(seconds=4.2),self.now)
        self.assertEqual(result["status"],"online")
        self.assertEqual(result["age_seconds"],4.2)

    def test_esp32_stale_and_exact_boundary(self):
        for age in [15,16,5000]:
            self.assertEqual(status.esp32_status(self.now-timedelta(seconds=age),self.now)["status"],"offline")

    def test_esp32_missing_database_and_future_timestamp(self):
        self.assertEqual(status.esp32_status(None,self.now)["status"],"offline")
        self.assertEqual(status.esp32_status(None,self.now,False)["status"],"unknown")
        self.assertEqual(status.esp32_status(self.now+timedelta(seconds=1),self.now)["status"],"unknown")
        self.assertEqual(status.esp32_status(self.now.replace(tzinfo=None),self.now)["status"],"unknown")

    def test_zigbee_not_configured_does_not_scan_serial_devices(self):
        with patch.object(status.Path,"exists") as exists:
            self.assertEqual(status.SystemStatusMonitor({})._zigbee(),
                             {"configured":False,"device_present":False,"status":"not_configured"})
            exists.assert_not_called()

    def test_zigbee_configured_present_and_missing(self):
        for present in [True,False]:
            with patch.object(status.Path,"exists",return_value=present):
                result = status.SystemStatusMonitor({},zigbee_device="/dev/ttyUSB0")._zigbee()
                self.assertTrue(result["configured"])
                self.assertEqual(result["device_present"],present)
                self.assertEqual(result["status"],"device_present" if present else "offline")
                self.assertNotIn("/dev",json.dumps(result))

    def test_one_failed_metric_does_not_break_full_response(self):
        self.probes["_memory"].side_effect = OSError("unavailable")
        self.temperature.return_value = None
        result = self.monitor.get_system_status()
        self.assertIsNone(result["memory"]["usage_percent"])
        self.assertIsNone(result["cpu"]["temperature_c"])
        self.assertEqual(result["cpu"]["usage_percent"],12.3)
        self.assertEqual(result["services"]["database"]["status"],"connected")
        json.dumps(result,allow_nan=False)

    def test_healthy_optional_zigbee_is_not_a_failure(self):
        result = self.monitor.get_system_status()
        self.assertEqual(result["overall_status"],"healthy")
        self.assertEqual(result["alerts"],[])

    def test_degraded_database_and_mqtt(self):
        for probe, offline in [("_mqtt",{"status":"offline"}),
                               ("_database",{"status":"error","query_ok":False,"last_seen":None,"telemetry_query_ok":False})]:
            with self.subTest(probe=probe):
                self.monitor._cache.clear()
                previous = self.probes[probe].return_value
                self.probes[probe].return_value = offline
                self.assertEqual(self.monitor.get_system_status()["overall_status"],"degraded")
                self.probes[probe].return_value = previous

    def test_high_metrics_and_critical_temperature(self):
        for probe,key,threshold in [("_cpu_usage",None,80),("_memory","usage_percent",85),("_disk","usage_percent",85)]:
            with self.subTest(probe=probe):
                self.monitor._cache.clear()
                original = copy.deepcopy(self.probes[probe].return_value)
                if key: self.probes[probe].return_value[key] = threshold
                else: self.probes[probe].return_value = threshold
                self.assertEqual(self.monitor.get_system_status()["overall_status"],"warning")
                self.probes[probe].return_value = original
        for temperature, expected in [(70,"warning"),(80,"degraded")]:
            self.monitor._cache.clear()
            self.temperature.return_value = temperature
            self.assertEqual(self.monitor.get_system_status()["overall_status"],expected)

    def test_internet_offline_is_warning(self):
        self.probes["_internet"].return_value = {"status":"offline"}
        self.assertEqual(self.monitor.get_system_status()["overall_status"],"warning")

    def test_cache_ttls_and_result_mutation_safety(self):
        with patch.object(status.time,"monotonic",return_value=100) as clock:
            first = self.monitor.get_system_status()
            first["memory"]["usage_percent"] = 999
            clock.return_value = 104
            self.assertEqual(self.monitor.get_system_status()["memory"]["usage_percent"],16.5)
            clock.return_value = 105
            self.monitor.get_system_status()
            self.assertEqual(self.probes["_mqtt"].call_count,2)
            self.assertEqual(self.probes["_database"].call_count,2)
            self.assertEqual(self.probes["_internet"].call_count,1)
            clock.return_value = 130
            self.monitor.get_system_status()
            self.assertEqual(self.probes["_zigbee"].call_count,2)
            clock.return_value = 145
            self.monitor.get_system_status()
            self.assertEqual(self.probes["_internet"].call_count,2)

    def test_concurrent_requests_share_probes(self):
        barrier = threading.Barrier(4)
        def read(_):
            barrier.wait()
            return self.monitor.get_system_status()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(read,range(4)))
        self.assertTrue(all(result["overall_status"] == "healthy" for result in results))
        for probe in self.probes.values(): probe.assert_called_once()

    def test_unexpected_failures_log_once_and_return_safe_fallback(self):
        self.probes["_disk"].side_effect = RuntimeError("private implementation details")
        with patch.object(status.LOGGER,"exception") as log:
            result = self.monitor.get_system_status()
            self.monitor._cache.clear()
            self.monitor.get_system_status()
            log.assert_called_once()
            self.assertIsNone(result["disk"]["usage_percent"])
            self.assertNotIn("private",json.dumps(result))


REAL_TEMPERATURE = status.read_cpu_temperature

if __name__ == "__main__":
    unittest.main()
