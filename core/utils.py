import math
from datetime import datetime, timezone

from services.geo import METERS_PER_MILE, cumulative_miles, downsample, interpolate, point_at_distance
from services.weatherapi import weather_at

HEATMAP_HOURS = 48
MAX_GEOMETRY_POINTS = 2000


# ---------------------------------------------------------------------------
# Weather risk
# ---------------------------------------------------------------------------

LOW, MODERATE, HIGH, SEVERE, NO_TRAVEL = range(5)
LEVEL_NAMES = ["Low", "Moderate", "High", "Severe", "No Travel"]
UNKNOWN = "Unknown"


def classify_wind(mph):
    if mph < 25:
        return LOW
    if mph < 35:
        return MODERATE
    if mph < 45:
        return HIGH
    if mph < 55:
        return SEVERE
    return NO_TRAVEL


def classify_rain(in_hr):
    """
    Boundary handling:
    - Shared boundaries (0.10, 0.25, 0.50) belong to the higher band.
    - Exactly 1.00 is still Severe; No Travel is strictly "> 1.00".
    """
    if in_hr < 0.10:
        return LOW
    if in_hr < 0.25:
        return MODERATE
    if in_hr < 0.50:
        return HIGH
    if in_hr <= 1.00:
        return SEVERE
    return NO_TRAVEL


def classify_snow(in_hr):
    """Same boundary handling as rain; No Travel is strictly "> 3.0"."""
    if in_hr < 0.5:
        return LOW
    if in_hr < 1.0:
        return MODERATE
    if in_hr < 2.0:
        return HIGH
    if in_hr <= 3.0:
        return SEVERE
    return NO_TRAVEL


def apply_load_rules(wind_mph, wind_level, load_lbs):
    """
    Heavier loads are more exposed to crosswinds, so wind risk is bumped up:

    - ≥55 mph            → No Travel for any load (already handled by classify_wind)
    - 45-54 mph + >30,000 lb → No Travel
    - 35-44 mph + >40,000 lb → Severe
    """
    if 45 <= wind_mph < 55 and load_lbs > 30_000:
        return NO_TRAVEL
    if 35 <= wind_mph < 45 and load_lbs > 40_000:
        return max(wind_level, SEVERE)
    return wind_level


def assess_risk(weather, load_lbs):
    """
    Risk for a single forecast hour.

    Flow:
        Classify wind, rain and snow independently
            ↓
        Bump the wind level using the load rules
            ↓
        Final risk = the worst of the three

    Returns None when there is no forecast, otherwise:
    - score:          0 (Low) … 4 (No Travel), used for averages and the heatmap
    - level:          the score's name, e.g. "Severe"
    - factors:        each condition's own level
    - drivers:        which condition(s) set the final level (empty when Low)
    - load_escalated: True if the load rules raised the wind level
    """
    if weather is None:
        return None

    wind_level = classify_wind(weather["wind_mph"])
    factors = {
        "wind": apply_load_rules(weather["wind_mph"], wind_level, load_lbs),
        "rain": classify_rain(weather["rain_in_hr"]),
        "snow": classify_snow(weather["snow_in_hr"]),
    }
    score = max(factors.values())
    return {
        "score": score,
        "level": LEVEL_NAMES[score],
        "factors": {name: LEVEL_NAMES[lvl] for name, lvl in factors.items()},
        "drivers": [name for name, lvl in factors.items() if lvl == score and score > LOW],
        "load_escalated": factors["wind"] > wind_level,
    }


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

def pick_interval(route_miles):
    """
    Default checkpoint spacing when the client doesn't choose one.

    - ≤150 mi → every 10 mi
    - ≤500 mi → every 25 mi
    - longer  → every 50 mi

    Denser sampling on short trips; sparser on long ones to limit weather calls.
    """
    if route_miles <= 150:
        return 10
    if route_miles <= 500:
        return 25
    return 50


def build_checkpoints(route, departure_epoch, interval):
    """
    Place a checkpoint every `interval` miles and estimate when the truck gets there.

    Stage 1: Where is each checkpoint?
    - Marks at 0, interval, 2xinterval, … plus the destination.
    - Each mark is turned into a lat/lon by walking along the route line.
    - Our haversine length differs slightly from Geoapify's distance,
      so marks are scaled between the two.

    Stage 2: When does the truck get there?
    - Each Geoapify step has its own distance and time.
    - Adding them up gives (distance, time) pairs along the route.
    - A checkpoint's ETA is interpolated from those pairs.
    - This follows the real speed profile (slow city streets vs highway)
      instead of assuming one flat average speed.

    Returns [{mile, lat, lon, eta_epoch}].
    """
    coords = route["coords"]
    cum = cumulative_miles(coords)
    geom_miles = cum[-1] or 1
    total_m = route["distance_m"]

    dist_marks, time_marks = [0.0], [0.0]
    for step in route["steps"]:
        dist_marks.append(dist_marks[-1] + step["distance"])
        time_marks.append(time_marks[-1] + step["time"])
    if len(dist_marks) == 1:
        # No steps returned: fall back to a flat average speed
        dist_marks, time_marks = [0.0, total_m], [0.0, route["duration_s"]]

    route_miles = total_m / METERS_PER_MILE
    marks = [i * interval for i in range(math.ceil(route_miles / interval))] + [route_miles]

    checkpoints = []
    for mile in marks:
        lon, lat = point_at_distance(coords, cum, mile / route_miles * geom_miles)
        elapsed = interpolate(dist_marks, time_marks, mile * METERS_PER_MILE)
        checkpoints.append({
            "mile": round(mile, 1),
            "lat": lat,
            "lon": lon,
            "eta_epoch": departure_epoch + elapsed,
        })
    return checkpoints


# ---------------------------------------------------------------------------
# Route summaries and ranking
# ---------------------------------------------------------------------------

RANK_REASONS = [
    "fewest No Travel miles",
    "fewest Severe miles",
    "fewest High miles",
    "lowest average risk",
    "shortest travel time",
]


def to_iso(epoch_seconds):
    return datetime.fromtimestamp(round(epoch_seconds), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def format_duration(seconds):
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours}h {minutes:02d}m"


def miles_owned(checkpoints):
    """
    How many road miles each checkpoint stands for.

    Each checkpoint covers half the road to each neighbour:

        cp0 ──── cp1 ──── cp2
        |←0→|←── cp1 ──→|←cp2→|

    So the first and last checkpoints cover half an interval each.
    """
    miles = [c["mile"] for c in checkpoints]
    owned = []
    for i, m in enumerate(miles):
        prev_m = miles[i - 1] if i > 0 else m
        next_m = miles[i + 1] if i < len(miles) - 1 else m
        owned.append((next_m - prev_m) / 2)
    return owned


def summarise_risk(checkpoints):
    """
    Roll checkpoint risks up into route-level numbers.

    - miles_by_level:      road miles at each risk level
    - unknown_miles:       miles with no forecast (ETA beyond the forecast window)
    - average_risk_score:  mile-weighted average, 0 (Low) … 4 (No Travel);
                           unknown miles are left out rather than counted as Low
    - peak_risk_level / peak_checkpoint_mile: the worst spot on the route
    """
    by_level = {name: 0.0 for name in LEVEL_NAMES}
    unknown_miles = weighted = known_miles = 0.0
    peak = None

    for cp, miles in zip(checkpoints, miles_owned(checkpoints)):
        assessment = cp["risk"]
        if assessment is None:
            unknown_miles += miles
            continue
        by_level[assessment["level"]] += miles
        weighted += assessment["score"] * miles
        known_miles += miles
        if peak is None or assessment["score"] > peak["risk"]["score"]:
            peak = cp

    return {
        "miles_by_level": {k: round(v, 1) for k, v in by_level.items()},
        "unknown_miles": round(unknown_miles, 1),
        "average_risk_score": round(weighted / known_miles, 2) if known_miles else None,
        "peak_risk_level": peak["risk"]["level"] if peak else UNKNOWN,
        "peak_checkpoint_mile": peak["mile"] if peak else None,
    }


def rank_key(route):
    """
    Sort key for picking the best route; lower is better.

    Compared in order, moving on only when tied:
    1. No Travel miles
    2. Severe miles
    3. High miles
    4. Average risk
    5. Travel time

    No Travel comes first even though it isn't in the given rules,
    otherwise a route with an impassable stretch could win just by
    having fewer Severe miles.
    """
    s = route["risk_summary"]
    avg = s["average_risk_score"]
    return (
        s["miles_by_level"]["No Travel"],
        s["miles_by_level"]["Severe"],
        s["miles_by_level"]["High"],
        avg if avg is not None else math.inf,
        route["duration_seconds"],
    )


def format_rank_value(reason, value):
    if reason == "shortest travel time":
        return format_duration(value)
    if reason == "lowest average risk":
        return "n/a" if value == math.inf else f"{value:.2f}"
    return f"{value:g} mi"


def recommendation_reason(best, runner_up):
    """
    One sentence explaining why `best` beat `runner_up`.

    Uses the first ranking criterion where they differ, e.g.
    "Shortest has the fewest High miles (0 mi vs 25 mi on Toll-free)."
    """
    if runner_up is None:
        return "Only one distinct route was available."
    for reason, a, b in zip(RANK_REASONS, rank_key(best), rank_key(runner_up)):
        if a != b:
            return (
                f"{best['label']} has the {reason} "
                f"({format_rank_value(reason, a)} vs {format_rank_value(reason, b)} on {runner_up['label']})."
            )
    return f"{best['label']} ties with {runner_up['label']} on every criterion."


def route_summary(route):
    """
    Human-readable one-liner for the route card, e.g.
    "996.9 mi · 16h 23m · 21 checkpoints every 50 mi · 50 mi High · peak risk High near mile 700"
    """
    s = route["risk_summary"]
    levels = s["miles_by_level"]
    parts = [
        f"{route['distance_miles']:g} mi",
        format_duration(route["duration_seconds"]),
        f"{len(route['checkpoints'])} checkpoints every {route['sample_interval_miles']} mi",
    ]
    flagged = [f"{levels[name]:g} mi {name}" for name in ("No Travel", "Severe", "High") if levels[name]]
    parts.append(", ".join(flagged) if flagged else "no High or worse weather")
    if s["peak_checkpoint_mile"] is not None:
        parts.append(f"peak risk {s['peak_risk_level']} near mile {s['peak_checkpoint_mile']:g}")
    if s["unknown_miles"]:
        parts.append(f"{s['unknown_miles']:g} mi beyond forecast range")
    return " · ".join(parts)


def serialize_route(route_id, route, departure_epoch):
    """
    Shape a scored route for the response.

    Expects `route` to already have `interval` and scored `checkpoints`
    (each with weather, risk and risk_level). Adds risk_summary and summary.
    """
    result = {
        "id": route_id,
        "label": route["label"],
        "distance_miles": round(route["distance_m"] / METERS_PER_MILE, 1),
        "duration_seconds": round(route["duration_s"]),
        "duration_text": format_duration(route["duration_s"]),
        "departure_time": to_iso(departure_epoch),
        "arrival_time": to_iso(departure_epoch + route["duration_s"]),
        "has_tolls": route["toll"],
        "sample_interval_miles": route["interval"],
        # GeoJSON order is [lon, lat]; Leaflet expects [lat, lon]
        "geometry": {
            "type": "LineString",
            "coordinates": downsample(route["coords"], MAX_GEOMETRY_POINTS),
        },
        "checkpoints": [
            {
                "index": i,
                "mile": cp["mile"],
                "lat": cp["lat"],
                "lon": cp["lon"],
                "eta": to_iso(cp["eta_epoch"]),
                "weather": cp["weather"],
                "risk": cp["risk"],
                "risk_level": cp["risk_level"],
            }
            for i, cp in enumerate(route["checkpoints"])
        ],
    }
    result["risk_summary"] = summarise_risk(result["checkpoints"])
    result["summary"] = route_summary(result)
    return result


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------

def build_heatmap(forecasts, load_lbs, start_epoch):
    """
    Weather risk over the trip corridor for each hour from now to +48h.

    Points: every checkpoint location from every route (already fetched,
    so no extra API calls).

    Shape:
    - points: [{lat, lon}]
    - frames: one per hour (0 … 48), each holding arrays that line up
              index-for-index with `points`:
                  risk_scores[i] → points[i]
                  wind_mph[i]    → points[i]
                  …

    A null value means that point has no forecast for that hour.
    """
    keys = list(forecasts)
    frames = []
    for offset in range(HEATMAP_HOURS + 1):
        epoch = start_epoch + offset * 3600
        values, wind, rain, snow = [], [], [], []
        for key in keys:
            wx = weather_at(forecasts[key], epoch)
            assessment = assess_risk(wx, load_lbs)
            values.append(assessment["score"] if assessment else None)
            wind.append(wx["wind_mph"] if wx else None)
            rain.append(wx["rain_in_hr"] if wx else None)
            snow.append(wx["snow_in_hr"] if wx else None)
        frames.append({
            "hour_offset": offset,
            "time": to_iso(epoch),
            "risk_scores": values,
            "wind_mph": wind,
            "rain_in_hr": rain,
            "snow_in_hr": snow,
        })
    return {
        "start_time": to_iso(start_epoch),
        "hours": HEATMAP_HOURS,
        "max_risk_score": NO_TRAVEL,
        "risk_levels": LEVEL_NAMES,
        "points": [{"lat": lat, "lon": lon} for lat, lon in keys],
        "frames": frames,
    }


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------

def first_error(errors, path=()):
    """
    Flatten DRF's nested serializer errors into one message for the `error` key.

    {"origin": {"lat": ["Ensure this value is less than or equal to 90."]}}
        ↓
    "origin.lat: Ensure this value is less than or equal to 90."
    """
    if isinstance(errors, dict):
        field, detail = next(iter(errors.items()))
        return first_error(detail, path if field == "non_field_errors" else (*path, field))
    if isinstance(errors, list):
        return first_error(errors[0], path)
    return f"{'.'.join(path)}: {errors}" if path else str(errors)
