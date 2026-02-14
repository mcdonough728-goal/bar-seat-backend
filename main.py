from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
from collections import defaultdict
from datetime import datetime
import math

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

seat_reports = defaultdict(list)

@app.route("/submit", methods=["POST"])
def submit():
    data = request.json
    place_id = data["place_id"]
    seats = data["seats"]

    # Store report with timestamp
    seat_reports[place_id].append({
        "seats": seats,
        "time": datetime.utcnow()
    })

    now = datetime.utcnow()
    weighted_sum = 0
    weight_total = 0

    for report in seat_reports[place_id]:
        minutes_old = (now - report["time"]).total_seconds() / 60

        # Fade reports after 60 minutes
        weight = max(0, 60 - minutes_old)

        weighted_sum += report["seats"] * weight
        weight_total += weight

    if weight_total == 0:
        return jsonify({"average": None})

    avg = math.floor(weighted_sum / weight_total)
    return jsonify({"average": avg})

@app.route("/seats/<place_id>", methods=["GET"])
def get_seats(place_id):
    reports = seat_reports.get(place_id)
    if not reports:
        return jsonify({"average": None})

    now = datetime.utcnow()
    weighted_sum = 0
    weight_total = 0

    for report in reports:
        minutes_old = (now - report["time"]).total_seconds() / 60
        weight = max(0, 60 - minutes_old)
        weighted_sum += report["seats"] * weight
        weight_total += weight

    if weight_total == 0:
        return jsonify({"average": None})

    avg = math.floor(weighted_sum / weight_total)
    return jsonify({"average": avg})

@app.route("/last-update/<place_id>")
def last_update(place_id):
    reports = seat_reports.get(place_id)
    if not reports:
        return jsonify({"minutes": None})

    last_time = max(r["time"] for r in reports)
    minutes_ago = int((datetime.utcnow() - last_time).total_seconds() / 60)

    return jsonify({"minutes": minutes_ago})

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)

   