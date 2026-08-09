#!/usr/bin/env python3
"""
run_trackbot.py

High-level main loop for trackbot. Owns the operating mode and ticks the
active behavior each cycle -- currently ball-following via YellowBallTracker.

Extension points for future work:
    Controller  -- bluetooth controller input (poll() is a stub for now)
    SensorHub   -- onboard sensor processing (read() is a stub for now)
    Mode.MANUAL -- direct drive from controller commands, once wired up

Usage:
    python3 run_trackbot.py --model ssd --color-check --color-space lab --show-preview
"""

import argparse
import sys
import time
from enum import Enum, auto

from track_yellow_ball import YellowBallTracker


class Mode(Enum):
    IDLE = auto()
    FOLLOW_BALL = auto()
    MANUAL = auto()


class Controller:
    """Placeholder for the bluetooth controller. poll() always reports no
    input until this is wired up, so mode switches and manual drive commands
    are inert for now."""

    def poll(self):
        return None  # TODO: read bluetooth controller input, return a command


class SensorHub:
    """Placeholder for onboard sensor processing (e.g. bump/IR/ultrasonic).
    read() always reports nothing until sensors are wired up."""

    def read(self):
        return None  # TODO: read and return sensor state


def drive(dx=0.0, dy=0.0, size_frac=0.0):
    """Placeholder motor control. TODO: wire up to the actual drive hardware."""
    pass


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
    return parser.parse_args()


def mainloop(tracker, start_mode="follow_ball"):
    controller = Controller()
    sensors = SensorHub()
    mode = Mode[start_mode.upper()]

    try:
        while True:
            command = controller.poll()
            if command is not None:
                pass  # TODO: apply mode switches / manual drive commands from the controller

            sensors.read()  # TODO: react to sensor state (e.g. obstacle stop) once wired up

            if mode is Mode.FOLLOW_BALL:
                result = tracker.tick()
                if result is not None:
                    drive(dx=result.dx, dy=result.dy, size_frac=result.size_frac)
                else:
                    drive()
            elif mode is Mode.MANUAL:
                drive()  # TODO: drive from controller command once bluetooth input exists
                time.sleep(0.05)
            else:  # Mode.IDLE
                drive()
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        tracker.stop()


def main():
    args = get_args()

    tracker = YellowBallTracker(
        model=args.model,
        color_check=args.color_check,
        color_space=args.color_space,
        debug=args.debug,
        show_preview=args.show_preview,
    )
    tracker.start()

    mainloop(tracker, start_mode=args.start_mode)


if __name__ == "__main__":
    sys.exit(main() or 0)
