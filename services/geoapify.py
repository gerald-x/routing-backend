"""
Geoapify Routing API client.

Docs: https://apidocs.geoapify.com/docs/routing/
"""
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

ROUTING_URL = "https://api.geoapify.com/v1/routing"
TIMEOUT_SECONDS = 20
MAX_ROUTES = 3

# Geoapify has no "give me alternatives" flag, so options come from different
# routing strategies instead. Some strategies often return the same road
# (e.g. balanced vs less_maneuvers), so there are more candidates than needed.
ROUTE_CANDIDATES = [
    {"label": "Balanced", "params": {"type": "balanced"}},
    {"label": "Shortest", "params": {"type": "short"}},
    {"label": "Toll-free", "params": {"type": "balanced", "avoid": "tolls:1"}},
    {"label": "Fewer turns", "params": {"type": "less_maneuvers"}},
    {"label": "Avoids highways", "params": {"type": "balanced", "avoid": "highways:1"}},
]


def fetch_route(origin, destination, extra_params):
    """
    Request one truck route for a single strategy.

    Returns the first GeoJSON feature, or None if the call fails.
    A failure here is not fatal: other strategies may still succeed.
    """
    params = {
        "waypoints": f"{origin['lat']},{origin['lon']}|{destination['lat']},{destination['lon']}",
        "mode": "truck",
        "apiKey": settings.GEOAPIFY_API_KEY,
        **extra_params,
    }
    try:
        resp = requests.get(ROUTING_URL, params=params, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        features = resp.json().get("features") or []
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Geoapify routing failed for %s: %s", extra_params, exc)
        return None
    return features[0] if features else None


def flatten_coords(geometry):
    """
    Turn Geoapify's MultiLineString (one line per leg) into a single [lon, lat] list.

    Consecutive legs share their joining point, so repeated points are dropped.
    """
    lines = geometry["coordinates"] if geometry["type"] == "MultiLineString" else [geometry["coordinates"]]
    coords = []
    for line in lines:
        for point in line:
            if not coords or coords[-1] != point:
                coords.append(point)
    return coords


def fetch_route_options(origin, destination):
    """
    Get up to 3 distinct truck routes between two places.

    Flow:
        Request every strategy in ROUTE_CANDIDATES in parallel
            ↓
        Drop failed calls and duplicate roads
            ↓
        Keep the first 3 unique routes (candidate order = priority)

    Each route is returned as:
    - label:      human name of the strategy, e.g. "Toll-free"
    - distance_m: total length in meters
    - duration_s: total drive time in seconds
    - toll:       whether the route uses toll roads
    - coords:     the full line as [lon, lat] pairs
    - steps:      turn-by-turn steps, each with its own distance and time

    Raises Exception when no strategy produced a route.
    """
    with ThreadPoolExecutor(max_workers=len(ROUTE_CANDIDATES)) as pool:
        features = list(pool.map(
            lambda c: fetch_route(origin, destination, c["params"]), ROUTE_CANDIDATES
        ))

    routes, seen = [], set()
    for candidate, feature in zip(ROUTE_CANDIDATES, features):
        if not feature:
            continue
        props = feature["properties"]
        # Same distance and time (to the nearest 100 m / minute) means the same road
        fingerprint = (round(props["distance"], -2), round(props["time"] / 60))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        routes.append({
            "label": candidate["label"],
            "distance_m": props["distance"],
            "duration_s": props["time"],
            "toll": bool(props.get("toll")),
            "coords": flatten_coords(feature["geometry"]),
            "steps": [step for leg in props.get("legs", []) for step in leg.get("steps", [])],
        })
        if len(routes) == MAX_ROUTES:
            break

    if not routes:
        raise Exception("No truck route could be found between the origin and destination.")
    return routes
