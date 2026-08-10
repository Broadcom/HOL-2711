#!/usr/bin/env python3
"""
Simple REST server with HTTP Basic Authentication.

Endpoints:
    GET  /test          - Test the connection to the server.
    POST /receiveAlert  - Receive a JSON alert payload and display it nicely.

Requirements:
    pip install flask

Usage:
    python3 rest_server.py
"""

import threading
import json
import logging
from functools import wraps

from flask import Flask, request, jsonify, Response

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
USERNAME = "admin"
PASSWORD = "VMware123!VMware123!"
HOST = "0.0.0.0"
PORT = 5000

# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("rest_server")

app = Flask(__name__)


# ----------------------------------------------------------------------
# Basic Authentication
# ----------------------------------------------------------------------
def check_auth(username, password):
    """Validate the provided username and password."""
    return username == USERNAME and password == PASSWORD


def authenticate():
    """Send a 401 response that triggers the browser/client auth prompt."""
    return Response(
        json.dumps({"error": "Authentication required"}),
        status=401,
        mimetype="application/json",
        headers={"WWW-Authenticate": 'Basic realm="Login Required"'},
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            logger.warning("Unauthorized access attempt from %s", request.remote_addr)
            return authenticate()
        return f(*args, **kwargs)
    return decorated


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.route("/", methods=["GET"])
@requires_auth
def index():
    return jsonify({"message": "Welcome to the REST server. Use /test or /receiveAlert."}), 200

@app.route("/test", methods=["GET"])
@requires_auth
def test():
    """Simple endpoint used to test the connection to the server."""
    logger.info("Test endpoint called by user '%s'", request.authorization.username)
    return jsonify({"status": "ok", "message": "Connection successful"}), 20

@app.route("/receiveAlert", methods=["POST"])
@requires_auth
def receive_alert():
    """Receive a JSON alert body and display it nicely in the console/log."""
    if not request.is_json:
        return jsonify({"error": "Request body must be JSON (Content-Type: application/json)"}), 400

    alert = request.get_json(silent=True)
    if alert is None:
        return jsonify({"error": "Invalid or empty JSON body"}), 400

    pretty = json.dumps(alert, indent=4, ensure_ascii=False, sort_keys=True)

    print("\n" + "=" * 60)
    print(" NEW ALERT RECEIVED")
    print(" from user: {}".format(request.authorization.username))
    print("=" * 60)
    print(pretty)
    print("=" * 60 + "\n")

    logger.info("Alert received from user '%s' (%d top-level fields)",
                request.authorization.username, len(alert) if isinstance(alert, dict) else 0)

    return jsonify({"status": "ok", "message": "Alert received"}), 200


# ----------------------------------------------------------------------
# Error handlers
# ----------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(_error):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(500)
def internal_error(_error):
    return jsonify({"error": "Internal server error"}), 500


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting REST server on %s:%s", HOST, PORT)
    logger.info("Username: %s", USERNAME)
    app.run(host=HOST, port=PORT, debug=False)