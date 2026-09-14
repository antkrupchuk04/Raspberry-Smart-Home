"""Small, on-demand host and dependency probes for the monitoring dashboard."""
import copy
import logging
import math
import socket
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import psutil
import psycopg2

LOGGER = logging.getLogger(__name__)

SYSTEM_THRESHOLDS = {
    "cpu_warning": 80,
    "memory_warning": 85,
    "disk_warning": 85,
    "cpu_temp_warning": 70,
    "cpu_temp_critical": 80,
    "esp32_timeout_seconds": 15,
}
CACHE_TTLS = {"cpu": 5, "temperature": 5, "memory": 5, "disk": 5,
              "host": 5, "mqtt": 5, "database": 5, "internet": 45, "zigbee": 30}
SOCKET_TIMEOUT_SECONDS = 0.75
DB_CONNECT_TIMEOUT_SECONDS = 2
DB_STATEMENT_TIMEOUT_MS = 1000
THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"
INTERNET_TARGET = ("1.1.1.1", 443)
GB = 1024 ** 3


def format_uptime(seconds):
    if seconds is None:
        return None
    minutes = max(0, int(seconds)) // 60
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return " ".join(([f"{days}d"] if days else []) +
                    ([f"{hours}h"] if hours or days else []) + [f"{minutes}m"])


def read_cpu_temperature(path=THERMAL_PATH):
    try:
        value = float(Path(path).read_text().strip()) / 1000
        return round(value, 1) if math.isfinite(value) else None
    except (OSError, ValueError):
        return None


def tcp_reachable(host, port):
    try:
        with socket.create_connection((host, port), timeout=SOCKET_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def esp32_status(last_seen, now, query_ok=True):
    result = {"status": "unknown", "last_seen": None, "age_seconds": None}
    if not query_ok:
        return result
    if last_seen is None:
        return {**result, "status": "offline"}
    # PostgreSQL returns aware datetimes; do not guess the zone of an invalid value.
    if not isinstance(last_seen, datetime) or last_seen.tzinfo is None:
        return result
    age = (now - last_seen).total_seconds()
    result.update(last_seen=last_seen.astimezone(timezone.utc).isoformat(), age_seconds=round(age, 1))
    if age < 0:
        return result
    result["status"] = "online" if age < SYSTEM_THRESHOLDS["esp32_timeout_seconds"] else "offline"
    return result


def aggregate_status(result):
    alerts = []

    def alert(component, status, message):
        alerts.append({"component": component, "status": status, "message": message})

    services = result["services"]
    for name, expected in (("mqtt", "online"), ("database", "connected"), ("api", "running")):
        if services[name]["status"] != expected:
            alert(name, "critical", f"{name.upper() if name != 'database' else 'Database'} unavailable")
    for name in ("internet", "esp32"):
        if services[name]["status"] != "online":
            alert(name, "warning", f"{name.upper() if name == 'esp32' else 'Internet'} {services[name]['status']}")
    zigbee = services["zigbee"]
    if zigbee["configured"] and not zigbee["device_present"]:
        alert("zigbee", "warning", "Configured Zigbee device is missing")
    for name, threshold in (("cpu", "cpu_warning"), ("memory", "memory_warning"), ("disk", "disk_warning")):
        value = result[name]["usage_percent"]
        if value is None:
            alert(name, "warning", f"{name.upper() if name == 'cpu' else name.capitalize()} usage unavailable")
        elif value >= SYSTEM_THRESHOLDS[threshold]:
            alert(name, "warning", f"{name.upper() if name == 'cpu' else name.capitalize()} usage is high")
    temperature = result["cpu"]["temperature_c"]
    if temperature is None:
        alert("temperature", "warning", "CPU temperature unavailable")
    elif temperature >= SYSTEM_THRESHOLDS["cpu_temp_critical"]:
        alert("temperature", "critical", "CPU temperature is critical")
    elif temperature >= SYSTEM_THRESHOLDS["cpu_temp_warning"]:
        alert("temperature", "warning", "CPU temperature is high")
    if result["host"]["uptime_seconds"] is None:
        alert("host", "warning", "System uptime unavailable")
    status = "degraded" if any(item["status"] == "critical" for item in alerts) else "warning" if alerts else "healthy"
    return status, alerts


class SystemStatusMonitor:
    """One instance per API process. Locks prevent concurrent cache stampedes.

    CPU utilization uses differences between psutil CPU counters across cached
    samples. Unlike cpu_percent(None), this baseline is independent of which
    FastAPI worker thread handles the request. No sampling sleep is required.
    """

    def __init__(self, db_config, mqtt_host="localhost", mqtt_port=1883, zigbee_device=None):
        self._db_config = dict(db_config)
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.zigbee_device = zigbee_device
        self._cache = {}
        self._locks = {key: threading.Lock() for key in CACHE_TTLS}
        self._unexpected_failures = set()
        self._previous_cpu = None
        self._started_at = time.monotonic()

    def _cached(self, key, probe, fallback):
        with self._locks[key]:
            cached = self._cache.get(key)
            if cached is not None and time.monotonic() - cached[0] < CACHE_TTLS[key]:
                return copy.deepcopy(cached[1])
            try:
                value = probe()
                self._unexpected_failures.discard(key)
            except (OSError, psutil.Error):
                value = copy.deepcopy(fallback)
            except Exception:
                # Log once per failure episode, never put exception text in JSON.
                if key not in self._unexpected_failures:
                    LOGGER.exception("Unexpected system monitoring failure in %s", key)
                    self._unexpected_failures.add(key)
                value = copy.deepcopy(fallback)
            self._cache[key] = (time.monotonic(), value)
            return copy.deepcopy(value)

    def _cpu_usage(self):
        sample = psutil.cpu_times()._asdict()
        previous, self._previous_cpu = self._previous_cpu, sample
        if previous is None:
            return None  # The next cached sample has a real measurement interval.
        deltas = {key: max(0, value - previous.get(key, value)) for key, value in sample.items()}
        # On Linux guest times are already included in user/nice, and iowait is idle.
        total = sum(deltas.values()) - deltas.get("guest", 0) - deltas.get("guest_nice", 0)
        idle = deltas.get("idle", 0) + deltas.get("iowait", 0)
        return round(min(100, max(0, (total - idle) / total * 100)), 1) if total > 0 else None

    def _memory(self):
        memory = psutil.virtual_memory()
        return {"used_gb": round((memory.total - memory.available) / GB, 1),
                "total_gb": round(memory.total / GB, 1), "usage_percent": round(memory.percent, 1)}

    def _disk(self):
        disk = psutil.disk_usage("/")
        return {"used_gb": round(disk.used / GB, 1), "total_gb": round(disk.total / GB, 1),
                "usage_percent": round(disk.percent, 1)}

    def _host(self):
        return {"hostname": socket.gethostname(), "boot_time": psutil.boot_time()}

    def _mqtt(self):
        return {"status": "online" if tcp_reachable(self.mqtt_host, self.mqtt_port) else "offline",
                "host": self.mqtt_host, "port": self.mqtt_port}

    def _database(self):
        result = {"status": "error", "query_ok": False, "last_seen": None, "telemetry_query_ok": False}
        config = dict(self._db_config)
        config["connect_timeout"] = DB_CONNECT_TIMEOUT_SECONDS
        config["options"] = f"{config.get('options', '')} -c statement_timeout={DB_STATEMENT_TIMEOUT_MS}".strip()
        try:
            # psycopg2's transaction context alone does not close the connection.
            with closing(psycopg2.connect(**config)) as connection:
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    if cursor.fetchone() != (1,):
                        return result
                    result.update(status="connected", query_ok=True)
                    cursor.execute("SELECT recorded_at FROM sensor_readings ORDER BY recorded_at DESC LIMIT 1")
                    row = cursor.fetchone()
                    result.update(last_seen=row[0] if row else None, telemetry_query_ok=True)
        except psycopg2.Error:
            pass  # An unavailable dependency is health data, not an application error.
        return result

    def _internet(self):
        return {"status": "online" if tcp_reachable(*INTERNET_TARGET) else "offline"}

    def _zigbee(self):
        configured = bool(self.zigbee_device)
        present = Path(self.zigbee_device).exists() if configured else False
        return {"configured": configured, "device_present": present,
                "status": "device_present" if present else "offline" if configured else "not_configured"}

    def get_system_status(self):
        usage = self._cached("cpu", self._cpu_usage, None)
        temperature = self._cached("temperature", read_cpu_temperature, None)
        memory = self._cached("memory", self._memory, {"used_gb": None, "total_gb": None, "usage_percent": None})
        disk = self._cached("disk", self._disk, {"used_gb": None, "total_gb": None, "usage_percent": None})
        host = self._cached("host", self._host, {"hostname": None, "boot_time": None})
        mqtt = self._cached("mqtt", self._mqtt, {"status": "unknown"})
        database = self._cached("database", self._database,
                                {"status": "error", "query_ok": False, "last_seen": None, "telemetry_query_ok": False})
        internet = self._cached("internet", self._internet, {"status": "unknown"})
        zigbee = self._cached("zigbee", self._zigbee,
                              {"configured": bool(self.zigbee_device), "device_present": False, "status": "unknown"})
        now = datetime.now(timezone.utc)
        uptime = max(0, int(now.timestamp() - host["boot_time"])) if host["boot_time"] is not None else None
        result = {
            "generated_at": now.isoformat(),
            "host": {"hostname": host["hostname"], "platform": "Raspberry Pi", "uptime_seconds": uptime, "uptime": format_uptime(uptime)},
            "cpu": {"usage_percent": usage, "temperature_c": temperature},
            "memory": memory, "disk": disk,
            "services": {
                "mqtt": mqtt,
                "database": {"status": database["status"], "query_ok": database["query_ok"]},
                "internet": internet,
                "esp32": esp32_status(database["last_seen"], now, database["telemetry_query_ok"]),
                "api": {"status": "running", "uptime_seconds": int(time.monotonic() - self._started_at)},
                "zigbee": zigbee,
            },
            "thresholds": dict(SYSTEM_THRESHOLDS),
        }
        result["overall_status"], result["alerts"] = aggregate_status(result)
        return result
