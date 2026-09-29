import math
import sys
import unittest
from datetime import datetime, timedelta, timezone

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from room_analytics import (  # noqa: E402
    analyze_records,
    anomaly_analysis,
    clean_readings,
    comfort_analysis,
    temperature_forecast,
    temperature_trend,
)


def records(count=40, slope=0.0, pressure=1000.0):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{"temperature": 22.0 + slope * index / 12, "pressure": pressure + index * 0.01, "humidity": None, "recorded_at": start + timedelta(minutes=5 * index)} for index in range(count)]


class RoomAnalyticsTests(unittest.TestCase):
    def test_normal_temperature_is_good(self):
        self.assertIn(comfort_analysis({"temperature": 22.0, "humidity": None})["status"], {"excellent", "good"})

    def test_high_temperature_is_warning_or_poor(self):
        self.assertIn(comfort_analysis({"temperature": 30.0, "humidity": None})["status"], {"warning", "poor"})

    def test_missing_humidity_does_not_crash(self):
        self.assertIn("Humidity is not available", " ".join(comfort_analysis({"temperature": 22.0, "humidity": None})["messages"]))

    def test_insufficient_samples_disable_anomaly_detection(self):
        result = anomaly_analysis(clean_readings(records(10)))
        self.assertFalse(result["available"])

    def test_anomaly_detector_accepts_valid_dataset(self):
        result = anomaly_analysis(clean_readings(records(45)))
        self.assertTrue(result["available"])

    def test_rising_temperature_trend(self):
        result = temperature_trend(clean_readings(records(12, slope=1.2)))
        self.assertEqual(result["direction"], "rising")

    def test_stable_temperature_trend(self):
        result = temperature_trend(clean_readings(records(12)))
        self.assertEqual(result["direction"], "stable")

    def test_temperature_forecast(self):
        result = temperature_forecast(clean_readings(records(14, slope=1.2)))
        self.assertTrue(result["available"])
        self.assertGreater(result["predicted_temperature"], result["current_temperature"])

    def test_nan_data_is_ignored(self):
        data = records(12)
        data.append({"temperature": math.nan, "pressure": math.inf, "humidity": None, "recorded_at": datetime.now(timezone.utc)})
        self.assertEqual(len(clean_readings(data)), 12)

    def test_combined_analysis_survives_insufficient_history(self):
        result = analyze_records(records(2))
        self.assertIn("comfort", result)
        self.assertFalse(result["anomaly"]["available"])


if __name__ == "__main__":
    unittest.main()
