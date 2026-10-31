"""Persist confirmed CSI transitions separately from Zigbee events."""
import logging
import queue
import threading
from contextlib import closing

import psycopg2
from psycopg2.extras import RealDictCursor


LOGGER = logging.getLogger(__name__)
DB_CONNECT_TIMEOUT_SECONDS = 1
DB_STATEMENT_TIMEOUT_MS = 750


def connect(db_config):
    config = dict(db_config, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)
    config['options'] = f"{config.get('options', '')} -c statement_timeout={DB_STATEMENT_TIMEOUT_MS}".strip()
    return psycopg2.connect(**config)


def initialize_schema(db_config):
    with closing(connect(db_config)) as conn, conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS csi_motion_events (
                id UUID PRIMARY KEY,
                device_id TEXT NOT NULL,
                room TEXT NOT NULL,
                motion BOOLEAN NOT NULL,
                score DOUBLE PRECISION NOT NULL,
                threshold_on DOUBLE PRECISION NOT NULL,
                threshold_off DOUBLE PRECISION NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX IF NOT EXISTS csi_motion_events_device_time_idx
                ON csi_motion_events (device_id, recorded_at DESC, id DESC);
        """)


class CsiEventStore:
    def __init__(self, db_config, room="Room 1"):
        self.db_config = dict(db_config)
        self.room = room
        self._queue = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._thread = None
        self._write_failed = self._read_failed = self._full = False

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="csi-history", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        # A daemon thread blocked by a DB outage must not prevent API shutdown.

    def submit(self, event):
        try:
            self._queue.put_nowait({**event, "room": self.room})
            self._full = False
        except queue.Full:
            if not self._full:
                LOGGER.warning("CSI history queue full; live monitoring continues")
            self._full = True

    def _write(self, event):
        with closing(connect(self.db_config)) as conn, conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO csi_motion_events
                    (id, device_id, room, motion, score, threshold_on, threshold_off, recorded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, tuple(event[key] for key in
                       ("id", "device_id", "room", "motion", "score", "threshold_on", "threshold_off", "recorded_at")))

    def _run(self):
        event = None
        while not self._stop.is_set() or event is not None or not self._queue.empty():
            if event is None:
                try:
                    event = self._queue.get(timeout=.2)
                except queue.Empty:
                    continue
            try:
                # Retrying the same UUID also covers uncertain commits safely.
                self._write(event)
                self._write_failed = False
            except Exception:
                if not self._write_failed:
                    LOGGER.warning("CSI history write unavailable; retrying")
                self._write_failed = True
                if self._stop.wait(1):
                    return
                continue
            self._queue.task_done()
            event = None

    def read(self, device_id, hours=24, limit=20):
        if type(hours) is not int or not 1 <= hours <= 168 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid CSI history window or limit")
        try:
            with closing(connect(self.db_config)) as conn, conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, device_id, room, motion, score, threshold_on, threshold_off, recorded_at
                    FROM csi_motion_events
                    WHERE device_id = %s AND recorded_at >= NOW() - (%s * INTERVAL '1 hour')
                    ORDER BY recorded_at DESC, id DESC LIMIT %s
                """, (device_id, hours, limit))
                events = [dict(row) for row in cur.fetchall()]
                cur.execute("""
                    SELECT recorded_at FROM csi_motion_events
                    WHERE device_id = %s AND motion = TRUE
                    ORDER BY recorded_at DESC, id DESC LIMIT 1
                """, (device_id,))
                last_motion = cur.fetchone()
            for event in events:
                event["id"] = str(event["id"])
                event["recorded_at"] = event["recorded_at"].isoformat()
            self._read_failed = False
            return {"events": events, "history_available": not self._write_failed and not self._full,
                    "history_last_motion_at": last_motion["recorded_at"].isoformat() if last_motion else None,
                    "hours": hours, "limit": limit}
        except psycopg2.Error:
            if not self._read_failed:
                LOGGER.warning("CSI history read unavailable")
            self._read_failed = True
            return {"events": [], "history_available": False, "history_last_motion_at": None,
                    "hours": hours, "limit": limit}
