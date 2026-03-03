from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
from datetime import datetime
import math
import os
import requests

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

# ----------------------------------------
# SUBMIT SEAT REPORT
# ----------------------------------------

@app.route("/submit", methods=["POST"])
def submit():
    data = request.json
    place_id = data["place_id"]
    seats = data["seats"]
    has_bar_seating = data.get("has_bar_seating")  # NEW

    payload = {
        "place_id": place_id,
        "seats": seats,
    }

    # Only include if user answered Yes/No
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)


   