"""Live ESP32 CSI report and confirmed transition notifications."""
import copy
import json
import logging
import math
import threading
import time
import uuid
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

LOGGER = logging.getLogger(__name__)
CSI_STALE_SECONDS = 5
MAX_PAYLOAD_BYTES = 2048
STATES = {"settling", "calibrating", "ready", "no_data"}
CSI_FILTER = {"score_multiplier": 1.25, "confirm_reports": 3,
              "clear_reports": 3, "max_gap_seconds": 2.5}


def parse_report(payload, device_id):
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("CSI payload too large")
    def reject_constant(value):
        raise ValueError("Non-finite CSI value")
    data = json.loads(payload, parse_constant=reject_constant)
    if not isinstance(data, dict) or data.get("device_id") != device_id or type(data.get("state")) is not str or data.get("state") not in STATES:
        raise ValueError("Invalid CSI source or state")
    if "motion" not in data or (data["state"] == "ready" and type(data["motion"]) is not bool) or (data["state"] != "ready" and data["motion"] is not None):
        raise ValueError("Invalid CSI motion state")
    result = {"state": data["state"], "motion": data["motion"]}
    for key in ("score", "threshold_on", "threshold_off"):
        value = data.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError("Invalid CSI score or threshold")
        if data["state"] == "ready" and value is None:
            raise ValueError("Missing ready-state score or threshold")
        result[key] = value
    if data["state"] == "ready" and result["threshold_off"] > result["threshold_on"]:
        raise ValueError("Invalid CSI hysteresis")
    for key in ("calibration", "samples", "accepted", "rejected", "dropped", "ping_replies", "uptime_ms"):
        value = data.get(key)
        if type(value) is not int or not 0 <= value <= 0xffffffff:
            raise ValueError("Invalid CSI counter")
        result[key] = value
    rssi = data.get("rssi")
    if type(rssi) not in (int, float) or not math.isfinite(rssi) or not -127 <= rssi <= 0:
        raise ValueError("Invalid CSI RSSI")
    result["rssi"] = rssi
    return result


class CsiMotionFilter:
    """Confirm sustained score activity, not the firmware's latched motion flag."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.motion = False
        self.transition = None
        self.pending = self.quiet = 0
        self.last_received = self.last_uptime = self.thresholds = None

    def update(self, report, now):
        self.transition = None
        if report["state"] != "ready":
            self.reset()
            return True
        uptime = report["uptime_ms"]
        if self.last_uptime == uptime:
            return False  # A repeated report cannot confirm motion or refresh its age.
        thresholds = (report["threshold_on"], report["threshold_off"])
        if self.last_received is not None and (
                now - self.last_received > CSI_FILTER["max_gap_seconds"] or
                uptime < self.last_uptime or
                uptime - self.last_uptime > CSI_FILTER["max_gap_seconds"] * 1000 or
                thresholds != self.thresholds):
            self.reset()
        self.last_received, self.last_uptime, self.thresholds = now, uptime, thresholds
        high = report["motion"] and report["score"] >= thresholds[0] * CSI_FILTER["score_multiplier"]
        low = not report["motion"] or report["score"] < thresholds[1] * CSI_FILTER["score_multiplier"]
        if not self.motion:
            self.pending = self.pending + 1 if high else 0
            if self.pending >= CSI_FILTER["confirm_reports"]:
                self.motion = True
                self.transition = "detected"
                self.pending = self.quiet = 0
        else:
            self.quiet = self.quiet + 1 if low else 0
            if self.quiet >= CSI_FILTER["clear_reports"]:
                self.motion = False
                self.transition = "cleared"
                self.pending = self.quiet = 0
        return True


class CsiMonitor:
    def __init__(self, host="localhost", port=1883, topic="home/motion/room1/csi", device_id="esp32-room1", on_event=None):
        if not topic or any(char in topic for char in ("+", "#", "\x00")):
            raise ValueError("CSI topic must be exact")
        self.host, self.port, self.topic, self.device_id = host, port, topic, device_id
        self._lock = threading.Lock()
        self._client = None
        self._connected = False
        self._report = None
        self._received = None
        self._last_seen = None
        self._last_motion = None
        self._invalid_logged = False
        self._filter = CsiMotionFilter()
        self.on_event = on_event

    def start(self):
        if self._client is not None:
            return
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="smarthome-csi-" + uuid.uuid4().hex[:12])
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(self.host, self.port, keepalive=15)
        client.loop_start()
        self._client = client

    def stop(self):
        client, self._client = self._client, None
        if client is not None:
            client.disconnect()
            client.loop_stop()
        with self._lock:
            self._connected = False
            self._filter.reset()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            self._connected = reason_code == 0
            # A reconnect requires a new live report, not a previous connection's data.
            self._report = None
            self._received = None
            self._filter.reset()
        if reason_code == 0:
            client.subscribe(self.topic, qos=0)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            self._connected = False
            self._filter.reset()

    def _on_message(self, client, userdata, message):
        # Retained messages have no source timestamp and cannot prove freshness.
        if message.topic != self.topic or message.retain:
            return
        try:
            report = parse_report(message.payload, self.device_id)
        except (ValueError, UnicodeDecodeError, TypeError, RecursionError):
            if not self._invalid_logged:
                LOGGER.warning("Invalid CSI report ignored")
                self._invalid_logged = True
            return
        self._invalid_logged = False
        event = None
        with self._lock:
            if not self._connected:
                return
            now = time.monotonic()
            if not self._filter.update(report, now):
                return
            seen = datetime.now(timezone.utc).isoformat()
            if self._filter.transition == "detected":
                self._last_motion = seen
            if self._filter.transition:
                event = {"id": str(uuid.uuid4()), "device_id": self.device_id,
                         "motion": self._filter.transition == "detected", "score": report["score"],
                         "threshold_on": report["threshold_on"] * CSI_FILTER["score_multiplier"],
                         "threshold_off": report["threshold_off"] * CSI_FILTER["score_multiplier"],
                         "recorded_at": seen}
            self._report = report
            self._received = now
            self._last_seen = seen
        if event is not None and self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                LOGGER.warning("CSI event sink unavailable; live monitoring continues")

    def snapshot(self):
        with self._lock:
            report = copy.deepcopy(self._report)
            age = max(0, time.monotonic() - self._received) if self._received is not None else None
            connected, seen, last_motion = self._connected, self._last_seen, self._last_motion
            filtered_motion, pending = self._filter.motion, self._filter.pending
        fresh = connected and report is not None and age < CSI_STALE_SECONDS
        status = "mqtt_offline" if not connected else "waiting" if report is None else "live" if fresh else "stale"
        result = {"device_id": self.device_id, "status": status, "mqtt_connected": connected,
                  "state": report["state"] if fresh else "unknown",
                  "motion": filtered_motion if fresh and report["state"] == "ready" else None,
                  "raw_motion": report["motion"] if fresh else None,
                  "last_seen": seen, "last_motion_at": last_motion,
                  "age_seconds": round(age, 3) if age is not None else None,
                  "fresh_for_seconds": max(0, CSI_STALE_SECONDS - age) if fresh else 0,
                  "generated_at": datetime.now(timezone.utc).isoformat()}
        ready = fresh and report["state"] == "ready"
        result["filter"] = {
            "score_multiplier": CSI_FILTER["score_multiplier"],
            "confirm_reports": CSI_FILTER["confirm_reports"],
            "clear_reports": CSI_FILTER["clear_reports"],
            "pending_reports": pending if ready else 0,
            "threshold_on": report["threshold_on"] * CSI_FILTER["score_multiplier"] if ready else None,
            "threshold_off": report["threshold_off"] * CSI_FILTER["score_multiplier"] if ready else None,
        }
        for key in ("score", "threshold_on", "threshold_off", "calibration", "samples", "accepted", "rejected", "dropped", "ping_replies", "rssi", "uptime_ms"):
            value = report.get(key) if fresh else None
            if key.startswith("threshold_") and result["state"] != "ready":
                value = None
            result[key] = value
        return result
