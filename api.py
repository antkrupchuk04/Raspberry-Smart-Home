import os
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import psycopg2
from psycopg2.extras import RealDictCursor

from room_analytics import analyze_room
from weather_service import WeatherUnavailable, get_current_weather
from system_status import SystemStatusMonitor


LOGGER = logging.getLogger(__name__)


app = FastAPI(
    title="Smart Home API",
    version="1.0"
)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "database": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

system_monitor = SystemStatusMonitor(
    DB_CONFIG,
    mqtt_host=os.getenv("MQTT_BROKER", "localhost"),
    mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
    zigbee_device=os.getenv("ZIGBEE_SERIAL_DEVICE"),
)


def get_connection():
    return psycopg2.connect(**DB_CONFIG)


app.mount(
    "/static",
    StaticFiles(directory="/home/anton/smarthome/static"),
    name="static"
)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(
        "/home/anton/smarthome/static/index.html"
    )


@app.get("/api/sensors/latest")
def get_latest():
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    id,
                    device_id,
                    sensor_type,
                    temperature,
                    pressure,
                    humidity,
                    recorded_at
                FROM sensor_readings
                ORDER BY recorded_at DESC
                LIMIT 1
            """)

            reading = cur.fetchone()

    if reading is None:
        raise HTTPException(
            status_code=404,
            detail="No sensor readings available"
        )

    return reading


@app.get("/api/system/status")
def get_system_status():
    return JSONResponse(system_monitor.get_system_status(), headers={"Cache-Control": "no-store"})


@app.get("/api/sensors/history")
def get_history(
    hours: int = Query(default=1, ge=1, le=24)
):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    id,
                    device_id,
                    sensor_type,
                    temperature,
                    pressure,
                    humidity,
                    recorded_at
                FROM sensor_readings
                WHERE recorded_at >= NOW() - (%s * INTERVAL '1 hour')
                ORDER BY recorded_at ASC
            """, (hours,))

            readings = cur.fetchall()

    return readings


@app.get("/api/analytics/room")
def get_room_analytics():
    """Return lightweight local room analytics without exposing database details."""
    try:
        return analyze_room(DB_CONFIG)
    except psycopg2.Error:
        LOGGER.exception("Room analytics database query failed")
        raise HTTPException(status_code=503, detail="Room analytics is temporarily unavailable")
    except Exception:
        LOGGER.exception("Room analytics failed unexpectedly")
        raise HTTPException(status_code=500, detail="Room analytics failed")


@app.get("/api/weather/current")
async def get_weather_current():
    try:
        return await get_current_weather()
    except WeatherUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error))
