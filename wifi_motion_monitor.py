#!/usr/bin/env python3
"""Detect large Wi-Fi RSSI changes as a simple motion signal.

This is a heuristic, not Wi-Fi CSI. It samples the Raspberry Pi's Wi-Fi link
signal using ``iw`` and reports an event when the current RSSI differs from a
rolling median baseline by the configured threshold. It needs a connected
wireless interface; it cannot operate over Ethernet.

Examples:
    python3 wifi_motion_monitor.py
    python3 wifi_motion_monitor.py --interface wlan0 --threshold-db 6
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import time
from collections import deque
from datetime import datetime
from typing import Sequence


SIGNAL_PATTERN = re.compile(r"^\s*signal:\s*(-?\d+(?:\.\d+)?)\s*dBm\s*$", re.MULTILINE)


class SignalUnavailable(RuntimeError):
    """Raised when the Wi-Fi link has no usable RSSI reading."""


def find_iw() -> str:
    """Find ``iw`` even when system administration paths are not in PATH."""
    candidates = (shutil.which("iw"), "/usr/sbin/iw", "/sbin/iw")
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("iw")


def read_signal_dbm(interface: str) -> float:
    """Return the current Wi-Fi signal in dBm from ``iw dev <interface> link``."""
    result = subprocess.run(
        [find_iw(), "dev", interface, "link"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    output = f"{result.stdout}\n{result.stderr}"
    match = SIGNAL_PATTERN.search(output)
    if result.returncode != 0 or not match:
        raise SignalUnavailable(output.strip() or f"No signal reported for {interface}")
    return float(match.group(1))


def event_payload(signal_dbm: float, baseline_dbm: float) -> dict[str, object]:
    """Build one machine-readable event from observed, not simulated, values."""
    return {
        "event": "wifi_rssi_change",
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "signal_dbm": round(signal_dbm, 1),
        "baseline_dbm": round(baseline_dbm, 1),
        "delta_db": round(abs(signal_dbm - baseline_dbm), 1),
    }


def monitor(
    interface: str,
    interval_seconds: float,
    threshold_db: float,
    window_size: int,
    cooldown_seconds: float,
) -> None:
    try:
        find_iw()
    except FileNotFoundError as error:
        raise SystemExit(
            "The 'iw' command is required. On Raspberry Pi OS run: sudo apt install iw"
        ) from error

    samples: deque[float] = deque(maxlen=window_size)
    last_event_at = float("-inf")

    print(
        f"Monitoring {interface}: threshold={threshold_db:.1f} dB, "
        f"baseline window={window_size}, interval={interval_seconds:.1f}s"
    )

    while True:
        try:
            signal_dbm = read_signal_dbm(interface)
        except FileNotFoundError:
            raise SystemExit(
                "The 'iw' command is required. On Raspberry Pi OS run: sudo apt install iw"
            )
        except (OSError, subprocess.TimeoutExpired, SignalUnavailable) as error:
            print(f"NO_SIGNAL {datetime.now().astimezone().isoformat(timespec='seconds')} {error}")
            time.sleep(interval_seconds)
            continue

        if len(samples) < window_size:
            samples.append(signal_dbm)
            print(f"WARMING_UP signal={signal_dbm:.1f} dBm samples={len(samples)}/{window_size}")
            time.sleep(interval_seconds)
            continue

        baseline_dbm = statistics.median(samples)
        delta_db = abs(signal_dbm - baseline_dbm)
        now = time.monotonic()

        if delta_db >= threshold_db and now - last_event_at >= cooldown_seconds:
            print(f"MOTION {json.dumps(event_payload(signal_dbm, baseline_dbm), separators=(',', ':'))}")
            last_event_at = now
        else:
            print(
                f"STABLE signal={signal_dbm:.1f} dBm baseline={baseline_dbm:.1f} dBm "
                f"delta={delta_db:.1f} dB"
            )

        samples.append(signal_dbm)
        time.sleep(interval_seconds)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="wlan0", help="Wireless interface to sample (default: wlan0)")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between samples (default: 2)")
    parser.add_argument("--threshold-db", type=float, default=6.0, help="RSSI change that triggers an event (default: 6)")
    parser.add_argument("--window-size", type=int, default=15, help="Rolling baseline sample count (default: 15)")
    parser.add_argument("--cooldown", type=float, default=20.0, help="Seconds between events (default: 20)")
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.threshold_db <= 0 or args.window_size < 3 or args.cooldown < 0:
        parser.error("interval and threshold must be positive, window-size must be at least 3, and cooldown cannot be negative")
    return args


if __name__ == "__main__":
    config = parse_args()
    monitor(
        interface=config.interface,
        interval_seconds=config.interval,
        threshold_db=config.threshold_db,
        window_size=config.window_size,
        cooldown_seconds=config.cooldown,
    )
