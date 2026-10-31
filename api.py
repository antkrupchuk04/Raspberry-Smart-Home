import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import psycopg2
from psycopg2.extras import RealDictCursor

from room_analytics import analyze_room
from weather_service import WeatherUnavailable, get_current_weather
from system_status import SystemStatusMonitor
from motion_service import MotionConfig, get_motion_status
from topology_service import TopologyService
from csi_service import CsiMonitor
from csi_history import CsiEventStore


LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    csi_event_store.start()
    try:
        csi_monitor.start()
        yield
    finally:
        csi_monitor.stop()
        csi_event_store.stop()


app = FastAPI(
    title="Smart Home API",
    version="1.0",
    lifespan=lifespan
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
motion_config = MotionConfig.from_env()
csi_event_store = CsiEventStore(DB_CONFIG, room=os.getenv("CSI_ROOM", "Room 1"))
csi_monitor = CsiMonitor(
    host=os.getenv("MQTT_BROKER", "localhost"),
    port=int(os.getenv("MQTT_PORT", "1883")),
    topic=os.getenv("CSI_MQTT_TOPIC", "home/motion/room1/csi"),
    device_id=os.getenv("CSI_DEVICE_ID", "esp32-room1"),
    on_event=csi_event_store.submit,
)
topology_service = TopologyService(DB_CONFIG, system_monitor,
                                   motion_devices=({"id": "motion-room1", "config": motion_config},))


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


@app.get("/topology", include_in_schema=False)
def topology_page():
    return FileResponse("/home/anton/smarthome/static/topology.html")


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


@app.get("/api/motion")
def get_motion(response: Response, hours: int = Query(default=24, ge=1, le=168),
               limit: int = Query(default=20, ge=1, le=100)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return get_motion_status(DB_CONFIG, motion_config, hours, limit)
    except psycopg2.Error:
        LOGGER.exception("Motion database query failed")
        raise HTTPException(status_code=503, detail="Motion monitoring is temporarily unavailable")


@app.get("/api/motion/csi")
def get_csi_status(hours: int = Query(default=24, ge=1, le=168),
                   limit: int = Query(default=20, ge=1, le=100)):
    history = csi_event_store.read(csi_monitor.device_id, hours, limit)
    data = csi_monitor.snapshot()
    if data["last_motion_at"] is None:
        data["last_motion_at"] = history["history_last_motion_at"]
    return JSONResponse({**data, **history}, headers={"Cache-Control": "no-store"})


@app.get("/api/topology")
def get_topology():
    return JSONResponse(topology_service.get_topology(), headers={"Cache-Control": "no-store"})


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
