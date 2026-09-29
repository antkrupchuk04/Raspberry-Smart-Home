"""Optional Telegram alerts; network calls run outside the MQTT callback."""
import hashlib
from html import escape
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
import urllib.error
import urllib.request

LOGGER = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 5
MAX_EVENT_AGE_SECONDS = 120
QUEUE_SIZE = 8


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    token: str = field(default="", repr=False)
    chat_id: str = field(default="", repr=False)
    cooldown_seconds: int = 60
    state_path: Path = field(default_factory=lambda: Path.home() / ".local/state/smarthome/telegram-notifications.json")
    sensor_label: str = ""
    location_label: str = ""

    @classmethod
    def from_env(cls):
        try:
            cooldown = int(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "60"))
            if not 1 <= cooldown <= 86400:
                raise ValueError
        except ValueError:
            LOGGER.warning("Invalid Telegram cooldown; using 60 seconds")
            cooldown = 60
        return cls(os.getenv("TELEGRAM_ENABLED", "false").lower() in ("true", "1", "yes"),
                   os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip(),
                   cooldown, Path(os.getenv("TELEGRAM_STATE_FILE", str(cls().state_path))),
                   os.getenv("TELEGRAM_SENSOR_LABEL", "").strip(),
                   os.getenv("TELEGRAM_LOCATION_LABEL", "").strip())

    @property
    def ready(self):
        return bool(self.enabled and re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self.token)
                    and re.fullmatch(r"-?[0-9]+", self.chat_id))


def format_motion_message(config, report, received_at, *, sensor_label="", location_label=""):
    detected = report.get("occupancy") is True
    status = "Motion detected" if detected else "Motion cleared"
    icon = "🚨" if detected else "✅"
    location = escape((location_label or config.room)[:200])
    sensor = escape((sensor_label or config.name)[:200])
    local_time = received_at.astimezone().strftime("%d.%m.%Y, %H:%M")
    return (f"{icon} <b>{status}</b>\n\n"
            f"📍 Location: {location}\n"
            f"📡 Sensor: {sensor}\n"
            f"🕒 Time: {local_time}\n"
            f"⚡ Status: {status}")


class TelegramNotifier:
    def __init__(self, config):
        self.config = config
        self._queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._queued_at = {}
        self._attempted_at = {}
        self._open_alerts = {}
        self._scheduled_state = {}
        self._retry_until = 0
        self._scope = hashlib.sha256((config.token + ":" + config.chat_id).encode()).hexdigest()
        if config.ready:
            self._load_state()

    def _load_state(self):
        try:
            data = json.loads(self.config.state_path.read_text())
            if data.get("scope") != self._scope:
                return
            attempts = data.get("attempted_at", {})
            if not isinstance(attempts, dict):
                raise ValueError
            self._attempted_at = {str(key): float(value) for key, value in attempts.items()
                                  if type(value) in (float, int) and math.isfinite(value)}
            retry = data.get("retry_until", 0)
            self._retry_until = float(retry) if type(retry) in (float, int) and math.isfinite(retry) else 0
            open_alerts = data.get("open_alerts", {})
            if not isinstance(open_alerts, dict):
                raise ValueError
            self._open_alerts = {str(key): True for key, value in open_alerts.items() if value is True}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            LOGGER.warning("Telegram cooldown state unavailable; starting with no prior attempts")

    def _save_state(self):
        path = self.config.state_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".telegram-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump({"scope": self._scope, "attempted_at": self._attempted_at,
                           "retry_until": self._retry_until, "open_alerts": self._open_alerts}, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def start(self):
        if not self.config.ready:
            LOGGER.info("Telegram notifications disabled or missing valid configuration")
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="telegram-notifications", daemon=True)
        self._thread.start()
        LOGGER.info("Telegram motion notifications enabled; cooldown=%ss", self.config.cooldown_seconds)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=REQUEST_TIMEOUT_SECONDS + 1)

    def submit_motion(self, motion_config, report, received_at):
        occupancy = report.get("occupancy")
        if not self.config.ready or self._stop.is_set() or type(occupancy) is not bool:
            return False
        now = time.time()
        age = now - received_at.timestamp()
        if age < -5 or age > MAX_EVENT_AGE_SECONDS:
            return False
        device = motion_config.device_id
        with self._lock:
            previous = max(self._queued_at.get(device, 0), self._attempted_at.get(device, 0))
            if now < self._retry_until:
                return False
            if occupancy:
                if now - previous < self.config.cooldown_seconds:
                    return False
            elif not self._scheduled_state.get(device, self._open_alerts.get(device, False)):
                return False
            message = format_motion_message(motion_config, report, received_at,
                                            sensor_label=self.config.sensor_label,
                                            location_label=self.config.location_label)
            try:
                self._queue.put_nowait((device, message, received_at, occupancy))
            except queue.Full:
                LOGGER.warning("Telegram notification queue full; alert skipped")
                return False
            if occupancy:
                self._queued_at[device] = now
            self._scheduled_state[device] = occupancy
        return True

    def _deliver(self, device, message, received_at, occupancy=True):
        now = time.time()
        if now - received_at.timestamp() > MAX_EVENT_AGE_SECONDS or now < self._retry_until:
            return
        # Record the attempt before network I/O so restarts/timeouts cannot create a burst.
        with self._lock:
            if occupancy:
                self._attempted_at[device] = now
            elif not self._open_alerts.pop(device, False):
                # A queued detection may have failed: never send an orphan clear.
                return
        self._save_state()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.config.token}/sendMessage",
            data=json.dumps({"chat_id": self.config.chat_id, "text": message, "parse_mode": "HTML"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                result = json.load(response)
            if result.get("ok") is True:
                if occupancy:
                    with self._lock:
                        self._open_alerts[device] = True
                    self._save_state()
                LOGGER.info("Telegram motion notification sent for %s (%s)",
                            device, "detected" if occupancy else "cleared")
            else:
                LOGGER.warning("Telegram rejected a notification")
        except urllib.error.HTTPError as error:
            # Never log error strings or request objects: their URL contains the token.
            if error.code == 429:
                try:
                    delay = json.loads(error.read()).get("parameters", {}).get("retry_after", 60)
                    if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0:
                        delay = 60
                    self._retry_until = time.time() + delay
                    self._save_state()
                except (ValueError, TypeError, AttributeError):
                    self._retry_until = time.time() + 60
                    self._save_state()
            LOGGER.warning("Telegram notification rejected (HTTP %s)", error.code)
        except (OSError, ValueError):
            # Do not retry ambiguous timeouts: Telegram may already have accepted the message.
            LOGGER.warning("Telegram notification failed; collector continues normally")

    def _run(self):
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._deliver(*event)
            except Exception:
                LOGGER.error("Unexpected Telegram notification failure; event skipped")
            finally:
                self._queue.task_done()
