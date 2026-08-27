#!/usr/bin/env python3
"""
bot_commands.py

Translates raw gamepad state (bot_gamepad.GamepadState) into bot-level commands --
mode switches, manual drive input, etc. process_command() is called from
run_trackbot.py's main loop whenever BTGamepadController.poll() returns a new state.
"""

from evdev import ecodes

# This gamepad's D-pad shows up as plain keyboard keycodes rather than BTN_DPAD_*/
# ABS_HAT0X,Y -- confirmed by testing: UP sends 'c', DOWN sends 'd', LEFT sends 'e',
# RIGHT sends 'f'.
KEY_UP = ecodes.KEY_C
KEY_DOWN = ecodes.KEY_D
KEY_LEFT = ecodes.KEY_E
KEY_RIGHT = ecodes.KEY_F

# Step size range for easing throttle/steering toward the D-pad's target each time
# process_command() runs: small steps near zero (gentle start/fine control), larger
# steps further out (faster ramp once already moving). First pass, tune on hardware.
MIN_STEP = 0.02
MAX_STEP = 0.10


def _step_toward(current, target):
    if current == target:
        return current
    step = MIN_STEP + (MAX_STEP - MIN_STEP) * abs(current)

    if target == 0.0:
        # If target is zero, we want to ease back to zero more slowly the normal
        # step size would allow, so decrease the step size.
        step *= 0.1

    if target > current:
        return min(target, current + step)
    return max(target, current - step)


class BotCommandHandler:
    """Owns the mapping from raw gamepad state to bot behavior.

    throttle/steering ease toward the D-pad's current up/down/left/right state
    (-1.0..1.0 each) rather than jumping straight there, for a caller in Mode.MANUAL
    to feed into BotMotor.drive().
    """

    def __init__(self):
        self.throttle = 0.0
        self.steering = 0.0

    def process_command(self, command):
        """command is a bot_gamepad.GamepadState (buttons + axes dicts)."""
        buttons = command.buttons
        up = buttons.get(KEY_UP, False)
        down = buttons.get(KEY_DOWN, False)
        left = buttons.get(KEY_LEFT, False)
        right = buttons.get(KEY_RIGHT, False)

        throttle_target = float(up) - float(down)
        steering_target = float(right) - float(left)

        self.throttle = _step_toward(self.throttle, throttle_target)
        self.steering = _step_toward(self.steering, steering_target)
