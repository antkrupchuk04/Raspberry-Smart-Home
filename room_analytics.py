"""Local statistical room analytics for the Smart Home dashboard.

The module is intentionally self-contained: it queries only the existing
PostgreSQL readings and uses small CPU-friendly NumPy/scikit-learn models.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import psycopg2
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LinearRegression


LOGGER = logging.getLogger(__name__)

COMFORT_CONFIG = {
    "temperature": {"ideal_min": 20.0, "ideal_max": 24.0, "warning_min": 18.0, "warning_max": 27.0},
    "humidity": {"ideal_min": 40.0, "ideal_max": 60.0, "warning_min": 30.0, "warning_max": 70.0},
}
QUALITY_BOUNDS = {"temperature": (-40.0, 85.0), "humidity": (0.0, 100.0), "pressure": (300.0, 1100.0)}
ANOMALY_CONFIG = {"minimum_samples": 30, "contamination": 0.02, "cache_seconds": 300}
TREND_CONFIG = {"window_minutes": 30, "minimum_samples": 6, "stable_slope_c_per_hour": 0.2}
FORECAST_CONFIG = {"history_minutes": 60, "horizon_minutes": 30, "minimum_samples": 8, "minimum_r2": 0.2}

_MODEL_CACHE: dict[str, Any] = {"trained_at": 0.0, "model": None, "columns": None, "signature": None}


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_recent_readings(db_config: Mapping[str, Any], hours: int = 24, limit: int = 2000) -> list[dict[str, Any]]:
    """Load only the recent rows needed for local analytics."""
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT temperature, pressure, humidity, recorded_at
                FROM sensor_readings
                WHERE recorded_at >= NOW() - (%s * INTERVAL '1 hour')
                ORDER BY recorded_at ASC
                LIMIT %s
                """,
                (hours, limit),
            )
            columns = [column.name for column in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def clean_readings(readings: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """Normalize records and discard impossible or non-finite telemetry."""
    frame = pd.DataFrame(readings, columns=["temperature", "pressure", "humidity", "recorded_at"])
    if frame.empty:
        return frame
    frame["recorded_at"] = pd.to_datetime(frame["recorded_at"], utc=True, errors="coerce")
    for column in ("temperature", "pressure", "humidity"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        lower, upper = QUALITY_BOUNDS[column]
        frame.loc[~frame[column].between(lower, upper), column] = np.nan
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["recorded_at", "temperature"])
    frame = frame.sort_values("recorded_at").drop_duplicates(subset=["recorded_at"], keep="last")
    return frame.reset_index(drop=True)


def _range_score(value: float | None, config: Mapping[str, float], label: str) -> tuple[int, str]:
    if value is None or not np.isfinite(value):
        return 100, f"{label} is unavailable"
    if config["ideal_min"] <= value <= config["ideal_max"]:
        return 100, f"{label} is within the comfort range"
    if config["warning_min"] <= value <= config["warning_max"]:
        return 70, f"{label} is outside the preferred range"
    return 25, f"{label} is outside the recommended range"


def comfort_analysis(latest: Mapping[str, Any] | None, pressure_slope: float | None = None) -> dict[str, Any]:
    if not latest:
        return {"status": "warning", "score": 0, "messages": ["No valid sensor reading is available"]}
    temperature = latest.get("temperature")
    humidity = latest.get("humidity")
    temperature_score, temperature_message = _range_score(temperature, COMFORT_CONFIG["temperature"], "Temperature")
    scores = [temperature_score]
    messages = [temperature_message]
    if humidity is not None and np.isfinite(humidity):
        humidity_score, humidity_message = _range_score(humidity, COMFORT_CONFIG["humidity"], "Humidity")
        scores.append(humidity_score)
        messages.append(humidity_message)
    else:
        messages.append("Humidity is not available from the current sensor")
    if pressure_slope is None:
        messages.append("Pressure trend needs more history")
    elif abs(pressure_slope) < 1.0:
        messages.append("Pressure is stable")
    else:
        messages.append("Pressure is changing")
        scores.append(85)
    score = int(round(float(np.mean(scores))))
    status = "excellent" if score >= 95 else "good" if score >= 75 else "warning" if score >= 50 else "poor"
    return {"status": status, "score": score, "messages": messages}


def _slope(frame: pd.DataFrame, column: str) -> float | None:
    usable = frame.dropna(subset=[column])
    if len(usable) < 2:
        return None
    origin = usable["recorded_at"].iloc[0]
    hours = (usable["recorded_at"] - origin).dt.total_seconds().to_numpy() / 3600
    if np.ptp(hours) == 0:
        return None
    return float(np.polyfit(hours, usable[column].to_numpy(dtype=float), 1)[0])


def temperature_trend(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"available": False, "reason": "insufficient_data", "samples": 0, "required_samples": TREND_CONFIG["minimum_samples"]}
    cutoff = frame["recorded_at"].max() - pd.Timedelta(minutes=TREND_CONFIG["window_minutes"])
    recent = frame[frame["recorded_at"] >= cutoff]
    if len(recent) < TREND_CONFIG["minimum_samples"]:
        return {"available": False, "reason": "insufficient_data", "samples": len(recent), "required_samples": TREND_CONFIG["minimum_samples"]}
    slope = _slope(recent, "temperature")
    if slope is None:
        return {"available": False, "reason": "invalid_timestamps"}
    direction = "stable" if abs(slope) < TREND_CONFIG["stable_slope_c_per_hour"] else "rising" if slope > 0 else "falling"
    return {"available": True, "direction": direction, "slope_c_per_hour": round(slope, 3), "samples": len(recent)}


def temperature_forecast(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"available": False, "reason": "insufficient_data", "samples": 0, "required_samples": FORECAST_CONFIG["minimum_samples"]}
    cutoff = frame["recorded_at"].max() - pd.Timedelta(minutes=FORECAST_CONFIG["history_minutes"])
    recent = frame[frame["recorded_at"] >= cutoff].dropna(subset=["temperature"])
    if len(recent) < FORECAST_CONFIG["minimum_samples"]:
        return {"available": False, "reason": "insufficient_data", "samples": len(recent), "required_samples": FORECAST_CONFIG["minimum_samples"]}
    origin = recent["recorded_at"].iloc[0]
    minutes = (recent["recorded_at"] - origin).dt.total_seconds().to_numpy() / 60
    if np.ptp(minutes) == 0:
        return {"available": False, "reason": "invalid_timestamps"}
    model = LinearRegression().fit(minutes.reshape(-1, 1), recent["temperature"].to_numpy())
    r2 = float(model.score(minutes.reshape(-1, 1), recent["temperature"].to_numpy()))
    current_temperature = float(recent["temperature"].iloc[-1])
    predicted = float(model.predict(np.array([[minutes[-1] + FORECAST_CONFIG["horizon_minutes"]]]))[0])
    response = {"horizon_minutes": FORECAST_CONFIG["horizon_minutes"], "current_temperature": round(current_temperature, 2), "predicted_temperature": round(predicted, 2), "model": "linear_regression", "r2": round(r2, 3), "samples": len(recent)}
    if r2 < FORECAST_CONFIG["minimum_r2"] or not QUALITY_BOUNDS["temperature"][0] <= predicted <= QUALITY_BOUNDS["temperature"][1]:
        return {"available": False, "reason": "low_model_quality", **response}
    return {"available": True, **response}


def _feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    features = frame.dropna(subset=["temperature", "pressure"]).copy()
    if features.empty:
        return features
    elapsed = features["recorded_at"].diff().dt.total_seconds().replace(0, np.nan)
    features["temperature_rate"] = features["temperature"].diff() / elapsed * 60
    features["pressure_rate"] = features["pressure"].diff() / elapsed * 60
    features = features.replace([np.inf, -np.inf], np.nan).dropna(subset=["temperature_rate", "pressure_rate"])
    columns = ["temperature", "pressure", "temperature_rate", "pressure_rate"]
    if features["humidity"].notna().sum() >= ANOMALY_CONFIG["minimum_samples"]:
        features["humidity"] = features["humidity"].fillna(features["humidity"].median())
        columns.append("humidity")
    return features[columns]


def anomaly_analysis(frame: pd.DataFrame) -> dict[str, Any]:
    features = _feature_frame(frame)
    required = ANOMALY_CONFIG["minimum_samples"]
    if len(features) < required:
        LOGGER.info("Analytics anomaly model needs %s samples; only %s are valid", required, len(features))
        return {"available": False, "reason": "insufficient_data", "samples": len(features), "required_samples": required}
    signature = (len(features), tuple(features.columns), float(features.iloc[-1, 0]))
    cache_valid = time.monotonic() - _MODEL_CACHE["trained_at"] < ANOMALY_CONFIG["cache_seconds"]
    if not cache_valid or _MODEL_CACHE["model"] is None:
        LOGGER.info("Training IsolationForest with %s samples", len(features))
        _MODEL_CACHE.update({
            "trained_at": time.monotonic(),
            "model": IsolationForest(contamination=ANOMALY_CONFIG["contamination"], random_state=42).fit(features),
            "columns": list(features.columns),
            "signature": signature,
        })
    model: IsolationForest = _MODEL_CACHE["model"]
    latest = features.iloc[[-1]]
    decision = float(model.decision_function(latest)[0])
    anomalous = bool(model.predict(latest)[0] == -1)
    return {
        "available": True,
        "anomaly": anomalous,
        "score": round(max(0.0, -decision), 4),
        "description": "Unusual environmental behaviour detected" if anomalous else "No unusual environmental behaviour detected",
        "samples": len(features),
    }


def analyze_records(readings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Analyze supplied records; used by both PostgreSQL access and tests."""
    frame = clean_readings(readings)
    latest = frame.iloc[-1].to_dict() if not frame.empty else None
    pressure_slope = _slope(frame, "pressure")
    result = {"generated_at": _generated_at(), "comfort": comfort_analysis(latest, pressure_slope)}
    for name, function in (("anomaly", anomaly_analysis), ("temperature_trend", temperature_trend), ("forecast", temperature_forecast)):
        try:
            result[name] = function(frame)
        except Exception:
            LOGGER.exception("Room analytics %s failed", name)
            result[name] = {"available": False, "reason": "analysis_error"}
    return result


def analyze_room(db_config: Mapping[str, Any]) -> dict[str, Any]:
    return analyze_records(load_recent_readings(db_config))
