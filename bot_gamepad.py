#!/usr/bin/env python3
"""
bot_gamepad.py

Bluetooth gamepad input for trackbot, built on evdev (see gamepad_test.py for the
device-discovery approach this is based on). BTGamepadController wraps device discovery and
non-blocking event polling so it can be ticked from run_trackbot.py's main loop
without blocking on camera frames.
"""

import select
from dataclasses import dataclass, field

import evdev
from evdev import ecodes


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
    connected or no new events arrived since the last poll()."""

    def __init__(self):
        gamepads = find_gamepads()
        self.device = gamepads[0] if gamepads else None
        self.state = GamepadState()
        if self.device is None:
            print("[BTGamepadController] No gamepad found -- input disabled.")
        else:
            print(f"[BTGamepadController] Connected to: {self.device.name} ({self.device.path})")

    def poll(self):
        if self.device is None:
            return None

        readable, _, _ = select.select([self.device.fd], [], [], 0)
        if not readable:
            return None

        try:
            for event in self.device.read():
                if event.type == ecodes.EV_KEY:
                    self.state.buttons[event.code] = event.value != 0
                elif event.type == ecodes.EV_ABS:
                    self.state.axes[event.code] = event.value
        except BlockingIOError:
            pass
        except OSError as e:
            print(f"[BTGamepadController] Lost connection to gamepad: {e}")
            self.device = None
            return None

        return self.state


if __name__ == "__main__":
    import time

    controller = BTGamepadController()
    if controller.device is None:
        raise SystemExit(1)

    print("Move sticks / press buttons to test. Press Ctrl+C to exit.\n")
    try:
        while True:
            if controller.poll() is not None:
                print(f"buttons={controller.state.buttons} axes={controller.state.axes}")
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
