#!/usr/bin/env python3
"""
run_trackbot.py

High-level main loop for trackbot. Owns the operating mode and ticks the
active behavior each cycle -- currently ball-following via YellowBallTracker.

Extension points for future work:
    BTGamepadController  -- bluetooth gamepad input, see bot_gamepad.py
    SensorHub   -- onboard sensor processing (read() is a stub for now)
    Mode switching -- switching in/out of Mode.MANUAL from a controller command
                      isn't wired up yet; --start-mode is the only way to pick it

Usage:
    python3 run_trackbot.py --model ssd --color-check --color-space lab --show-preview
"""

import argparse
import sys
import time
from enum import Enum, auto

from bot_commands import BotCommandHandler
from bot_gamepad import BTGamepadController
from bot_motor import BotMotor
from plain_camera import PlainCameraViewer
from track_yellow_ball import YellowBallTracker


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


def _follow_ball_throttle_steering(result):
    """First-pass proportional control law: steer toward the ball's dx, move forward
    proportional to how much smaller than FOLLOW_TARGET_SIZE_FRAC the box is, and stop
    (no reverse) once it's at/above target size. Needs tuning on real hardware."""
    steering = result.dx
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
    parser.add_argument("--start-mode", choices=[m.name.lower() for m in Mode], default="follow_ball",
                         help="Operating mode to start in")
    parser.add_argument("--record-preview", action=argparse.BooleanOptionalAction, default=False,
                         help="Record camera video to ~/Videos/Trackbot (default: off)")
    return parser.parse_args()


def mainloop(tracker, motor, start_mode="follow_ball", debug=False):
    controller = BTGamepadController(verbose=debug)
    command_handler = BotCommandHandler()
    sensors = SensorHub()
    mode = Mode[start_mode.upper()]
    last_debug_msg = None

    def debug_print(msg):
        nonlocal last_debug_msg
        if debug and msg != last_debug_msg:
            print(msg)
        last_debug_msg = msg

    if isinstance(tracker, PlainCameraViewer) and mode is Mode.FOLLOW_BALL:
        # PlainCameraViewer.tick() never finds anything -- no AI camera is attached,
        # so FOLLOW_BALL can't do anything but spam "ball not found". MANUAL still
        # works without AI detection, so start there instead.
        print("[mainloop] No AI camera available (plain camera fallback in use) -- "
              "starting in MANUAL instead of FOLLOW_BALL.")
        mode = Mode.MANUAL

    try:
        while True:
            command = controller.poll()
            if command is not None:
                command_handler.process_command(command)

            sensors.read()  # TODO: react to sensor state (e.g. obstacle stop) once wired up

            if mode is Mode.FOLLOW_BALL:
                result = tracker.tick()
                if result is not None:
                    throttle, steering = _follow_ball_throttle_steering(result)
                    debug_print(f"[mainloop] FOLLOW_BALL: motor.drive(throttle={throttle:.3f}, steering={steering:.3f})")
                    motor.drive(throttle=throttle, steering=steering)
                else:
                    debug_print("[mainloop] FOLLOW_BALL: ball not found -> motor.stop()")
                    motor.stop()
            elif mode is Mode.MANUAL:
                debug_print(
                    f"[mainloop] MANUAL: motor.drive(throttle={command_handler.throttle:.3f}, "
                    f"steering={command_handler.steering:.3f})"
                )
                motor.drive(throttle=command_handler.throttle, steering=command_handler.steering)
                time.sleep(0.05)
            else:  # Mode.IDLE
                debug_print("[mainloop] IDLE: motor.stop()")
                motor.stop()
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        motor.stop()
        motor.close()
        tracker.stop()


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
    args = get_args()
    tracker, motor = init(args)
    mainloop(tracker, motor, start_mode=args.start_mode, debug=args.debug)


if __name__ == "__main__":
    sys.exit(main() or 0)
