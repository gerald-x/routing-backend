"""
Plain geometry helpers shared by anything that works with route lines.

Coordinate order: every line here is a list of [lon, lat] pairs,
matching GeoJSON (and therefore Geoapify) output.
"""
import math
from bisect import bisect_left

EARTH_RADIUS_MILES = 3958.8
METERS_PER_MILE = 1609.344


def haversine_miles(lat1, lon1, lat2, lon2):
    """Straight-line ("as the crow flies") distance between two points, in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def cumulative_miles(coords):
    """
    Running distance along a line, one value per coordinate.

    Example for a 3-point line:
        [0.0, 4.2, 9.7]
        ↑     ↑    ↑
        start      total length of the line
    """
    totals = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        totals.append(totals[-1] + haversine_miles(lat1, lon1, lat2, lon2))
    return totals


def point_at_distance(coords, cumulative, target):
    """
    The [lon, lat] position `target` miles along the line.

    - `cumulative` must come from cumulative_miles(coords).
    - The point usually falls between two coordinates, so it is interpolated.
    - Targets outside the line are clamped to its start or end.
    """
    if target <= 0:
        return coords[0]
    if target >= cumulative[-1]:
        return coords[-1]
    i = bisect_left(cumulative, target)
    seg = cumulative[i] - cumulative[i - 1]
    t = (target - cumulative[i - 1]) / seg if seg else 0
    (lon1, lat1), (lon2, lat2) = coords[i - 1], coords[i]
    return [lon1 + (lon2 - lon1) * t, lat1 + (lat2 - lat1) * t]


def interpolate(xs, ys, x):
    """
    Piecewise-linear lookup: given known (xs, ys) pairs, estimate y at `x`.

    - `xs` must be ascending.
    - Values outside the range are clamped to the first / last y.
    """
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_left(xs, x)
    span = xs[i] - xs[i - 1]
    t = (x - xs[i - 1]) / span if span else 0
    return ys[i - 1] + (ys[i] - ys[i - 1]) * t


def downsample(coords, max_points):
    """
    Thin a line to at most ~`max_points` coordinates, always keeping the last one.

    Long routes can have tens of thousands of points, which bloats the response
    without making the drawn line look any different on a map.
    """
    if len(coords) <= max_points:
        return coords
    stride = math.ceil(len(coords) / max_points)
    sampled = coords[::stride]
    if sampled[-1] != coords[-1]:
        sampled.append(coords[-1])
    return sampled
