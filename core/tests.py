from unittest.mock import patch

from django.test import SimpleTestCase

from .serializers import TripRequestSerializer
from .utils import assess_risk, first_error


def wx(wind=0, rain=0, snow=0):
    return {"wind_mph": wind, "rain_in_hr": rain, "snow_in_hr": snow, "temp_f": 50, "condition": "Clear"}


VALID_TRIP = {
    "origin": {"address": "Chicago, IL", "lat": 41.8781, "lon": -87.6298},
    "destination": {"address": "Indianapolis, IN", "lat": 39.7684, "lon": -86.1581},
    "departure_time": "2026-10-15T12:00:00.000Z",
    "load_weight_lbs": 35000,
}


class RiskClassificationTests(SimpleTestCase):
    def assertLevel(self, weather, load, expected):
        self.assertEqual(assess_risk(weather, load)["level"], expected)

    def test_wind_bands(self):
        for mph, level in [(24.9, "Low"), (25, "Moderate"), (35, "High"), (45, "Severe"), (55, "No Travel")]:
            self.assertLevel(wx(wind=mph), 10_000, level)

    def test_rain_bands(self):
        for inches, level in [(0.09, "Low"), (0.10, "Moderate"), (0.25, "High"), (0.50, "Severe"),
                              (1.00, "Severe"), (1.01, "No Travel")]:
            self.assertLevel(wx(rain=inches), 10_000, level)

    def test_snow_bands(self):
        for inches, level in [(0.4, "Low"), (0.5, "Moderate"), (1.0, "High"), (2.0, "Severe"),
                              (3.0, "Severe"), (3.1, "No Travel")]:
            self.assertLevel(wx(snow=inches), 10_000, level)

    def test_load_rules(self):
        self.assertLevel(wx(wind=50), 30_000, "Severe")
        self.assertLevel(wx(wind=50), 30_001, "No Travel")
        self.assertLevel(wx(wind=40), 40_000, "High")
        self.assertLevel(wx(wind=40), 40_001, "Severe")
        self.assertLevel(wx(wind=60), 1_000, "No Travel")

    def test_worst_condition_wins(self):
        result = assess_risk(wx(wind=30, rain=0.6, snow=0.2), 10_000)
        self.assertEqual(result["level"], "Severe")
        self.assertEqual(result["drivers"], ["rain"])

    def test_missing_forecast(self):
        self.assertIsNone(assess_risk(None, 10_000))


class TripRequestTests(SimpleTestCase):
    url = "/api/routing/trips/plan/"

    def test_rejects_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_validation_errors(self):
        cases = [
            {**VALID_TRIP, "origin": None},
            {**VALID_TRIP, "destination": {"address": "x", "lat": 100, "lon": 0}},
            {**VALID_TRIP, "departure_time": "tomorrow"},
            {**VALID_TRIP, "load_weight_lbs": -5},
        ]
        for body in cases:
            resp = self.client.post(self.url, body, content_type="application/json")
            self.assertEqual(resp.status_code, 400, body)
            self.assertIn("error", resp.json())

    def test_nested_error_message(self):
        serializer = TripRequestSerializer(data={**VALID_TRIP, "destination": {"address": "x", "lat": 100, "lon": 0}})
        serializer.is_valid()
        self.assertTrue(first_error(serializer.errors).startswith("destination.lat: "))

    def test_accepts_string_weight(self):
        serializer = TripRequestSerializer(data={**VALID_TRIP, "load_weight_lbs": "35000"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["load_weight_lbs"], 35000.0)

    @patch("core.views.fetch_route_options", side_effect=Exception("No truck route could be found."))
    def test_routing_failure_returns_502(self, _):
        resp = self.client.post(self.url, VALID_TRIP, content_type="application/json")
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json(), {"error": "No truck route could be found."})
