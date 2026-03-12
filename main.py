from datetime import datetime, timedelta, timezone
import math
import os
import time
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, jsonify, redirect, request
from flask_cors import CORS
from flask_socketio import SocketIO

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")

HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
}

NEARBY_CACHE_TTL_MINUTES = 5
COOLDOWN_MINUTES = 10
RECENT_WINDOW_MINUTES = 60
MAX_SUBMIT_DISTANCE_MILES = 1.0


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def json_error(message: str, status_code: int = 400, **extra: Any):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status_code


def require_google_api_key():
    if not GOOGLE_API_KEY:
        raise RuntimeError("Missing GOOGLE_API_KEY on server")
    return GOOGLE_API_KEY


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_miles = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius_miles * c


def build_nearby_cache_key(lat: str, lng: str, radius: str) -> str:
    lat_key = f"{float(lat):.3f}"
    lng_key = f"{float(lng):.3f}"
    return f"nearby:{lat_key}:{lng_key}:{radius}"


def supabase_get(
    table: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
):
    return requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS,
        params=params,
        timeout=timeout,
    )


def supabase_post(
    table: str,
    *,
    json_body: Dict[str, Any],
    timeout: int = 10,
    merge_duplicates: bool = False,
):
    headers = HEADERS.copy()
    if merge_duplicates:
        headers["Prefer"] = "resolution=merge-duplicates"

    return requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers,
        json=json_body,
        timeout=timeout,
    )


def google_get(url: str, *, params: Dict[str, Any], timeout: int = 10):
    return requests.get(url, params=params, timeout=timeout)


def get_nearby_cache(cache_key: str):
    response = supabase_get(
        "nearby_cache",
        params={
            "select": "cache_key,payload,updated_at",
            "cache_key": f"eq.{cache_key}",
            "limit": "1",
        },
    )

    if response.status_code != 200:
        print("NEARBY CACHE GET ERROR:", response.status_code, response.text)
        return None

    rows = response.json() or []
    if not rows:
        return None

    row = rows[0]
    updated_at = row.get("updated_at")
    payload = row.get("payload")

    if not updated_at or payload is None:
        return None

    try:
        updated = parse_iso_datetime(updated_at)
    except Exception:
        return None

    if now_utc() - updated > timedelta(minutes=NEARBY_CACHE_TTL_MINUTES):
        return None

    return payload


def set_nearby_cache(cache_key: str, payload: Any):
    body = {
        "cache_key": cache_key,
        "payload": payload,
        "updated_at": now_utc().isoformat(),
    }

    response = supabase_post(
        "nearby_cache",
        json_body=body,
        merge_duplicates=True,
    )

    if response.status_code not in (200, 201):
        print("NEARBY CACHE SET ERROR:", response.status_code, response.text)


def get_cached_place_lat_lng(place_id: str):
    response = supabase_get(
        "places_cache",
        params={
            "select": "place_id,lat,lng",
            "place_id": f"eq.{place_id}",
            "limit": "1",
        },
    )

    if response.status_code != 200:
        print("PLACES_CACHE GET ERROR:", response.status_code, response.text)
        return None

    rows = response.json() or []
    if not rows:
        return None

    row = rows[0]
    return float(row["lat"]), float(row["lng"])


def upsert_cached_place_lat_lng(place_id: str, lat: float, lng: float):
    payload = {
        "place_id": place_id,
        "lat": lat,
        "lng": lng,
        "updated_at": now_utc().isoformat(),
    }

    response = supabase_post(
        "places_cache",
        json_body=payload,
        merge_duplicates=True,
    )

    if response.status_code not in (200, 201):
        print("PLACES_CACHE UPSERT ERROR:", response.status_code, response.text)


def get_place_lat_lng(place_id: str):
    cached = get_cached_place_lat_lng(place_id)
    if cached is not None:
        return cached

    google_key = require_google_api_key()

    response = google_get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={
            "place_id": place_id,
            "fields": "geometry/location",
            "key": google_key,
        },
    )
    payload = response.json()

    if payload.get("status") != "OK":
        raise RuntimeError(
            f"Places details error: {payload.get('status')} {payload.get('error_message')}"
        )

    location = payload["result"]["geometry"]["location"]
    lat = float(location["lat"])
    lng = float(location["lng"])

    try:
        upsert_cached_place_lat_lng(place_id, lat, lng)
    except Exception as error:
        print("PLACES_CACHE SAVE FAILED:", str(error))

    return lat, lng


def calculate_weighted_status(rows: List[Dict[str, Any]]):
    current_time = now_utc()
    weighted_sum = 0.0
    weight_total = 0.0

    for row in rows:
        seats = row["seats"]
        created_at = parse_iso_datetime(row["created_at"])
        minutes_old = (current_time - created_at).total_seconds() / 60
        weight = max(0, RECENT_WINDOW_MINUTES - minutes_old)

        weighted_sum += seats * weight
        weight_total += weight

    average = None if weight_total == 0 else math.floor(weighted_sum / weight_total)

    if not rows:
        return {"average": None, "minutes": None}

    latest_created_at = parse_iso_datetime(rows[0]["created_at"])
    minutes_ago = int((current_time - latest_created_at).total_seconds() / 60)

    return {"average": average, "minutes": minutes_ago}


def fetch_recent_report_for_cooldown(place_id: str, reporter_id: str):
    since = now_utc() - timedelta(minutes=COOLDOWN_MINUTES)

    response = supabase_get(
        "seat_reports",
        params={
            "select": "id,created_at",
            "place_id": f"eq.{place_id}",
            "reporter_id": f"eq.{reporter_id}",
            "created_at": f"gte.{since.isoformat()}",
            "order": "created_at.desc",
            "limit": "1",
        },
        timeout=10,
    )

    return response


def fetch_nearby_all_pages(
    lat: str,
    lng: str,
    radius: str,
    place_type: str,
    google_key: str,
    max_pages: int = 2,
):
    base_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    all_results: List[Dict[str, Any]] = []
    next_page_token = None

    for page in range(max_pages):
        if next_page_token:
            params = {
                "pagetoken": next_page_token,
                "key": google_key,
            }
        else:
            params = {
                "location": f"{lat},{lng}",
                "radius": radius,
                "type": place_type,
                "key": google_key,
            }

        response = google_get(base_url, params=params, timeout=10)
        payload = response.json()

        status = payload.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            print(f"NEARBY {place_type} page {page + 1} status:", status)
            print(
                f"NEARBY {place_type} page {page + 1} error:",
                payload.get("error_message"),
            )
            break

        all_results.extend(payload.get("results", []))

        next_page_token = payload.get("next_page_token")
        if not next_page_token:
            break

        time.sleep(2)

    return all_results


@app.route("/submit", methods=["POST"])
def submit():
    data = request.json or {}

    place_id = data.get("place_id")
    seats = data.get("seats")
    has_bar_seating = data.get("has_bar_seating")
    reporter_id = data.get("reporter_id")
    reporter_lat = data.get("reporter_lat")
    reporter_lng = data.get("reporter_lng")

    if not place_id:
        return json_error("Missing place_id", 400)

    if reporter_id is None or str(reporter_id).strip() == "":
        return json_error("Missing reporter_id", 400)

    if seats is None:
        return json_error("Missing seats", 400)

    try:
        seats_num = int(seats)
        if seats_num < 0:
            return json_error("Seats must be 0 or greater", 400)
    except Exception:
        return json_error("Seats must be a number", 400)

    if reporter_lat is None or reporter_lng is None:
        return (
            jsonify(
                {
                    "error": "missing_location",
                    "message": "Please enable location to submit a report.",
                }
            ),
            400,
        )

    try:
        reporter_lat = float(reporter_lat)
        reporter_lng = float(reporter_lng)
    except Exception:
        return (
            jsonify(
                {
                    "error": "bad_location",
                    "message": "Invalid location.",
                }
            ),
            400,
        )

    try:
        place_lat, place_lng = get_place_lat_lng(place_id)
        distance_miles = haversine_miles(
            reporter_lat,
            reporter_lng,
            place_lat,
            place_lng,
        )

        if distance_miles > MAX_SUBMIT_DISTANCE_MILES:
            return (
                jsonify(
                    {
                        "error": "too_far",
                        "message": (
                            f"You must be within {MAX_SUBMIT_DISTANCE_MILES} miles "
                            "to submit a report."
                        ),
                        "distance_miles": distance_miles,
                    }
                ),
                403,
            )
    except Exception as error:
        print("PROXIMITY CHECK FAILED:", repr(error))
        print("place_id:", place_id)
        return (
            jsonify(
                {
                    "error": "proximity_check_failed",
                    "message": "Could not verify location. Try again.",
                }
            ),
            503,
        )

    cooldown_response = fetch_recent_report_for_cooldown(place_id, reporter_id)

    if cooldown_response.status_code != 200:
        print(
            "COOLDOWN CHECK ERROR:",
            cooldown_response.status_code,
            cooldown_response.text,
        )
    else:
        recent = cooldown_response.json() or []
        if recent:
            return (
                jsonify(
                    {
                        "error": "cooldown",
                        "message": (
                            f"Please wait {COOLDOWN_MINUTES} minutes before "
                            "reporting again for this place."
                        ),
                    }
                ),
                429,
            )

    payload = {
        "place_id": place_id,
        "seats": seats_num,
        "reporter_id": reporter_id,
    }

    if has_bar_seating is not None:
        payload["has_bar_seating"] = bool(has_bar_seating)

    response = supabase_post(
        "seat_reports",
        json_body=payload,
        timeout=10,
    )

    if response.status_code != 201:
        return jsonify({"error": response.text}), 400

    return jsonify({"success": True})


@app.route("/status/<path:place_id>", methods=["GET"])
def status(place_id):
    latest_response = supabase_get(
        "seat_reports",
        params={
            "place_id": f"eq.{place_id}",
            "order": "created_at.desc",
            "limit": "1",
        },
    )

    if latest_response.status_code != 200:
        return jsonify({"average": None, "minutes": None}), 200

    latest_rows = latest_response.json() or []
    if not latest_rows:
        return jsonify({"average": None, "minutes": None}), 200

    all_rows_response = supabase_get(
        "seat_reports",
        params={
            "place_id": f"eq.{place_id}",
        },
    )

    if all_rows_response.status_code != 200:
        return jsonify({"average": None, "minutes": None}), 200

    rows = all_rows_response.json() or []
    result = calculate_weighted_status(rows)
    return jsonify(result)


@app.route("/status-batch", methods=["POST"])
def status_batch():
    data = request.json or {}
    place_ids = data.get("place_ids", [])

    if not isinstance(place_ids, list) or len(place_ids) == 0:
        return jsonify({"statuses": {}})

    quoted = ",".join([f'"{pid}"' for pid in place_ids])

    response = supabase_get(
        "seat_reports",
        params={
            "place_id": f"in.({quoted})",
            "select": "place_id,seats,created_at,has_bar_seating",
            "order": "created_at.desc",
        },
        timeout=10,
    )

    if response.status_code != 200:
        return (
            jsonify(
                {
                    "where": "supabase batch GET /seat_reports",
                    "status_code": response.status_code,
                    "response_text": response.text,
                }
            ),
            500,
        )

    rows = response.json() or []
    current_time = now_utc()

    statuses = {
        pid: {
            "average": None,
            "minutes": None,
            "recent_reports": 0,
            "has_bar_seating": None,
        }
        for pid in place_ids
    }

    weighted = {
        pid: {
            "weighted_sum": 0.0,
            "weight_total": 0.0,
        }
        for pid in place_ids
    }

    for row in rows:
        place_id = row["place_id"]
        if place_id not in statuses:
            continue

        created_at = parse_iso_datetime(row["created_at"])
        minutes_old = (current_time - created_at).total_seconds() / 60

        if statuses[place_id]["minutes"] is None:
            statuses[place_id]["minutes"] = int(minutes_old)

        if (
            statuses[place_id]["has_bar_seating"] is None
            and row.get("has_bar_seating") is not None
        ):
            statuses[place_id]["has_bar_seating"] = bool(row.get("has_bar_seating"))

        if minutes_old <= RECENT_WINDOW_MINUTES:
            statuses[place_id]["recent_reports"] += 1

        weight = max(0, RECENT_WINDOW_MINUTES - minutes_old)
        if weight > 0:
            seats = row["seats"]
            weighted[place_id]["weighted_sum"] += seats * weight
            weighted[place_id]["weight_total"] += weight

    for place_id in place_ids:
        weight_total = weighted[place_id]["weight_total"]
        if weight_total > 0:
            statuses[place_id]["average"] = math.floor(
                weighted[place_id]["weighted_sum"] / weight_total
            )

    return jsonify({"statuses": statuses})


@app.route("/latest/<path:place_id>", methods=["GET"])
def latest(place_id):
    response = supabase_get(
        "seat_reports",
        params={
            "place_id": f"eq.{place_id}",
            "select": "seats,created_at",
            "order": "created_at.desc",
            "limit": "1",
        },
    )

    if response.status_code != 200:
        return jsonify({"seats": None, "minutes": None}), 200

    rows = response.json() or []
    if not rows:
        return jsonify({"seats": None, "minutes": None}), 200

    created_at = parse_iso_datetime(rows[0]["created_at"])
    minutes_ago = int((now_utc() - created_at).total_seconds() / 60)

    return jsonify({"seats": rows[0]["seats"], "minutes": minutes_ago})


@app.route("/last-update/<path:place_id>")
def last_update(place_id):
    response = supabase_get(
        "seat_reports",
        params={
            "place_id": f"eq.{place_id}",
            "order": "created_at.desc",
            "limit": "1",
        },
    )

    if response.status_code != 200:
        return (
            jsonify(
                {
                    "where": "supabase GET /seat_reports last-update",
                    "status_code": response.status_code,
                    "response_text": response.text,
                }
            ),
            500,
        )

    rows = response.json() or []
    if not rows:
        return jsonify({"minutes": None})

    created_at = parse_iso_datetime(rows[0]["created_at"])
    minutes_ago = int((now_utc() - created_at).total_seconds() / 60)

    return jsonify({"minutes": minutes_ago})


@app.route("/bar-seating-batch", methods=["POST"])
def bar_seating_batch():
    data = request.json or {}
    place_ids = data.get("place_ids") or []

    if not isinstance(place_ids, list) or not place_ids:
        return jsonify({"votes": {}})

    quoted = ",".join([f'"{pid}"' for pid in place_ids])

    response = supabase_get(
        "place_bar_seating_votes",
        params={
            "place_id": f"in.({quoted})",
        },
    )

    if response.status_code != 200:
        return jsonify({"error": response.text}), 500

    rows = response.json() or []
    output = {}

    for row in rows:
        place_id = row["place_id"]
        output[place_id] = {
            "yes": row.get("yes_votes", 0) or 0,
            "no": row.get("no_votes", 0) or 0,
            "total": row.get("total_votes", 0) or 0,
        }

    return jsonify({"votes": output})


@app.route("/places-nearby", methods=["GET"])
def places_nearby():
    lat = request.args.get("lat")
    lng = request.args.get("lng")
    radius = request.args.get("radius", "8000")

    if not lat or not lng:
        return json_error("missing coordinates", 400)

    try:
        google_key = require_google_api_key()
    except RuntimeError as error:
        return json_error(str(error), 500)

    cache_key = build_nearby_cache_key(lat, lng, radius)

    try:
        cached = get_nearby_cache(cache_key)
        if cached is not None:
            print("PLACES_NEARBY CACHE HIT:", cache_key)
            return jsonify(cached), 200

        print("PLACES_NEARBY CACHE MISS:", cache_key)

        restaurants = fetch_nearby_all_pages(
            lat, lng, radius, "restaurant", google_key, max_pages=3
        )
        bars = fetch_nearby_all_pages(
            lat, lng, radius, "bar", google_key, max_pages=2
        )

        payload = {
            "restaurants": restaurants,
            "bars": bars,
        }

        try:
            set_nearby_cache(cache_key, payload)
        except Exception as error:
            print("NEARBY CACHE SAVE FAILED:", repr(error))

        return jsonify(payload), 200
    except Exception as error:
        return jsonify({"error": f"places_nearby failed: {repr(error)}"}), 500


@app.route("/places-autocomplete", methods=["GET"])
def places_autocomplete():
    try:
        google_key = require_google_api_key()
    except RuntimeError as error:
        return json_error(str(error), 500)

    query = request.args.get("input", "").strip()
    if len(query) < 2:
        return jsonify({"predictions": []})

    lat = request.args.get("lat")
    lng = request.args.get("lng")
    radius = request.args.get("radius", "30000")

    params = {
        "input": query,
        "types": "establishment",
        "key": google_key,
        "strictbounds": "false",
    }

    if lat and lng:
        params["location"] = f"{lat},{lng}"
        params["radius"] = radius

    response = google_get(
        "https://maps.googleapis.com/maps/api/place/autocomplete/json",
        params=params,
        timeout=10,
    )
    payload = response.json()

    return (
        jsonify(
            {
                "status": payload.get("status"),
                "predictions": payload.get("predictions", []),
                "error_message": payload.get("error_message"),
            }
        ),
        200,
    )


@app.route("/place-details", methods=["GET"])
def place_details():
    try:
        google_key = require_google_api_key()
    except RuntimeError as error:
        return json_error(str(error), 500)

    place_id = request.args.get("place_id", "").strip()
    if not place_id:
        return json_error("missing place_id", 400)

    fields = request.args.get(
        "fields",
        "place_id,name,vicinity,geometry,opening_hours,types,photos,rating,user_ratings_total,price_level",
    )

    response = google_get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={
            "place_id": place_id,
            "fields": fields,
            "key": google_key,
        },
        timeout=10,
    )
    payload = response.json()

    return (
        jsonify(
            {
                "status": payload.get("status"),
                "result": payload.get("result"),
                "error_message": payload.get("error_message"),
            }
        ),
        200,
    )


@app.route("/place-photo", methods=["GET"])
def place_photo():
    try:
        google_key = require_google_api_key()
    except RuntimeError as error:
        return json_error(str(error), 500)

    ref = request.args.get("ref", "").strip()
    if not ref:
        return json_error("missing ref", 400)

    maxwidth = request.args.get("maxwidth", "800")
    url = "https://maps.googleapis.com/maps/api/place/photo"
    query_string = f"?maxwidth={maxwidth}&photoreference={ref}&key={google_key}"
    return redirect(url + query_string, code=302)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)


   