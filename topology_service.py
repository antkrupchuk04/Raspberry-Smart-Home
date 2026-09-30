"""Read-only topology assembled from existing telemetry and health probes."""
import copy
import logging
import math
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

from system_status import (DB_CONNECT_TIMEOUT_SECONDS, DB_STATEMENT_TIMEOUT_MS,
                           SYSTEM_THRESHOLDS, esp32_status)

LOGGER = logging.getLogger(__name__)
REFRESH_SECONDS = 10
DATA_CACHE_SECONDS = 5
CLIMATE_DEVICES = ({"id": "esp32-room1", "name": "ESP32 Room 1", "room": "Room 1"},)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) and value.tzinfo else None


def metric(key, label, value, unit="", precision=0, kind="number", state=None):
    if kind == "number" and (type(value) not in (int, float) or not math.isfinite(value)):
        value = None
    result = {"key": key, "label": label, "value": value, "unit": unit, "precision": precision, "kind": kind}
    if state:
        result["state"] = state
    return result


def text_metric(key, label, value):
    return metric(key, label, value, kind="text")


def timestamp_metric(key, label, value):
    return metric(key, label, stamp(value), kind="timestamp")


def hub_node(system):
    system = system or {}
    host, services = system.get("host", {}), system.get("services", {})
    status = {"healthy": "online", "warning": "warning", "degraded": "degraded"}.get(system.get("overall_status"), "unknown")
    details = [text_metric("hostname", "Hostname", host.get("hostname")),
               text_metric("uptime", "Uptime", host.get("uptime"))]
    labels = {"online": "Online", "offline": "Offline", "connected": "Connected", "running": "Running",
              "error": "Error", "unknown": "Unknown", "device_present": "Device present", "not_configured": "Not configured"}
    for key, label in (("api", "FastAPI"), ("mqtt", "MQTT"), ("database", "PostgreSQL"), ("internet", "Internet"), ("zigbee", "USB coordinator")):
        details.append(text_metric(key, label, labels.get(services.get(key, {}).get("status"), "Unknown")))
    details.append(text_metric("zigbee2mqtt", "Zigbee2MQTT health", "Not monitored"))
    return {"id": "raspberry", "type": "hub", "name": "Raspberry Pi 5", "room": host.get("hostname"),
            "status": status, "status_label": "Online" if status == "online" else status.capitalize(),
            "data_status": "available" if system else "unavailable", "device_id": host.get("hostname"),
            "metrics": [metric("cpu", "CPU", system.get("cpu", {}).get("usage_percent"), "%"),
                        metric("memory", "RAM", system.get("memory", {}).get("usage_percent"), "%"),
                        metric("disk", "Disk", system.get("disk", {}).get("usage_percent"), "%"),
                        metric("temperature", "CPU temp", system.get("cpu", {}).get("temperature_c"), "°C", 1)],
            "details": details, "note": "USB presence does not verify Zigbee2MQTT or device availability."}


def climate_node(device, reading, query_ok, now):
    reading = reading or {}
    health = esp32_status(reading.get("recorded_at"), now, query_ok)
    remaining = max(0, SYSTEM_THRESHOLDS["esp32_timeout_seconds"] - health["age_seconds"]) if health["status"] == "online" else None
    return {"id": device["id"], "type": "esp32", "name": device["name"], "room": device.get("room"),
            "device_id": device["id"], "status": health["status"], "status_label": health["status"].capitalize(),
            "data_status": "unavailable" if not query_ok else "reported" if reading else "waiting",
            "last_seen": health["last_seen"], "age_seconds": health["age_seconds"], "online_for_seconds": remaining,
            "metrics": [metric("temperature", "Temperature", reading.get("temperature"), "°C", 1),
                        metric("humidity", "Humidity", reading.get("humidity"), "%", 1),
                        metric("pressure", "Pressure", reading.get("pressure"), "hPa", 1)],
            "details": [text_metric("sensors", "Sensors", "BMP280 · DHT11"),
                        timestamp_metric("recorded_at", "Last reading", reading.get("recorded_at"))],
            "note": "Status follows stored telemetry freshness; ESP32 RSSI is not reported."}


def motion_node(device, reading, query_ok, now):
    config = device["config"]
    reading = reading or {}
    occupancy = reading.get("occupancy")
    state = "detected" if occupancy is True else "clear" if occupancy is False else "unknown"
    seen = reading.get("last_seen")
    age = (now - seen).total_seconds() if stamp(seen) else None
    return {"id": device["id"], "type": "zigbee_motion", "name": "Motion Sensor", "room": config.room,
            "device_id": config.device_id, "status": "unknown", "status_label": "Availability unknown",
            "data_status": "unavailable" if not query_ok else "reported" if reading else "waiting",
            "last_seen": stamp(seen), "age_seconds": round(age, 1) if age is not None and age >= 0 else None,
            "activity": state, "activity_at": stamp(reading.get("state_reported_at")),
            "metrics": [metric("occupancy", "Last reported PIR state", {"detected": "Motion detected", "clear": "Clear / no motion", "unknown": "No state reported"}[state],
                               kind="text", state="warning" if state == "detected" else "online" if state == "clear" else "unknown"),
                        metric("battery", "Battery", reading.get("battery"), "%", state="warning" if reading.get("battery_low") is True else None),
                        metric("lqi", "LQI", reading.get("linkquality"), "/ 255")],
            "details": [text_metric("model", "Model", config.name),
                        text_metric("battery_low", "Battery low", "Yes" if reading.get("battery_low") is True else "No" if reading.get("battery_low") is False else None),
                        metric("voltage", "Voltage", reading.get("voltage"), "mV"),
                        timestamp_metric("last_seen", "Last report", seen),
                        timestamp_metric("state_reported_at", "State reported", reading.get("state_reported_at")),
                        timestamp_metric("last_motion_at", "Last motion", reading.get("last_motion_at"))],
            "note": "Last reported motion, not continuous presence. Quiet periods do not prove the sensor is offline."}


def connection(node, system, transport):
    services = (system or {}).get("services", {})
    mqtt = services.get("mqtt", {}).get("status")
    zigbee = services.get("zigbee", {})
    status = "unknown"
    if mqtt == "offline" or (transport == "zigbee" and zigbee.get("status") == "offline"):
        status = "offline"
    elif transport == "wifi":
        if node["status"] == "offline":
            status = "stale"
        elif node["status"] == "online" and mqtt == "online":
            status = "online"
    return {"id": f'{node["id"]}:raspberry', "source": node["id"], "target": "raspberry",
            "protocol": "mqtt" if transport == "wifi" else "zigbee", "transport": transport,
            "label": "Wi-Fi / MQTT" if transport == "wifi" else "Zigbee", "status": status,
            "note": "Recent stored telemetry and broker reachability; no direct Wi-Fi probe." if transport == "wifi" else
                    "Via USB coordinator → Zigbee2MQTT → MQTT. Live Zigbee availability is not monitored."}


class TopologyService:
    """Small deployment registry; renderers consume arrays rather than fixed IDs."""
    def __init__(self, db_config, system_monitor, motion_devices=(), climate_devices=CLIMATE_DEVICES):
        self.db_config = dict(db_config)
        self.system_monitor = system_monitor
        self.climate_devices = tuple(climate_devices)
        self.motion_devices = tuple(device for device in motion_devices if device["config"].configured)
        ids = ["raspberry", *(device["id"] for device in self.climate_devices), *(device["id"] for device in self.motion_devices)]
        if len(ids) != len(set(ids)):
            raise ValueError("Topology node IDs must be unique")
        self._cache = {}
        self._lock = threading.Lock()
        self._failed = set()

    def _query(self, sql, ids):
        if not ids:
            return {}
        config = dict(self.db_config, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)
        config["options"] = f"{config.get('options', '')} -c statement_timeout={DB_STATEMENT_TIMEOUT_MS}".strip()
        with closing(psycopg2.connect(**config)) as conn:
            conn.autocommit = True
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, (list(ids),))
                return {row["device_id"]: dict(row) for row in cursor.fetchall()}

    def _read(self, key, loader, cached=True):
        with self._lock:
            entry = self._cache.get(key)
            if cached and entry and time.monotonic() - entry[0] < DATA_CACHE_SECONDS:
                return copy.deepcopy(entry[1]), entry[2]
            try:
                data, ok = loader(), True
                self._failed.discard(key)
            except Exception:
                data, ok = {}, False
                if key not in self._failed:
                    LOGGER.warning("Topology %s data unavailable", key)
                    self._failed.add(key)
            if cached:
                self._cache[key] = (time.monotonic(), data, ok)
            return copy.deepcopy(data), ok

    def get_topology(self):
        system, system_ok = self._read("system", self.system_monitor.get_system_status, cached=False)
        climate, climate_ok = self._read("climate", lambda: self._query("""
            SELECT DISTINCT ON (device_id) device_id, temperature, humidity, pressure, recorded_at
            FROM sensor_readings WHERE device_id = ANY(%s)
            ORDER BY device_id, recorded_at DESC, id DESC
        """, [device["id"] for device in self.climate_devices]))
        motion, motion_ok = self._read("motion", lambda: self._query("""
            SELECT device_id, occupancy, battery, battery_low, voltage, linkquality,
                   last_seen, state_reported_at, last_motion_at
            FROM motion_sensor_state WHERE device_id = ANY(%s)
        """, [device["config"].device_id for device in self.motion_devices]))
        now = datetime.now(timezone.utc)
        nodes, links = [hub_node(system)], []
        for device in self.climate_devices:
            node = climate_node(device, climate.get(device["id"]), climate_ok, now)
            nodes.append(node)
            links.append(connection(node, system, "wifi"))
        for device in self.motion_devices:
            node = motion_node(device, motion.get(device["config"].device_id), motion_ok, now)
            nodes.append(node)
            links.append(connection(node, system, "zigbee"))
        sources = {"system": "ok" if system_ok else "unavailable", "climate": "ok" if climate_ok else "unavailable",
                   "motion": "ok" if motion_ok else "unavailable"}
        return {"generated_at": now.isoformat(), "refresh_seconds": REFRESH_SECONDS,
                "system": {"status": system.get("overall_status", "unknown")},
                "status": "ok" if all(value == "ok" for value in sources.values()) else "partial",
                "sources": sources, "nodes": nodes, "links": links}
