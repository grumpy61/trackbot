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
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import evdev
from evdev import categorize, ecodes

RECONNECT_INTERVAL_S = 1.5  # how often poll() re-scans for an already-connected gamepad's device node
HELD_REPEAT_INTERVAL_S = 0.5  # a HELD event counts as a repeat at most this often, per key

# Most BT gamepads don't reconnect themselves after dropping out of range --
# BlueZ just leaves a paired device disconnected until something explicitly
# asks it to reconnect. So besides re-scanning for an already-connected
# device's evdev node (cheap, fast, done in poll()), a background thread
# periodically runs `bluetoothctl connect <mac>` against paired devices that
# look like a gamepad, to actively re-establish the link. A connect attempt
# against an out-of-range device takes ~5s (observed) to fail, so this must
# never run on poll()'s thread -- see _ReconnectWorker.
BT_RECONNECT_INTERVAL_S = 8.0  # how often the background thread retries a connect
BT_CONNECT_TIMEOUT_S = 10.0  # per-attempt subprocess timeout, generous vs. the ~5s observed


def _ensure_bluetooth_powered_on():
    """Best-effort: unblock Bluetooth via rfkill and power the adapter on via
    bluetoothctl if it's currently off, so a paired gamepad can still connect
    even if Bluetooth wasn't left on at the system level (e.g. after a reboot).
    Failures here (tools missing, no adapter, etc.) are logged but don't stop
    the program -- poll() just keeps failing to find a gamepad, as it already
    does today when none is available."""
    try:
        subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[BTGamepadController] rfkill unblock failed: {e}", file=sys.stderr)

    try:
        status = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=5)
        if "Powered: no" in status.stdout:
            print("[BTGamepadController] Bluetooth adapter is powered off -- powering on.")
            subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[BTGamepadController] bluetoothctl power-on check failed: {e}", file=sys.stderr)


def _looks_like_gamepad(name):
    lower_name = name.lower()
    return any(kw in lower_name for kw in ("gamepad", "controller", "joystick"))


def find_gamepads():
    """Scan system input devices for gamepad/joystick-like devices."""
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    return [device for device in devices if _looks_like_gamepad(device.name)]


def _paired_gamepad_macs():
    """Best-effort: MAC addresses of paired/trusted devices whose name looks like a
    gamepad (same filter as find_gamepads()), via `bluetoothctl devices Paired`.
    These are candidates for an active reconnect -- see _ReconnectWorker. Never
    raises; a missing/failing bluetoothctl just yields no candidates."""
    try:
        result = subprocess.run(
            ["bluetoothctl", "devices", "Paired"], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[BTGamepadController] bluetoothctl devices Paired failed: {e}", file=sys.stderr)
        return []
    macs = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(maxsplit=2)  # "Device <mac> <name>"
        if len(parts) == 3 and parts[0] == "Device" and _looks_like_gamepad(parts[2]):
            macs.append(parts[1])
    return macs


class _ReconnectWorker:
    """Background thread that periodically runs `bluetoothctl connect <mac>` against
    paired gamepad-like devices while none is connected, so a gamepad that dropped
    out of range reconnects on its own once back in range -- without the ~5-10s a
    connect attempt can take ever blocking poll()'s thread."""

    def __init__(self, is_connected):
        self._is_connected = is_connected  # callable: True if a gamepad is currently connected
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop_event.wait(BT_RECONNECT_INTERVAL_S):
            if self._is_connected():
                continue
            for mac in _paired_gamepad_macs():
                if self._is_connected() or self._stop_event.is_set():
                    break
                try:
                    subprocess.run(
                        ["bluetoothctl", "connect", mac],
                        capture_output=True, text=True, timeout=BT_CONNECT_TIMEOUT_S,
                    )
                except (OSError, subprocess.SubprocessError) as e:
                    print(f"[BTGamepadController] bluetoothctl connect {mac} failed: {e}", file=sys.stderr)

    def stop(self):
        self._stop_event.set()


@dataclass
class GamepadState:
    """Latest known state: buttons is {code: pressed_bool}, axes is {code: value}.
    repeat_codes is transient -- the set of keycodes that had a PRESSED or HELD
    (evdev value 1 or 2) event in the batch processed by the *most recent*
    poll() call only, so callers can tell a held button apart from one that's
    just sitting there unchanged (buttons alone can't distinguish those)."""
    buttons: dict = field(default_factory=dict)
    axes: dict = field(default_factory=dict)
    repeat_codes: set = field(default_factory=set)


class BTGamepadController:
    """Bluetooth gamepad input. poll() is non-blocking: it drains any pending
    events and returns the updated GamepadState, or None if no gamepad is
    connected or no new events arrived since the last poll(). While
    disconnected, poll() re-scans for a gamepad every RECONNECT_INTERVAL_S
    seconds and connects automatically once one shows up."""

    def __init__(self, verbose=False):
        _ensure_bluetooth_powered_on()
        self.device = None
        self.state = GamepadState()
        self.verbose = verbose  # print each raw key/axis event to the console as it arrives
        self._last_reconnect_attempt = time.monotonic()
        self._last_held_time = {}  # keycode -> monotonic time its last accepted HELD event landed
        self._try_connect()
        if self.device is None:
            print(
                "[BTGamepadController] No gamepad found -- input disabled. "
                f"Will keep checking every {RECONNECT_INTERVAL_S:g}s, and periodically try "
                f"reconnecting any paired gamepad every {BT_RECONNECT_INTERVAL_S:g}s."
            )
        self._reconnect_worker = _ReconnectWorker(is_connected=lambda: self.device is not None)

    def _try_connect(self):
        gamepads = find_gamepads()
        if gamepads:
            self.device = gamepads[0]
            print(f"[BTGamepadController] Connected to: {self.device.name} ({self.device.path})")

    def poll(self):
        # Cleared on every call (not just when new events arrive) so it always
        # reflects only *this* call's batch -- otherwise a single HELD event
        # would look like it's still happening on every later poll() too.
        self.state.repeat_codes = set()

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
                    if event.value == 1:  # PRESSED -- the real press, always counts right away
                        self._last_held_time[event.code] = time.monotonic()
                        self.state.repeat_codes.add(event.code)
                    elif event.value == 2:  # HELD -- the device's own autorepeat, fires much
                        # faster than we want to act on it, so only accept one every
                        # HELD_REPEAT_INTERVAL_S per key (independent of other keys).
                        now = time.monotonic()
                        if now - self._last_held_time.get(event.code, 0.0) >= HELD_REPEAT_INTERVAL_S:
                            self._last_held_time[event.code] = now
                            self.state.repeat_codes.add(event.code)
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
            # Forget held buttons -- the release events will never arrive, so a
            # stale "pressed" would keep acting (e.g. STOP counting toward shutdown).
            self.state.buttons.clear()
            return None

        return self.state

    def close(self):
        self._reconnect_worker.stop()


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
