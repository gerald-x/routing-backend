# routing-backend

Django backend for a truck trip planner. Given an origin, destination, departure time and load weight, it:

- Generates up to 3 truck route options (Geoapify)
- Places weather checkpoints every 10 / 25 / 50 miles along each route
- Looks up the forecast at each checkpoint for the hour the truck is expected to arrive (WeatherAPI)
- Scores weather risk using wind, rain, snow and load rules
- Recommends the best route, balancing weather risk and travel time
- Returns a 0–48 hour weather heatmap along the trip corridor

The frontend uses the response to draw routes, checkpoints, risk levels, ETAs and the heatmap on a map.

## Code structure

```
routing/                    ← repo root
├── manage.py
├── requirements.txt
├── routing/                Django project config
│   ├── settings.py         Installed apps, CORS, DRF
│   ├── settings_local.py   API keys and frontend URL (not committed, see Setup)
│   └── urls.py             Mounts core under /api/routing/
├── services/               Standalone API clients, shared by any app
│   ├── geoapify.py         Fetches and dedupes truck route options
│   ├── weatherapi.py       Fetches hourly forecasts, cached per ~1 km grid cell
│   └── geo.py              Distance and interpolation helpers for route lines
└── core/                   Trip planning app
    ├── views.py            The endpoint; reads top to bottom as the full flow
    ├── serializers.py      Validates the incoming trip request
    ├── utils.py            Risk rules, checkpoints, ranking, summaries, heatmap
    ├── urls.py
    └── tests.py
```

### Request flow

[core/views.py](core/views.py) runs these stages in order:

```
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
Stage 6: Build the 0–48h corridor heatmap
```

A few non-obvious details:

- **Route options:** Geoapify has no "alternatives" option, so the backend tries several routing strategies (balanced, shortest, toll-free, fewer turns, avoid highways), drops duplicate roads and keeps the first 3.
- **ETAs:** worked out from Geoapify's per-step distance and time, so slow city streets and fast highways are reflected rather than one average speed.
- **Checkpoint interval:** chosen automatically from each route's total distance: ≤150 mi → every 10 mi, ≤500 mi → every 25 mi, longer → every 50 mi. Each route reports the interval it used as `sample_interval_miles`.
- **Forecast range:** the WeatherAPI free plan covers 3 days. Checkpoints past that are marked `Unknown` and counted as `unknown_miles`.

### Risk rules

Wind, rain and snow are scored separately. The checkpoint's risk is the worst of the three.

| Level     | Wind (mph) | Rain (in/hr) | Snow (in/hr) |
|-----------|------------|--------------|--------------|
| Low       | < 25       | < 0.10       | < 0.5        |
| Moderate  | 25–34      | 0.10–0.25    | 0.5–1.0      |
| High      | 35–44      | 0.25–0.50    | 1.0–2.0      |
| Severe    | 45–54      | 0.50–1.00    | 2.0–3.0      |
| No Travel | ≥ 55       | > 1.00       | > 3.0        |

When a value sits on a shared boundary, it goes in the higher band.

Load rules raise the wind level:

- 45–54 mph and load > 30,000 lb → No Travel
- 35–44 mph and load > 40,000 lb → Severe

### Route recommendation

Routes are compared on these criteria in order. Each one only matters when the routes tie on the ones before it:

1. Fewest No Travel miles
2. Fewest Severe miles
3. Fewest High miles
4. Lowest average risk
5. Shortest travel time

## Setup

Requires Python 3.10+, plus API keys from [Geoapify](https://www.geoapify.com/) and [WeatherAPI](https://www.weatherapi.com/) (both have free plans).

1. Create a virtual environment and install dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. Create `routing/settings_local.py`. It is gitignored, and `settings.py` won't load without it:

   ```python
   GEOAPIFY_API_KEY = "your-geoapify-key"
   WEATHERAPI_API_KEY = "your-weatherapi-key"
   ENVIRONMENT = "development"
   FRONTEND_URL = "http://127.0.0.1:5173/"  # allowed CORS origin
   ```

3. Apply migrations:

   ```bash
   python manage.py migrate
   ```

## Running

```bash
python manage.py runserver
```

The API is then available at `http://127.0.0.1:8000/api/routing/`.

Run the tests with:

```bash
python manage.py test core
```

## API

### `POST /api/routing/trips/plan/`

Request:

```json
{
  "origin": { "address": "Chicago, IL", "lat": 41.8781, "lon": -87.6298 },
  "destination": { "address": "Denver, CO", "lat": 39.7392, "lon": -104.9903 },
  "departure_time": "2026-10-16T18:00:00.000Z",
  "load_weight_lbs": 42000
}
```

`load_weight_lbs` can be a number or a numeric string.

Response (trimmed):

```json
{
  "trip": {
    "recommended_route_id": "route-2",
    "recommendation_reason": "Shortest has the shortest travel time (16h 23m vs 16h 24m on Balanced).",
    "...": "echo of the request"
  },
  "routes": [
    {
      "id": "route-1",
      "label": "Balanced",
      "rank": 2,
      "is_recommended": false,
      "distance_miles": 1008.5,
      "duration_text": "16h 24m",
      "arrival_time": "2026-10-17T10:24:11Z",
      "summary": "1008.5 mi · 16h 24m · 22 checkpoints every 50 mi · no High or worse weather · ...",
      "geometry": { "type": "LineString", "coordinates": [[-87.62, 41.87], "..."] },
      "risk_summary": {
        "miles_by_level": { "Low": 908.5, "Moderate": 100, "High": 0, "Severe": 0, "No Travel": 0 },
        "unknown_miles": 0,
        "average_risk_score": 0.1,
        "peak_risk_level": "Moderate",
        "peak_checkpoint_mile": 100
      },
      "checkpoints": [
        {
          "index": 0,
          "mile": 0,
          "lat": 41.878,
          "lon": -87.629,
          "eta": "2026-10-16T18:00:00Z",
          "weather": { "wind_mph": 6.7, "rain_in_hr": 0.04, "snow_in_hr": 0, "temp_f": 62.5, "condition": "Light rain" },
          "risk": { "score": 0, "level": "Low", "factors": { "wind": "Low", "rain": "Low", "snow": "Low" }, "drivers": [], "load_escalated": false },
          "risk_level": "Low"
        }
      ]
    }
  ],
  "heatmap": {
    "start_time": "2026-10-16T09:00:00Z",
    "hours": 48,
    "max_risk_score": 4,
    "risk_levels": ["Low", "Moderate", "High", "Severe", "No Travel"],
    "points": [{ "lat": 41.88, "lon": -87.63 }],
    "frames": [
      { "hour_offset": 0, "time": "2026-10-16T09:00:00Z", "risk_scores": [0], "wind_mph": [6.7], "rain_in_hr": [0.04], "snow_in_hr": [0] }
    ]
  }
}
```

Notes for the frontend:

- `geometry.coordinates` are GeoJSON `[lon, lat]`. Flip them for Leaflet, which expects `[lat, lon]`.
- Risk scores run from `0` (Low) to `4` (No Travel). Use them as heatmap intensity.
- In each heatmap frame, `risk_scores[i]` (and the weather arrays) belong to `points[i]`. A `null` means that point has no forecast for that hour.

### Errors

Errors return `{"error": "<message>"}`:

- `400`: invalid request, e.g. `"destination.lat: Ensure this value is less than or equal to 90."`
- `502`: Geoapify returned no usable route
