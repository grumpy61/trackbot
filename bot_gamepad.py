#!/usr/bin/env python3
"""
bot_gamepad.py

Bluetooth gamepad input for trackbot, built on evdev (see gamepad_test.py for the
device-discovery approach this is based on). BTGamepadController wraps device discovery and
non-blocking event polling so it can be ticked from run_trackbot.py's main loop
without blocking on camera frames. If no gamepad is found (or it disconnects),
poll() periodically re-scans and connects automatically once one shows up.
"""

import select
import time
from dataclasses import dataclass, field

import evdev
from evdev import categorize, ecodes

RECONNECT_INTERVAL_S = 1.5  # how often to re-scan for a gamepad while disconnected


def find_gamepads():
    """Scan system input devices for gamepad/joystick-like devices."""
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    gamepads = []
    for device in devices:
        lower_name = device.name.lower()
        if any(kw in lower_name for kw in ("gamepad", "cinco", "controller", "joystick")):
            gamepads.append(device)
    return gamepads


@dataclass
class GamepadState:
    """Latest known state: buttons is {code: pressed_bool}, axes is {code: value}."""
    buttons: dict = field(default_factory=dict)
    axes: dict = field(default_factory=dict)


class BTGamepadController:
    """Bluetooth gamepad input. poll() is non-blocking: it drains any pending
    events and returns the updated GamepadState, or None if no gamepad is
    connected or no new events arrived since the last poll(). While
    disconnected, poll() re-scans for a gamepad every RECONNECT_INTERVAL_S
    seconds and connects automatically once one shows up."""

    def __init__(self, verbose=False):
        self.device = None
        self.state = GamepadState()
        self.verbose = verbose  # print each raw key/axis event to the console as it arrives
        self._last_reconnect_attempt = time.monotonic()
        self._try_connect()
        if self.device is None:
            print(
                "[BTGamepadController] No gamepad found -- input disabled. "
                f"Will keep checking every {RECONNECT_INTERVAL_S:g}s."
            )

    def _try_connect(self):
        gamepads = find_gamepads()
        if gamepads:
            self.device = gamepads[0]
            print(f"[BTGamepadController] Connected to: {self.device.name} ({self.device.path})")

    def poll(self):
        if self.device is None:
            now = time.monotonic()
            if now - self._last_reconnect_attempt >= RECONNECT_INTERVAL_S:
                self._last_reconnect_attempt = now
                self._try_connect()
            return None

        readable, _, _ = select.select([self.device.fd], [], [], 0)
        if not readable:
            return None

        try:
            for event in self.device.read():
                if event.type == ecodes.EV_KEY:
                    self.state.buttons[event.code] = event.value != 0
                    if self.verbose:
                        keycode = categorize(event).keycode
                        state = "PRESSED" if event.value == 1 else "RELEASED" if event.value == 0 else "HELD"
                        print(f"[BUTTON] Code: {event.code} ({keycode}) | State: {state}")
                elif event.type == ecodes.EV_ABS:
                    self.state.axes[event.code] = event.value
                    if self.verbose:
                        print(f"[AXIS/DPAD] Axis Code: {event.code} | Value: {event.value}")
        except BlockingIOError:
            pass
        except OSError as e:
            print(f"[BTGamepadController] Lost connection to gamepad: {e}")
            self.device = None
            return None

        return self.state


if __name__ == "__main__":
    controller = BTGamepadController(verbose=True)

    print("Move sticks / press buttons to test (will keep waiting for a gamepad "
          "to connect if none is found yet). Press Ctrl+C to exit.\n")
    try:
        while True:
            controller.poll()
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
