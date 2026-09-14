"""Cached Open-Meteo current-weather client for the local FastAPI backend."""

from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 5
CURRENT_FIELDS = (
    "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,"
    "weather_code,surface_pressure,wind_speed_10m"
)
WMO_CONDITIONS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
    61: "Rain", 63: "Rain", 65: "Rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Snow", 73: "Snow", 75: "Snow", 77: "Snow grains",
    80: "Rain showers", 81: "Rain showers", 82: "Rain showers",
    85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
}


class WeatherUnavailable(RuntimeError):
    """Raised when there is no usable current weather result."""


_cache: dict[str, Any] = {"value": None, "fetched_monotonic": 0.0}
_cache_lock = asyncio.Lock()


def _number(current: dict[str, Any], field: str, integer: bool = False) -> float | int:
    try:
        value = float(current[field])
    except (KeyError, TypeError, ValueError) as error:
        raise WeatherUnavailable(f"Open-Meteo response is missing {field}") from error
    if not math.isfinite(value):
        raise WeatherUnavailable(f"Open-Meteo response has invalid {field}")
    return int(value) if integer else round(value, 1)


def _config() -> tuple[float, float, str]:
    try:
        latitude = float(os.environ["WEATHER_LAT"])
        longitude = float(os.environ["WEATHER_LON"])
    except (KeyError, ValueError) as error:
        raise WeatherUnavailable("Weather location is not configured") from error
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise WeatherUnavailable("Weather coordinates are invalid")
    return latitude, longitude, os.getenv("WEATHER_LOCATION_NAME", "Configured location")


def _normalize(payload: dict[str, Any], location: str) -> dict[str, Any]:
    current = payload.get("current")
    if not isinstance(current, dict):
        raise WeatherUnavailable("Open-Meteo response has no current weather")
    code = _number(current, "weather_code", integer=True)
    return {
        "location": location,
        "temperature": _number(current, "temperature_2m"),
        "apparent_temperature": _number(current, "apparent_temperature"),
        "humidity": _number(current, "relative_humidity_2m", integer=True),
        "pressure": _number(current, "surface_pressure"),
        "wind_speed": _number(current, "wind_speed_10m"),
        "precipitation": _number(current, "precipitation"),
        "weather_code": code,
        "condition": WMO_CONDITIONS.get(code, "Unknown"),
        "weather_time": str(current.get("time", "")) or None,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "open-meteo",
        "stale": False,
    }


async def get_current_weather() -> dict[str, Any]:
    """Return a fresh cached value, or a stale value if a refresh fails."""
    now = time.monotonic()
    cached = _cache["value"]
    if cached and now - _cache["fetched_monotonic"] < CACHE_SECONDS:
        return dict(cached)

    async with _cache_lock:
        now = time.monotonic()
        cached = _cache["value"]
        if cached and now - _cache["fetched_monotonic"] < CACHE_SECONDS:
            return dict(cached)
        try:
            latitude, longitude, location = _config()
            params = {
                "latitude": latitude,
                "longitude": longitude,
                "current": CURRENT_FIELDS,
                "timezone": "auto",
                "wind_speed_unit": "kmh",
            }
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.get(OPEN_METEO_URL, params=params)
                response.raise_for_status()
            weather = _normalize(response.json(), location)
            _cache.update({"value": weather, "fetched_monotonic": time.monotonic()})
            return dict(weather)
        except (httpx.HTTPError, ValueError, WeatherUnavailable) as error:
            if cached:
                stale = dict(cached)
                stale["stale"] = True
                return stale
            raise WeatherUnavailable("Outdoor weather is temporarily unavailable") from error
