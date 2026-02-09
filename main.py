from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
from collections import defaultdict

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

seat_reports = defaultdict(list)

@app.route("/submit", methods=["POST"])
def submit():
    data = request.json
    seat_reports[data["place_id"]].append(data["seats"])
    avg = sum(seat_reports[data["place_id"]]) / len(seat_reports[data["place_id"]])
    return jsonify({"average": round(avg, 1)})

@app.route("/seats/<place_id>")
def seats(place_id):
    reports = seat_reports.get(place_id, [])
    if not reports:
        return jsonify({"average": None})
    return jsonify({"average": round(sum(reports)/len(reports), 1)})

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
