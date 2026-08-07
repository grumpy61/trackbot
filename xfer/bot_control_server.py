#!/usr/bin/env python3
"""
robot_control.py

Web-based remote control for a Raspberry Pi robot.

Run this on the Pi. The Pi should be connected to your iPhone's Personal
Hotspot. On the iPhone, open Safari and go to:

    http://<pi-ip-address>:5000
    (or, if mDNS/Bonjour is enabled, try http://raspberrypi.local:5000)

The page shows a virtual joystick, three on/off buttons, and a status line.
Joystick position and button states are sent to this server over plain
HTTP (no external dependencies, works fully offline since the hotspot has
no internet access). Your robot logic runs in a background thread
(control_loop below) and reads the latest state from `state`.

Setup on the Pi:
    pip3 install flask
    python3 robot_control.py

The page itself (HTML/CSS/JS) lives in bot_control_page.html next to this
script -- no CDN calls, so it works with zero internet access.
"""

import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# Names/labels for the three toggle buttons shown on the page.
BUTTON_IDS = ["button1", "button2", "button3"]
BUTTON_LABELS = {"button1": "BUTTON 1", "button2": "BUTTON 2", "button3": "BUTTON 3"}

# If no joystick update is received for this many seconds (e.g. phone
# screen locked, wifi dropped), the server zeroes the joystick itself
# as a safety fallback.
JOYSTICK_TIMEOUT = 0.75


class RobotState:
    """Thread-safe shared state between the Flask request handlers
    (driven by the phone) and your robot control loop (driven by the Pi)."""

    def __init__(self):
        """Initialize joystick, button, and status state to their defaults."""
        self._lock = threading.Lock()
        self.joystick_x = 0.0
        self.joystick_y = 0.0
        self.last_joystick_update = time.time()
        self.buttons = {b: False for b in BUTTON_IDS}
        self.status = "Ready"

    def set_joystick(self, x, y):
        """Store the latest joystick position, clamped to [-1, 1] on each axis."""
        with self._lock:
            self.joystick_x = max(-1.0, min(1.0, x))
            self.joystick_y = max(-1.0, min(1.0, y))
            self.last_joystick_update = time.time()

    def get_joystick(self):
        """Return (x, y, last_update_timestamp) for the current joystick position."""
        with self._lock:
            return self.joystick_x, self.joystick_y, self.last_joystick_update

    def set_button(self, name, value):
        """Set the on/off state of the named button, if it exists."""
        with self._lock:
            if name in self.buttons:
                self.buttons[name] = bool(value)

    def get_buttons(self):
        """Return a copy of the current button states, keyed by button name."""
        with self._lock:
            return dict(self.buttons)

    def set_status(self, text):
        """Call this from your robot code to update the text shown on the phone."""
        with self._lock:
            self.status = str(text)

    def get_status(self):
        """Return the current status text shown on the phone."""
        with self._lock:
            return self.status


state = RobotState()


# --------------------------------------------------------------------------
# Web page (loaded from bot_control_page.html, re-read whenever it changes
# on disk so edits show up without restarting the server)
# --------------------------------------------------------------------------

PAGE_PATH = Path(__file__).parent / "bot_control_page.html"
_page_html = PAGE_PATH.read_text(encoding="utf-8")
_page_mtime = PAGE_PATH.stat().st_mtime


def get_page_html():
    """Return the control page HTML, re-reading it from disk if the file's
    modification time has changed since the last read."""
    global _page_html, _page_mtime
    mtime = PAGE_PATH.stat().st_mtime
    if mtime != _page_mtime:
        _page_html = PAGE_PATH.read_text(encoding="utf-8")
        _page_mtime = mtime
    return _page_html


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the control page."""
    return Response(get_page_html(), mimetype="text/html")


@app.route("/control", methods=["POST"])
def control():
    """Receive a joystick position update ({"x": ..., "y": ...}) from the phone."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="x/y must be numbers"), 400
    state.set_joystick(x, y)
    return jsonify(ok=True)


@app.route("/button/<name>", methods=["POST"])
def button(name):
    """Receive an on/off update ({"value": bool}) for the named button."""
    if name not in BUTTON_IDS:
        return jsonify(ok=False, error="unknown button"), 404
    data = request.get_json(force=True, silent=True) or {}
    state.set_button(name, bool(data.get("value", False)))
    return jsonify(ok=True, buttons=state.get_buttons())


@app.route("/status")
def status():
    """Return the current status text and button states as JSON, polled by the phone."""
    return jsonify(status=state.get_status(), buttons=state.get_buttons())


# --------------------------------------------------------------------------
# Robot control loop -- replace the marked section with real GPIO/motor code.
# This runs in a background thread so the Flask server stays responsive.
# --------------------------------------------------------------------------

def control_loop():
    """Background loop: reads joystick/button state, drives the robot hardware,
    applies the joystick safety timeout, and periodically pushes status back."""
    last_status_push = 0.0

    while True:
        x, y, last_update = state.get_joystick()
        buttons = state.get_buttons()

        # Safety: if the phone stops sending updates (locked screen, wifi
        # drop, browser tab closed) zero the joystick after a short timeout.
        if (x != 0.0 or y != 0.0) and (time.time() - last_update > JOYSTICK_TIMEOUT):
            state.set_joystick(0.0, 0.0)
            x, y = 0.0, 0.0

        # ---- TODO: replace with your actual hardware control ----
        # Example differential-drive mixing:
        #   left_speed  = clamp(y + x, -1, 1)
        #   right_speed = clamp(y - x, -1, 1)
        #   set_motor_pwm(left_speed, right_speed)
        #
        # Example button use:
        #   if buttons["button1"]: turn_on_headlights()
        #   else: turn_off_headlights()
        # -----------------------------------------------------------

        # Example: periodically report state back to the phone.
        if time.time() - last_status_push > 2.0:
            state.set_status(
                "x=%.2f y=%.2f \n 1:%s 2:%s 3:%s"
                % (
                    x, y,
                    "ON" if buttons["button1"] else "off",
                    "ON" if buttons["button2"] else "off",
                    "ON" if buttons["button3"] else "off",
                )
            )
            last_status_push = time.time()

        time.sleep(0.05)


if __name__ == "__main__":
    threading.Thread(target=control_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
