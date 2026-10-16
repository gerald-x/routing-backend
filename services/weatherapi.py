"""
WeatherAPI forecast client.

Docs: https://www.weatherapi.com/docs/
"""
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.weatherapi.com/v1/forecast.json"
TIMEOUT_SECONDS = 15
MAX_WORKERS = 16
MAX_FORECAST_DAYS = 14  # API ceiling; the free plan silently caps this at 3
CM_PER_INCH = 2.54
SECONDS_PER_HOUR = 3600


def location_key(lat, lon):
    """
    Round a point onto a ~1 km grid.

    Used as the forecast cache key, so points that several routes share
    (origin, destination, overlapping roads) are only fetched once.
    """
    return (round(lat, 2), round(lon, 2))


def fetch_hourly(key, days):
    """
    Hourly forecast for one location, keyed by the hour's UTC epoch.

    Each hour is trimmed to what the risk rules need:
    - wind_mph:   sustained wind speed
    - rain_in_hr: precipitation in inches for that hour
    - snow_in_hr: snowfall, converted from WeatherAPI's cm to inches
    - temp_f, condition: display only

    Returns {} on failure, so one bad location doesn't sink the whole trip.
    """
    lat, lon = key
    params = {
        "key": settings.WEATHERAPI_API_KEY,
        "q": f"{lat},{lon}",
        "days": days,
        "aqi": "no",
        "alerts": "no",
    }
    try:
        resp = requests.get(FORECAST_URL, params=params, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        forecast_days = resp.json()["forecast"]["forecastday"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("WeatherAPI forecast failed for %s: %s", key, exc)
        return {}

    hourly = {}
    for day in forecast_days:
        for hour in day["hour"]:
            hourly[hour["time_epoch"]] = {
                "wind_mph": hour["wind_mph"],
                "rain_in_hr": hour["precip_in"],
                "snow_in_hr": round(hour.get("snow_cm", 0) / CM_PER_INCH, 2),
                "temp_f": hour["temp_f"],
                "condition": hour["condition"]["text"],
            }
    return hourly


def fetch_forecasts(keys, days):
    """
    Fetch hourly forecasts for many locations in parallel.

    Returns {location_key: {hour_epoch: weather}}.
    A location whose call failed maps to {} (every lookup on it returns None).
    """
    days = max(1, min(days, MAX_FORECAST_DAYS))
    keys = list(keys)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = pool.map(lambda k: fetch_hourly(k, days), keys)
    return dict(zip(keys, results))


def weather_at(hourly, epoch_seconds):
    """
    The forecast hour nearest to `epoch_seconds`.

    Returns None when that hour is outside the forecast window
    (e.g. an ETA 4 days out on the free plan's 3-day forecast).
    """
    hour = round(epoch_seconds / SECONDS_PER_HOUR) * SECONDS_PER_HOUR
    return hourly.get(hour)
