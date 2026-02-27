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

    payload = {
        "place_id": place_id,
        "seats": seats
    }

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
# LAST UPDATE TIME
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


   