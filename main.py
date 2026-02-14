from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
from collections import defaultdict
from datetime import datetime
import math
import psycopg2
import os
from urllib.parse import urlparse


app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

DATABASE_URL = os.environ.get("DATABASE_URL")

conn = psycopg2.connect(
    DATABASE_URL,
    sslmode="require"
)

conn.autocommit = True


@app.route("/submit", methods=["POST"])
def submit():
    data = request.json
    place_id = data["place_id"]
    seats = data["seats"]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO seat_reports (place_id, seats)
            VALUES (%s, %s)
            """,
            (place_id, seats),
        )

    return jsonify({"success": True})

@app.route("/seats/<place_id>", methods=["GET"])
def get_seats(place_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT seats, created_at
            FROM seat_reports
            WHERE place_id = %s
            """,
            (place_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return jsonify({"average": None})

    from datetime import datetime
    import math

    now = datetime.utcnow()
    weighted_sum = 0
    weight_total = 0

    for seats, created_at in rows:
        minutes_old = (now - created_at).total_seconds() / 60
        weight = max(0, 60 - minutes_old)

        weighted_sum += seats * weight
        weight_total += weight

    if weight_total == 0:
        return jsonify({"average": None})

    avg = math.floor(weighted_sum / weight_total)

    return jsonify({"average": avg})

@app.route("/last-update/<place_id>")
def last_update(place_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(created_at)
            FROM seat_reports
            WHERE place_id = %s
            """,
            (place_id,),
        )
        result = cur.fetchone()

    if not result or not result[0]:
        return jsonify({"minutes": None})

    from datetime import datetime

    minutes_ago = int((datetime.utcnow() - result[0]).total_seconds() / 60)

    return jsonify({"minutes": minutes_ago})



import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)

   