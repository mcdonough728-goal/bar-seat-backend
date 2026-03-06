from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from flask_socketio import SocketIO
from datetime import datetime, timedelta, timezone
import math
import os
import requests
import time

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json"
}

def haversine_miles(lat1, lon1, lat2, lon2):
    # Earth radius in miles
    R = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def get_place_lat_lng(place_id: str):
    # 1) Try cache first (fast, no Google quota)
    cached = get_cached_place_lat_lng(place_id)
    if cached is not None:
        return cached

    # 2) Not cached -> call Google once
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GOOGLE_API_KEY on server")

    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "geometry/location",
        "key": api_key,
    }
    r = requests.get(url, params=params, timeout=10)
    j = r.json()

    if j.get("status") != "OK":
        raise RuntimeError(f"Places details error: {j.get('status')} {j.get('error_message')}")

    loc = j["result"]["geometry"]["location"]
    lat = float(loc["lat"])
    lng = float(loc["lng"])

    # 3) Store in cache for future (best-effort)
    try:
        upsert_cached_place_lat_lng(place_id, lat, lng)
    except Exception as e:
        print("PLACES_CACHE SAVE FAILED:", str(e))

    return lat, lng

COOLDOWN_MINUTES = 10

def get_cached_place_lat_lng(place_id: str):
    # GET one row by place_id
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/places_cache",
        headers=HEADERS,
        params={
            "select": "place_id,lat,lng",
            "place_id": f"eq.{place_id}",
            "limit": "1",
        },
        timeout=10,
    )

    if res.status_code != 200:
        print("PLACES_CACHE GET ERROR:", res.status_code, res.text)
        return None

    rows = res.json() or []
    if not rows:
        return None

    row = rows[0]
    return float(row["lat"]), float(row["lng"])


def upsert_cached_place_lat_lng(place_id: str, lat: float, lng: float):
    # Upsert (insert if new, update if exists)
    payload = {
        "place_id": place_id,
        "lat": lat,
        "lng": lng,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/places_cache",
        headers={**HEADERS, "Prefer": "resolution=merge-duplicates"},
        json=payload,
        timeout=10,
    )

    if res.status_code not in (201, 200):
        print("PLACES_CACHE UPSERT ERROR:", res.status_code, res.text)

# ----------------------------------------
# SUBMIT SEAT REPORT
# ----------------------------------------

@app.route("/submit", methods=["POST"])
def submit():
    data = request.json or {}

    place_id = data.get("place_id")
    seats = data.get("seats")
    has_bar_seating = data.get("has_bar_seating")
    reporter_id = data.get("reporter_id")

    reporter_lat = data.get("reporter_lat")
    reporter_lng = data.get("reporter_lng")

    # Basic validation
    if not place_id:
        return jsonify({"error": "Missing place_id"}), 400

    if reporter_id is None or str(reporter_id).strip() == "":
        return jsonify({"error": "Missing reporter_id"}), 400

    if seats is None:
        return jsonify({"error": "Missing seats"}), 400

    # Ensure seats is a non-negative number
    try:
        seats_num = int(seats)
        if seats_num < 0:
            return jsonify({"error": "Seats must be 0 or greater"}), 400
    except Exception:
        return jsonify({"error": "Seats must be a number"}), 400

    # ----------------------------------------
    # Proximity check (must be near the place)
    # ----------------------------------------
    MAX_DISTANCE_MILES = 1.0

    if reporter_lat is None or reporter_lng is None:
        return jsonify({
            "error": "missing_location",
            "message": "Please enable location to submit a report."
        }), 400

    try:
        reporter_lat = float(reporter_lat)
        reporter_lng = float(reporter_lng)
    except Exception:
        return jsonify({
            "error": "bad_location",
            "message": "Invalid location."
        }), 400

    try:
        place_lat, place_lng = get_place_lat_lng(place_id)
        dist = haversine_miles(reporter_lat, reporter_lng, place_lat, place_lng)

        if dist > MAX_DISTANCE_MILES:
            return jsonify({
                "error": "too_far",
                "message": f"You must be within {MAX_DISTANCE_MILES} miles to submit a report.",
                "distance_miles": dist
            }), 403

    except Exception as e:
        print("PROXIMITY CHECK FAILED:", repr(e))
        print("place_id:", place_id)
        return jsonify({
            "error": "proximity_check_failed",
            "message": "Could not verify location. Try again."
        }), 503

    # ----------------------------------------
    # Cooldown check: has this reporter_id already submitted for this place recently?
    # ----------------------------------------
    since = datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MINUTES)

    # Query Supabase (REST) for a recent report from same reporter_id + place_id
    check_params = {
        "select": "id,created_at",
        "place_id": f"eq.{place_id}",
        "reporter_id": f"eq.{reporter_id}",
        "created_at": f"gte.{since.isoformat()}",
        "order": "created_at.desc",
        "limit": "1",
    }

    check_res = requests.get(
        f"{SUPABASE_URL}/rest/v1/seat_reports",
        headers=HEADERS,
        params=check_params,
    )

    if check_res.status_code != 200:
        # If the check fails, we can still allow submit or block.
        # Safer UX: allow submit to avoid breaking reports when Supabase hiccups.
        print("COOLDOWN CHECK ERROR:", check_res.status_code, check_res.text)
    else:
        recent = check_res.json() or []
        if len(recent) > 0:
            return jsonify({
                "error": "cooldown",
                "message": f"Please wait {COOLDOWN_MINUTES} minutes before reporting again for this place."
            }), 429

    # ----------------------------------------
    # Insert report
    # ----------------------------------------
    payload = {
        "place_id": place_id,
        "seats": seats_num,
        "reporter_id": reporter_id,  # ✅ NEW
    }

    if has_bar_seating is not None:
        payload["has_bar_seating"] = bool(has_bar_seating)

    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/seat_reports",
        headers=HEADERS,
        json=payload
    )

    if response.status_code != 201:
        return jsonify({"error": response.text}), 400

    return jsonify({"success": True})

# ----------------------------------------
# GET WEIGHTED AVERAGE
# ----------------------------------------

@app.route("/status/<path:place_id>", methods=["GET"])
def status(place_id):
    
    latest = requests.get(
        f"{SUPABASE_URL}/rest/v1/seat_reports?place_id=eq.{place_id}&order=created_at.desc&limit=1",
        headers=HEADERS
    )
    if latest.status_code != 200:
        return jsonify({"average": None, "minutes": None}), 200
    latest_rows = latest.json()
    if not latest_rows:
        return jsonify({"average": None, "minutes": None}), 200

    all_rows_res = requests.get(
        f"{SUPABASE_URL}/rest/v1/seat_reports?place_id=eq.{place_id}",
        headers=HEADERS
    )
    if all_rows_res.status_code != 200:
        return jsonify({"average": None, "minutes": None}), 200
    rows = all_rows_res.json()

    from datetime import timezone
    now = datetime.now(timezone.utc)

    weighted_sum = 0
    weight_total = 0
    for row in rows:
        seats = row["seats"]
        created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        minutes_old = (now - created_at).total_seconds() / 60
        weight = max(0, 60 - minutes_old)
        weighted_sum += seats * weight
        weight_total += weight

    avg = None if weight_total == 0 else math.floor(weighted_sum / weight_total)

    latest_created_at = datetime.fromisoformat(latest_rows[0]["created_at"].replace("Z", "+00:00"))
    minutes_ago = int((now - latest_created_at).total_seconds() / 60)

    return jsonify({"average": avg, "minutes": minutes_ago})

# ----------------------------------------
# STATUS BATCH
# ----------------------------------------

@app.route("/status-batch", methods=["POST"])
def status_batch():
    data = request.json or {}
    place_ids = data.get("place_ids", [])

    if not isinstance(place_ids, list) or len(place_ids) == 0:
        return jsonify({"statuses": {}})

    quoted = ",".join([f'"{pid}"' for pid in place_ids])

    url = (
        f"{SUPABASE_URL}/rest/v1/seat_reports"
        f"?place_id=in.({quoted})"
        f"&select=place_id,seats,created_at"
        f"&order=created_at.desc"
    )

    resp = requests.get(url, headers=HEADERS)
    if resp.status_code != 200:
        return jsonify({
            "where": "supabase batch GET /seat_reports",
            "status_code": resp.status_code,
            "response_text": resp.text
        }), 500

    rows = resp.json()

    grouped = {}
    for r in rows:
        pid = r["place_id"]
        grouped.setdefault(pid, []).append(r)

    from datetime import timezone
    now = datetime.now(timezone.utc)

    statuses = {}
    for pid in place_ids:
        pid_rows = grouped.get(pid, [])
        if not pid_rows:
            statuses[pid] = {"average": None, "minutes": None}
            continue

        latest_created_at = datetime.fromisoformat(
            pid_rows[0]["created_at"].replace("Z", "+00:00")
        )
        minutes_ago = int((now - latest_created_at).total_seconds() / 60)

        weighted_sum = 0
        weight_total = 0

        for row in pid_rows:
            seats = row["seats"]
            created_at = datetime.fromisoformat(
                row["created_at"].replace("Z", "+00:00")
            )
            minutes_old = (now - created_at).total_seconds() / 60
            weight = max(0, 60 - minutes_old)

            weighted_sum += seats * weight
            weight_total += weight

        avg = None if weight_total == 0 else math.floor(weighted_sum / weight_total)

        statuses[pid] = {"average": avg, "minutes": minutes_ago}

    return jsonify({"statuses": statuses})

# ----------------------------------------
# LAST UPDATE TIME
# ----------------------------------------

@app.route("/latest/<path:place_id>", methods=["GET"])
def latest(place_id):
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/seat_reports?place_id=eq.{place_id}&select=seats,created_at&order=created_at.desc&limit=1",
        headers=HEADERS
    )

    if response.status_code != 200:
        return jsonify({"seats": None, "minutes": None}), 200

    rows = response.json()
    if not rows:
        return jsonify({"seats": None, "minutes": None})

    from datetime import timezone
    now = datetime.now(timezone.utc)
    created_at = datetime.fromisoformat(rows[0]["created_at"].replace("Z", "+00:00"))
    minutes_ago = int((now - created_at).total_seconds() / 60)

    return jsonify({"seats": rows[0]["seats"], "minutes": minutes_ago})

# ----------------------------------------
# LATEST ENDPOINT
# ----------------------------------------

@app.route("/last-update/<path:place_id>")
def last_update(place_id):

    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/seat_reports?place_id=eq.{place_id}&order=created_at.desc&limit=1",
        headers=HEADERS
    )

    if response.status_code != 200:
        return jsonify({
            "where": "supabase GET /seat_reports last-update",
            "status_code": response.status_code,
            "response_text": response.text
        }), 500

    rows = response.json()

    if not rows:
        return jsonify({"minutes": None})

    from datetime import timezone
    created_at = datetime.fromisoformat(
        rows[0]["created_at"].replace("Z", "+00:00")
    )

    minutes_ago = int(
        (datetime.now(timezone.utc) - created_at).total_seconds() / 60
    )

    return jsonify({"minutes": minutes_ago})

# ----------------------------------------
# Bar Seating Batch
# ----------------------------------------

@app.route("/bar-seating-batch", methods=["POST"])
def bar_seating_batch():
    data = request.json or {}
    place_ids = data.get("place_ids") or []
    if not isinstance(place_ids, list) or not place_ids:
        return jsonify({"votes": {}})

    in_list = ",".join(place_ids)

    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/place_bar_seating_votes?place_id=in.({in_list})",
        headers=HEADERS,
    )

    if resp.status_code != 200:
        return jsonify({"error": resp.text}), 500

    rows = resp.json()
    out = {}
    for r in rows:
        pid = r["place_id"]
        out[pid] = {
            "yes": r.get("yes_votes", 0) or 0,
            "no": r.get("no_votes", 0) or 0,
            "total": r.get("total_votes", 0) or 0,
        }

    return jsonify({"votes": out})

# ----------------------------------------
# Places Nearby Endpoint
# ----------------------------------------

@app.route("/places-nearby", methods=["GET"])
def places_nearby():
    lat = request.args.get("lat")
    lng = request.args.get("lng")
    radius = request.args.get("radius", "8000")

    if not lat or not lng:
        return jsonify({"error": "missing coordinates"}), 400

    google_key = os.environ.get("GOOGLE_API_KEY")
    if not google_key:
        return jsonify({"error": "Missing GOOGLE_API_KEY on server"}), 500

    base_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"

    try:
        restaurant_res = requests.get(
            base_url,
            params={
                "location": f"{lat},{lng}",
                "radius": radius,
                "type": "restaurant",
                "key": google_key,
            },
            timeout=10,
        )
        bar_res = requests.get(
            base_url,
            params={
                "location": f"{lat},{lng}",
                "radius": radius,
                "type": "bar",
                "key": google_key,
            },
            timeout=10,
        )

        restaurant_json = restaurant_res.json()
        bar_json = bar_res.json()

        print("PLACES_NEARBY restaurant status:", restaurant_json.get("status"))
        print("PLACES_NEARBY bar status:", bar_json.get("status"))
        print("PLACES_NEARBY restaurant error:", restaurant_json.get("error_message"))
        print("PLACES_NEARBY bar error:", bar_json.get("error_message"))

        return jsonify({
            "restaurants": restaurant_json.get("results", []),
            "bars": bar_json.get("results", []),
            "restaurant_status": restaurant_json.get("status"),
            "bar_status": bar_json.get("status"),
            "restaurant_error": restaurant_json.get("error_message"),
            "bar_error": bar_json.get("error_message"),
        }), 200

    except Exception as e:
        return jsonify({"error": f"places_nearby failed: {repr(e)}"}), 500

# ----------------------------------------
# Places Autocomplete
# ----------------------------------------

@app.route("/places-autocomplete", methods=["GET"])
def places_autocomplete():
    google_key = os.environ.get("GOOGLE_API_KEY")
    if not google_key:
        return jsonify({"error": "Missing GOOGLE_API_KEY on server"}), 500

    q = request.args.get("input", "").strip()
    if len(q) < 2:
        return jsonify({"predictions": []})

    lat = request.args.get("lat")
    lng = request.args.get("lng")
    radius = request.args.get("radius", "30000")

    url = "https://maps.googleapis.com/maps/api/place/autocomplete/json"
    params = {
        "input": q,
        "types": "establishment",
        "key": google_key,
        "strictbounds": "false",
    }

    # Bias toward user's area, but don't hard-restrict
    if lat and lng:
        params["location"] = f"{lat},{lng}"
        params["radius"] = radius

    r = requests.get(url, params=params, timeout=10)
    j = r.json()

    return jsonify({
        "status": j.get("status"),
        "predictions": j.get("predictions", []),
        "error_message": j.get("error_message"),
    }), 200

# ----------------------------------------
# Place Details
# ----------------------------------------

@app.route("/place-details", methods=["GET"])
def place_details():
    google_key = os.environ.get("GOOGLE_API_KEY")
    if not google_key:
        return jsonify({"error": "Missing GOOGLE_API_KEY on server"}), 500

    place_id = request.args.get("place_id", "").strip()
    if not place_id:
        return jsonify({"error": "missing place_id"}), 400

    fields = request.args.get(
        "fields",
        "place_id,name,vicinity,geometry,opening_hours,types,photos,rating,user_ratings_total,price_level"
    )

    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": fields,
        "key": google_key,
    }

    r = requests.get(url, params=params, timeout=10)
    j = r.json()

    return jsonify({
        "status": j.get("status"),
        "result": j.get("result"),
        "error_message": j.get("error_message"),
    }), 200

# ----------------------------------------
# Place Photos
# ----------------------------------------

@app.route("/place-photo", methods=["GET"])
def place_photo():
    google_key = os.environ.get("GOOGLE_API_KEY")
    if not google_key:
        return jsonify({"error": "Missing GOOGLE_API_KEY on server"}), 500

    ref = request.args.get("ref", "").strip()
    if not ref:
        return jsonify({"error": "missing ref"}), 400

    maxwidth = request.args.get("maxwidth", "800")

    # Google Photos endpoint returns an image via redirect.
    # We redirect the client to Google, but the key stays server-side.
    url = "https://maps.googleapis.com/maps/api/place/photo"
    qs = f"?maxwidth={maxwidth}&photoreference={ref}&key={google_key}"
    return redirect(url + qs, code=302)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)


   