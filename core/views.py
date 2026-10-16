import math
from datetime import datetime, timezone

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from django.http import JsonResponse

from services.geo import METERS_PER_MILE
from services.geoapify import fetch_route_options
from services.weatherapi import fetch_forecasts, location_key, weather_at

from .serializers import TripRequestSerializer
from .utils import (
    HEATMAP_HOURS,
    UNKNOWN,
    assess_risk,
    build_checkpoints,
    build_heatmap,
    first_error,
    pick_interval,
    rank_key,
    recommendation_reason,
    serialize_route,
    to_iso,
)


@api_view(["POST"])
@permission_classes([AllowAny])
def plan_trip_view(request):
    """
    Plan a truck trip and score every route option for weather risk.

    Flow:
        Validate the trip request
            ↓
        Stage 1: Get up to 3 truck routes from Geoapify
            ↓
        Stage 2: Drop checkpoints along each route, with ETAs
            ↓
        Stage 3: Fetch forecasts for every checkpoint location (once each)
            ↓
        Stage 4: Score each checkpoint at the hour the truck arrives
            ↓
        Stage 5: Rank routes and pick the recommended one
            ↓
        Stage 6: Build the 0-48h corridor heatmap
            ↓
        Respond with trip, routes and heatmap
    """
    serializer = TripRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return JsonResponse({"error": first_error(serializer.errors)}, status=400)

    trip = serializer.validated_data
    load_lbs = trip["load_weight_lbs"]
    departure_epoch = trip["departure_time"].timestamp()

    # Stage 1: Routes
    try:
        routes = fetch_route_options(trip["origin"], trip["destination"])
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    # Stage 2: Checkpoints
    for route in routes:
        route_miles = route["distance_m"] / METERS_PER_MILE
        route["interval"] = trip.get("sample_interval_miles") or pick_interval(route_miles)
        route["checkpoints"] = build_checkpoints(route, departure_epoch, route["interval"])

    # Stage 3: Forecasts
    # The window must cover both the heatmap (now → +48h) and the latest arrival.
    # +1 day because WeatherAPI days start at local midnight, not at "now".
    now_hour = math.floor(datetime.now(timezone.utc).timestamp() / 3600) * 3600
    last_needed = max(
        now_hour + HEATMAP_HOURS * 3600,
        max(route["checkpoints"][-1]["eta_epoch"] for route in routes),
    )
    days = math.ceil((last_needed - now_hour) / 86400) + 1

    locations = {location_key(cp["lat"], cp["lon"]) for route in routes for cp in route["checkpoints"]}
    forecasts = fetch_forecasts(locations, days)

    # Stage 4: Score checkpoints at their ETA
    results = []
    for number, route in enumerate(routes, start=1):
        for cp in route["checkpoints"]:
            hourly = forecasts[location_key(cp["lat"], cp["lon"])]
            cp["weather"] = weather_at(hourly, cp["eta_epoch"])
            cp["risk"] = assess_risk(cp["weather"], load_lbs)
            cp["risk_level"] = cp["risk"]["level"] if cp["risk"] else UNKNOWN
        results.append(serialize_route(f"route-{number}", route, departure_epoch))

    # Stage 5: Rank (see rank_key for the order of criteria)
    ranked = sorted(results, key=rank_key)
    for rank, route in enumerate(ranked, start=1):
        route["rank"] = rank
        route["is_recommended"] = rank == 1
    best = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None

    return JsonResponse({
        "trip": {
            "origin": trip["origin"],
            "destination": trip["destination"],
            "departure_time": to_iso(departure_epoch),
            "load_weight_lbs": load_lbs,
            "recommended_route_id": best["id"],
            "recommendation_reason": recommendation_reason(best, runner_up),
        },
        "routes": results,
        # Stage 6: Heatmap
        "heatmap": build_heatmap(forecasts, load_lbs, now_hour),
    })
