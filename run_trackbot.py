#!/usr/bin/env python3
"""
run_trackbot.py

High-level main loop for trackbot. Owns the operating mode and ticks the
active behavior each cycle -- currently ball-following via YellowBallTracker.

Extension points for future work:
    BTGamepadController  -- bluetooth gamepad input, see bot_gamepad.py
    SensorHub   -- onboard sensor processing (read() is a stub for now)
    Mode switching -- --start-mode picks the base mode (idle/manual) to fall back
                      to; Mode.FOLLOW_BALL is layered on top of that at runtime via
                      the follow-ball button (BTN_R_FOLLOW_TOGGLE, see bot_commands.py)

Usage:
    python3 run_trackbot.py --model ssd --color-check --color-space lab --show-preview
"""

import argparse
import subprocess
import sys
import time
import tkinter as tk
from enum import Enum, auto

from bot_commands import BotCommandHandler, STOP_HOLD_SHUTDOWN_S
from bot_gamepad import BTGamepadController, GamepadState
from bot_motor import BotMotor
from plain_camera import PlainCameraViewer
from track_yellow_ball import YellowBallTracker
from trackbot_audio import TrackbotAudio
from trackbot_window import TrackbotWindow


class Mode(Enum):
    IDLE = auto()
    FOLLOW_BALL = auto()
    MANUAL = auto()


class SensorHub:
    """Placeholder for onboard sensor processing (e.g. bump/IR/ultrasonic).
    read() always reports nothing until sensors are wired up."""

    def read(self):
        return None  # TODO: read and return sensor state


FOLLOW_TARGET_SIZE_FRAC = 0.05  # ball size (box area / frame area) we're aiming to hold at
FOLLOW_MAX_THROTTLE = 0.4  # conservative cap until this is tuned on real hardware

# Ball within this much of center (normalized dx, 0..1) is treated as "centered"
# -- no steering correction at all, rather than constantly micro-steering at the
# slightest offset. Beyond the deadband, steering is rescaled back up to still
# reach full lock at the frame edge (dx = +/-1), so there's no jump at the edge
# of the zone. dx = +/-1 spans center-to-edge (half the frame width) each way,
# so this threshold value doubles as the deadband's total width as a fraction
# of the FULL frame width -- 0.20 here means a zone ~20% of the screen wide.
FOLLOW_CENTER_DEADBAND = 0.20

BALL_SOUND_PATH = "./sounds/ball.wav"  # played when a new ball is detected
BALL_SOUND_COOLDOWN_S = 5.0  # don't replay the sound more often than this

GAMEPAD_CONNECTED_SOUND_PATH = "./sounds/gamepadconnected.wav"
GAMEPAD_DISCONNECTED_SOUND_PATH = "./sounds/gamepadnotfound.wav"

VIDEO_ON_SOUND_PATH = "./sounds/videoison.wav"
VIDEO_OFF_SOUND_PATH = "./sounds/videoisoff.wav"

ETHERNET_CONNECTED_SOUND_PATH = "./sounds/ethernetconnected.wav"
WIFI_CONNECTED_SOUND_PATH = "./sounds/wificonnected.wav"
NO_NETWORK_SOUND_PATH = "./sounds/nonetworksfound.wav"

SHUTDOWN_SOUND_PATH = "./sounds/shuttingdown.wav"


def _follow_ball_steering(dx):
    if abs(dx) <= FOLLOW_CENTER_DEADBAND:
        return 0.0
    sign = 1.0 if dx > 0.0 else -1.0
    return sign * (abs(dx) - FOLLOW_CENTER_DEADBAND) / (1.0 - FOLLOW_CENTER_DEADBAND)


def _follow_ball_throttle_steering(result):
    """First-pass proportional control law: steer toward the ball's dx (once outside
    the FOLLOW_CENTER_DEADBAND), move forward proportional to how much smaller than
    FOLLOW_TARGET_SIZE_FRAC the box is, and stop (no reverse) once it's at/above
    target size. Needs tuning on real hardware."""
    steering = _follow_ball_steering(result.dx)
    gap = (FOLLOW_TARGET_SIZE_FRAC - result.size_frac) / FOLLOW_TARGET_SIZE_FRAC
    throttle = max(0.0, min(1.0, gap)) * FOLLOW_MAX_THROTTLE
    return throttle, steering


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=["nanodet", "ssd"], default="ssd",
                         help="Which on-sensor model to use for ball following")
    parser.add_argument("--color-check", action="store_true",
                         help="Require a yellow color check on top of the class filter")
    parser.add_argument("--color-space", choices=["hsv", "lab"], default="hsv",
                         help="Color space used for the --color-check yellow test")
    parser.add_argument("--show-preview", action="store_true", help="Show a live preview window")
    parser.add_argument("--debug", action="store_true", help="Print verbose per-frame debug info")
    parser.add_argument("--start-mode", choices=["idle", "manual"], default="manual",
                         help="Base operating mode to start in (Mode.FOLLOW_BALL isn't a start "
                              "mode -- it's toggled at runtime with the follow-ball button)")
    parser.add_argument("--record-preview", action=argparse.BooleanOptionalAction, default=False,
                         help="Record camera video to ~/Videos/Trackbot (default: off)")
    parser.add_argument("--upside-down", action=argparse.BooleanOptionalAction, default=True,
                         help="Camera is mounted upside down, so flip h/v (default: on; "
                              "pass --no-upside-down if it's mounted right-side up)")
    return parser.parse_args()


# --record-preview starts the encoder/muxer this long after the mainloop begins
# running, rather than during tracker.start() -- starting it before the camera
# and mainloop have settled was observed to hang the whole program.
RECORD_AUTOSTART_DELAY_S = 1.0


def mainloop(tracker, motor, start_mode="manual", debug=False, audio=None):
    if audio is None:
        audio = TrackbotAudio()
    window = TrackbotWindow()
    window_closed = False
    controller = BTGamepadController(verbose=debug)
    command_handler = BotCommandHandler()
    sensors = SensorHub()
    base_mode = Mode[start_mode.upper()]  # the mode to fall back to when FOLLOW_BALL is off
    window.set_mode(base_mode.name)
    window.set_video_status("Recording" if tracker.video_recorder.recording else "Off")
    last_debug_msg = None
    last_recording_state = tracker.video_recorder.recording
    last_follow_state = command_handler.follow_ball
    last_mode = base_mode  # for logging entering/leaving ball tracking mode below
    mainloop_start_time = time.monotonic()
    pending_record_autostart = tracker.video_recorder.enabled  # --record-preview was passed
    ball_was_found = False
    last_ball_sound_time = -BALL_SOUND_COOLDOWN_S  # so the very first detection can play
    last_effective_buttons = {}  # last (real gamepad | virtual window) merged button state we acted on

    # Announce the gamepad's connection state as of startup, then again on every
    # change (BTGamepadController.poll() reconnects/disconnects automatically).
    # queue() (not play()) so this waits behind the network-status announcement
    # (also queued, in _log_network_status()) instead of cutting it off if it's
    # still playing.
    gamepad_was_connected = controller.device is not None
    print(f"[mainloop] Gamepad {'connected' if gamepad_was_connected else 'not connected'} at startup")
    audio.queue(GAMEPAD_CONNECTED_SOUND_PATH if gamepad_was_connected else GAMEPAD_DISCONNECTED_SOUND_PATH,
                pause_after=True)

    # PlainCameraViewer.tick() never finds anything -- no AI camera is attached, so
    # FOLLOW_BALL can't do anything but spam "ball not found". Block the toggle from
    # taking effect in that case rather than silently failing to drive.
    follow_ball_available = not isinstance(tracker, PlainCameraViewer)

    def debug_print(msg):
        nonlocal last_debug_msg
        if debug and msg != last_debug_msg:
            print(msg)
        last_debug_msg = msg

    try:
        while True:
            controller.poll()  # updates controller.state.buttons as a side effect; return value unused below

            # Merge the real gamepad's live buttons with the virtual gamepad's
            # (window.virtual_buttons, keyed by the same evdev keycodes) so a
            # window button press drives BotCommandHandler exactly like a real
            # one would -- only re-running process_command() when the combined
            # state actually changes, same as it would only run on a genuinely
            # new physical event.
            effective_buttons = dict(controller.state.buttons)
            for keycode, pressed in window.virtual_buttons.items():
                if pressed:
                    effective_buttons[keycode] = True
            if effective_buttons != last_effective_buttons:
                last_effective_buttons = effective_buttons
                command_handler.process_command(GamepadState(buttons=effective_buttons))
            command_handler.tick()  # ease throttle toward its target, independent of new events

            gamepad_connected = controller.device is not None
            if gamepad_connected != gamepad_was_connected:
                gamepad_was_connected = gamepad_connected
                print(f"[mainloop] Gamepad {'connected' if gamepad_connected else 'disconnected'}")
                audio.queue(GAMEPAD_CONNECTED_SOUND_PATH if gamepad_connected else GAMEPAD_DISCONNECTED_SOUND_PATH)

            # Uses live button state, not just new events, since neither the real
            # device (while held) nor the virtual window sends repeat events.
            command_handler.check_shutdown_hold(effective_buttons)
            if command_handler.shutdown_requested:
                print(f"[mainloop] A button held for {STOP_HOLD_SHUTDOWN_S:g}s -> shutting down")
                break

            if pending_record_autostart and time.monotonic() - mainloop_start_time >= RECORD_AUTOSTART_DELAY_S:
                pending_record_autostart = False
                print(f"[mainloop] --record-preview: starting video recording "
                      f"({RECORD_AUTOSTART_DELAY_S:g}s after mainloop start)")
                command_handler.begin_recording()

            if command_handler.recording != last_recording_state:
                last_recording_state = command_handler.recording
                if command_handler.recording:
                    print("[mainloop] resuming video recording")
                    tracker.video_recorder.resume()
                    audio.queue(VIDEO_ON_SOUND_PATH)
                    window.set_video_status("Recording")
                else:
                    print("[mainloop] pausing video recording")
                    tracker.video_recorder.pause()
                    audio.queue(VIDEO_OFF_SOUND_PATH)
                    window.set_video_status("Off")

            if command_handler.follow_ball != last_follow_state:
                last_follow_state = command_handler.follow_ball
                if command_handler.follow_ball and not follow_ball_available:
                    print("[mainloop] Right trigger pressed -> FOLLOW_BALL requested, but no AI camera "
                          f"available -- staying in {base_mode.name}")

            mode = Mode.FOLLOW_BALL if (command_handler.follow_ball and follow_ball_available) else base_mode

            # Always log the actual mode boundary being crossed (not just the button
            # press above), so this stays correct regardless of what triggers a change.
            if mode != last_mode:
                if mode is Mode.FOLLOW_BALL:
                    print("[mainloop] Entering ball tracking mode")
                else:
                    print(f"[mainloop] Leaving ball tracking mode -> {mode.name}")
                last_mode = mode
                window.set_mode(mode.name)

            if not window_closed:
                try:
                    window.tick()
                except tk.TclError:
                    # User closed the window -- keep driving, just stop touching it.
                    print("[mainloop] Trackbot window closed")
                    window_closed = True

            if window.quit_requested:
                print("[mainloop] Quit button confirmed -> shutting down")
                break

            sensors.read()  # TODO: react to sensor state (e.g. obstacle stop) once wired up

            if mode is Mode.FOLLOW_BALL:
                result = tracker.tick()
                if result is not None:
                    if not ball_was_found and time.monotonic() - last_ball_sound_time >= BALL_SOUND_COOLDOWN_S:
                        last_ball_sound_time = time.monotonic()
                        audio.queue(BALL_SOUND_PATH)
                    ball_was_found = True

                    throttle, steering = _follow_ball_throttle_steering(result)
                    debug_print(f"[mainloop] FOLLOW_BALL: motor.drive(throttle={throttle:.3f}, steering={steering:.3f})")
                    motor.drive(throttle=throttle, steering=steering)
                else:
                    ball_was_found = False
                    debug_print("[mainloop] FOLLOW_BALL: ball not found -> motor.stop()")
                    motor.stop()
            elif mode is Mode.MANUAL:
                left, right = command_handler.left, command_handler.right
                debug_print(
                    f"[mainloop] MANUAL: motor.drive_lr(left={left:.3f}, right={right:.3f}) "
                    f"(throttle={command_handler.throttle:.3f}, steering={command_handler.steering:.3f})"
                )
                motor.drive_lr(left=left, right=right)
                time.sleep(0.05)
            else:  # Mode.IDLE
                debug_print("[mainloop] IDLE: motor.stop()")
                motor.stop()
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        motor.stop()
        print("[mainloop] Shutting down")
        # play(), not queue(): this is the one place interrupting is correct --
        # the program is exiting in a fixed 4s window regardless, so if some
        # other clip were still queued ahead of this, queue() could let the
        # shutdown announcement get silently skipped by the timeout below
        # without ever actually playing.
        audio.play(SHUTDOWN_SOUND_PATH)
        audio.wait_for_sound(timeout=4)
        motor.close()
        tracker.stop()
        audio.close()
        if not window_closed:
            try:
                window.close()
            except tk.TclError:
                pass


def _log_network_status(audio=None):
    """Best-effort: log which network interfaces (wifi/ethernet) are connected,
    to what, and the device's IP address(es) -- useful from the startup log for
    diagnosing "why can't I SSH/VNC in" without needing a monitor on the bot.
    If audio is given, also plays a sound reflecting the result: ethernet takes
    priority over wifi if both are connected, else the no-network sound.
    Never raises -- a missing nmcli or no network at all is just logged, not fatal."""
    connected_types = set()
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
            capture_output=True, text=True, timeout=5,
        )
        connected = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            device, dev_type, state = parts[0], parts[1], parts[2]
            connection = ":".join(parts[3:])  # connection names could contain ':'
            if dev_type in ("wifi", "ethernet") and state == "connected":
                connected.append(f'{dev_type} {device} -> "{connection}"')
                connected_types.add(dev_type)
        if connected:
            print(f"[network] Connected: {', '.join(connected)}")
        else:
            print("[network] No wifi/ethernet connection detected.")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[network] nmcli check failed: {e}", file=sys.stderr)

    if audio is not None:
        # queue(), not play() -- this runs first in practice, but queue() still
        # can't ever cut off something already playing/queued, unlike play().
        if "ethernet" in connected_types:
            audio.queue(ETHERNET_CONNECTED_SOUND_PATH, pause_after=True)
        elif "wifi" in connected_types:
            audio.queue(WIFI_CONNECTED_SOUND_PATH, pause_after=True)
        else:
            audio.queue(NO_NETWORK_SOUND_PATH, pause_after=True)

    try:
        ip_result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        ips = ip_result.stdout.split()
        if ips:
            print(f"[network] IP address(es): {', '.join(ips)}")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[network] IP address check failed: {e}", file=sys.stderr)


# Raised by IMX500(...) when no AI camera is attached. Checked explicitly below so an
# unrelated RuntimeError from YellowBallTracker/IMX500 setup doesn't get misread as
# "no IMX500 camera" and silently swallowed into a fallback.
IMX500_NOT_FOUND_ERROR = "IMX500: Requested camera dev-node not found"


def init(args):
    """Start the camera (IMX500 AI camera preferred, plain camera fallback) and the
    motor controller. Returns (tracker, motor); tracker is either a YellowBallTracker
    or, if no IMX500 is attached, a PlainCameraViewer (no detection, view-only)."""
    need_fallback = False
    try:
        tracker = YellowBallTracker(
            model=args.model,
            color_check=args.color_check,
            color_space=args.color_space,
            debug=args.debug,
            show_preview=args.show_preview,
            record_preview=args.record_preview,
            upside_down=args.upside_down,
        )
        tracker.start()
    except RuntimeError as e:
        if str(e) != IMX500_NOT_FOUND_ERROR:
            raise
        print(f"[init] IMX500 camera unavailable ({e}); falling back to plain camera preview.")
        need_fallback = True

    if need_fallback:
        try:
            # No ball detection on a plain camera, but still worth a look, so force
            # the preview on regardless of --show-preview; without it this path
            # shows nothing at all.
            tracker = PlainCameraViewer(show_preview=True, record_preview=args.record_preview)
            tracker.start()
        except Exception as e:
            print(f"[init] Plain camera fallback also failed: {e}", file=sys.stderr)
            raise

    motor = BotMotor()
    return tracker, motor


def main():
    audio = TrackbotAudio()  # shared across the network-status check and mainloop()
    _log_network_status(audio)
    args = get_args()
    tracker, motor = init(args)
    mainloop(tracker, motor, start_mode=args.start_mode, debug=args.debug, audio=audio)


if __name__ == "__main__":
    sys.exit(main() or 0)
