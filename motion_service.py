"""Persist real Zigbee PIR reports and expose a bounded activity history."""
import json
import logging
import math
import os
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

MAX_PAYLOAD_BYTES = 8192
DB_TIMEOUT_MS = 2000
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MotionConfig:
    topic: str = ""
    device_id: str = ""
    name: str = "Motion sensor"
    room: str = "Room 1"

    @property
    def configured(self):
        return bool(self.topic and self.device_id)

    @classmethod
    def from_env(cls):
        topic = os.getenv("MOTION_SENSOR_TOPIC", "").strip()
        if topic and ("+" in topic or "#" in topic or "\x00" in topic):
            raise ValueError("MOTION_SENSOR_TOPIC must be one exact MQTT topic")
        return cls(topic, os.getenv("MOTION_SENSOR_ID", "").strip() or topic.rsplit("/", 1)[-1],
                   os.getenv("MOTION_SENSOR_NAME", "Motion sensor"),
                   os.getenv("MOTION_SENSOR_ROOM", "Room 1"))


def parse_motion_report(payload):
    """Ignore unrelated fields; reject malformed reports without inventing values."""
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("Motion payload is too large")
    def reject_constant(_):
        raise ValueError("Non-finite JSON number")
    data = json.loads(payload, parse_constant=reject_constant)
    if not isinstance(data, dict):
        raise ValueError("Motion payload must be an object")
    report = {}
    for key in ("occupancy", "battery_low", "tamper"):
        if key in data and data[key] is not None:
            if type(data[key]) is not bool:
                raise ValueError(f"Invalid {key}")
            report[key] = data[key]
    for key, upper in (("battery", 100), ("voltage", 10000), ("linkquality", 255)):
        value = data.get(key)
        if value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= upper:
            raise ValueError(f"Invalid {key}")
        report[key] = value
    if not report:
        raise ValueError("No motion sensor fields")
    return report


def connect(db_config):
    config = dict(db_config, connect_timeout=2)
    config["options"] = f"{config.get('options', '')} -c statement_timeout={DB_TIMEOUT_MS}".strip()
    return psycopg2.connect(**config)


def initialize_schema(db_config):
    """Explicit, additive migration. Does not touch climate tables or existing rows."""
    with closing(connect(db_config)) as conn, conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS motion_sensor_state (
                device_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                room TEXT NOT NULL,
                source_topic TEXT NOT NULL,
                occupancy BOOLEAN,
                battery REAL,
                battery_low BOOLEAN,
                voltage REAL,
                tamper BOOLEAN,
                linkquality REAL,
                last_seen TIMESTAMPTZ,
                state_reported_at TIMESTAMPTZ,
                last_motion_at TIMESTAMPTZ
            );
            CREATE TABLE IF NOT EXISTS motion_events (
                id BIGSERIAL PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES motion_sensor_state(device_id),
                room TEXT NOT NULL,
                occupancy BOOLEAN NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS motion_events_device_time_idx
                ON motion_events (device_id, recorded_at DESC, id DESC);
        """)


def store_report(db_config, config, report, received_at=None):
    """The row lock makes state-change detection atomic and restart-safe."""
    received_at = received_at or datetime.now(timezone.utc)
    with closing(connect(db_config)) as conn, conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO motion_sensor_state (device_id, name, room, source_topic)
            VALUES (%s, %s, %s, %s) ON CONFLICT (device_id) DO NOTHING
        """, (config.device_id, config.name, config.room, config.topic))
        cur.execute("SELECT occupancy FROM motion_sensor_state WHERE device_id = %s FOR UPDATE",
                    (config.device_id,))
        previous = cur.fetchone()[0]
        changed = "occupancy" in report and previous is not report["occupancy"]
        # COALESCE preserves existing fields for partial reports, including valid 0/false.
        cur.execute("""
            UPDATE motion_sensor_state SET name = %s, room = %s, source_topic = %s,
                occupancy = COALESCE(%s, occupancy), battery = COALESCE(%s, battery),
                battery_low = COALESCE(%s, battery_low), voltage = COALESCE(%s, voltage),
                tamper = COALESCE(%s, tamper), linkquality = COALESCE(%s, linkquality),
                last_seen = %s, state_reported_at = COALESCE(%s, state_reported_at),
                last_motion_at = COALESCE(%s, last_motion_at)
            WHERE device_id = %s
        """, (config.name, config.room, config.topic,
              report.get("occupancy"), report.get("battery"), report.get("battery_low"),
              report.get("voltage"), report.get("tamper"), report.get("linkquality"), received_at,
              received_at if "occupancy" in report else None,
              received_at if changed and report["occupancy"] else None, config.device_id))
        if changed:
            cur.execute("""
                INSERT INTO motion_events (device_id, room, occupancy, recorded_at)
                VALUES (%s, %s, %s, %s)
            """, (config.device_id, config.room, report["occupancy"], received_at))
    return changed


def collect_motion_message(db_config, config, message, on_motion=None):
    if not config.configured or message.topic != config.topic:
        return False
    # A retained snapshot is not a new detection and has no trustworthy arrival time.
    if message.retain:
        return False
    report = parse_motion_report(message.payload)
    received_at = datetime.now(timezone.utc)
    changed = store_report(db_config, config, report, received_at)
    if changed and type(report.get("occupancy")) is bool and on_motion is not None:
        try:
            # Storage has committed before any notification is queued.
            on_motion(config, report, received_at)
        except Exception:
            LOGGER.error("Motion notification could not be queued; reading remains stored")
    return changed


def get_motion_status(db_config, config, hours=24, limit=20):
    result = {
        "configured": config.configured,
        "device": {"id": config.device_id, "name": config.name, "room": config.room},
        "sensor": None, "events": [], "today_count": None,
        "day_timezone": None, "hours": hours,
        "generated_at": datetime.now(timezone.utc),
    }
    if not config.configured:
        return result
    with closing(connect(db_config)) as conn, conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        # One snapshot keeps counters, latest state and timeline consistent during writes.
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute("SELECT * FROM motion_sensor_state WHERE device_id = %s", (config.device_id,))
        result["sensor"] = cur.fetchone()
        cur.execute("""
            SELECT COUNT(*) AS count, current_setting('TIMEZONE') AS timezone
            FROM motion_events WHERE device_id = %s AND occupancy = TRUE
              AND recorded_at >= date_trunc('day', NOW())
        """, (config.device_id,))
        summary = cur.fetchone()
        result.update(today_count=summary["count"], day_timezone=summary["timezone"])
        cur.execute("""
            SELECT id, device_id, room, occupancy, recorded_at FROM motion_events
            WHERE device_id = %s AND recorded_at >= NOW() - (%s * INTERVAL '1 hour')
            ORDER BY recorded_at DESC, id DESC LIMIT %s
        """, (config.device_id, hours, limit))
        result["events"] = cur.fetchall()
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Initialize the additive motion schema")
    parser.add_argument("--init-db", action="store_true", required=True)
    parser.parse_args()
    initialize_schema({"host": os.getenv("DB_HOST", "localhost"),
                       "port": os.getenv("DB_PORT", "5432"), "database": os.environ["DB_NAME"],
                       "user": os.environ["DB_USER"], "password": os.environ["DB_PASSWORD"]})
    print("Motion tables and index ready; existing readings preserved.")
