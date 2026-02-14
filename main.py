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

@app.route("/seats/test")
def test_seats():
    return "Seats route working"

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

@app.route("/seats/<place_id>", methods=["GET"])
def get_seats(place_id):

    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/seat_reports?place_id=eq.{place_id}",
        headers=HEADERS
    )

    if response.status_code != 200:
        return jsonify({"average": None})

    rows = response.json()

    if not rows:
        return jsonify({"average": None})

    now = datetime.utcnow()
    weighted_sum = 0
    weight_total = 0

    for row in rows:
        seats = row["seats"]
        created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))

        minutes_old = (now - created_at).total_seconds() / 60
        weight = max(0, 60 - minutes_old)

        weighted_sum += seats * weight
        weight_total += weight

    if weight_total == 0:
        return jsonify({"average": None})

    avg = math.floor(weighted_sum / weight_total)

    return jsonify({"average": avg})


# ----------------------------------------
# LAST UPDATE TIME
# ----------------------------------------

@app.route("/last-update/<place_id>")
def last_update(place_id):

    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/seat_reports?place_id=eq.{place_id}&order=created_at.desc&limit=1",
        headers=HEADERS
    )

    if response.status_code != 200:
        return jsonify({"minutes": None})

    rows = response.json()

    if not rows:
        return jsonify({"minutes": None})

    created_at = datetime.fromisoformat(rows[0]["created_at"].replace("Z", "+00:00"))
    minutes_ago = int((datetime.utcnow() - created_at).total_seconds() / 60)

    return jsonify({"minutes": minutes_ago})


# ----------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)


   