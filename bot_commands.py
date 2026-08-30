#!/usr/bin/env python3
"""
bot_commands.py

Translates raw gamepad state (bot_gamepad.GamepadState) into bot-level commands --
mode switches, manual drive input, etc. process_command() is called from
run_trackbot.py's main loop whenever BTGamepadController.poll() returns a new state.
"""

import time

from evdev import ecodes

# This gamepad's D-pad shows up as plain keyboard keycodes rather than BTN_DPAD_*/
# ABS_HAT0X,Y -- confirmed by testing: UP sends 'c', DOWN sends 'd', LEFT sends 'e',
# RIGHT sends 'f', and the "stop" button sends 'g'.
KEY_UP = ecodes.KEY_C
KEY_DOWN = ecodes.KEY_D
KEY_LEFT = ecodes.KEY_E
KEY_RIGHT = ecodes.KEY_F
KEY_STOP = ecodes.KEY_G
KEY_DECEL = ecodes.KEY_H  # drops throttle by one ramp step and zeroes steering
KEY_RECORD_TOGGLE = ecodes.KEY_K  # toggles video recording on/off

# A K press within this long of the previous K press counts as a "quick click":
# instead of toggling again, it forces recording off. So a single press toggles
# normally, but two or more rapid presses always end up off, regardless of parity.
RECORD_QUICK_CLICK_WINDOW_S = 0.6

# Step size range for easing steering (and throttle's ease-back-to-zero) toward
# the D-pad's target each time process_command() runs: near zero (gentle start/
# fine control), larger steps further out (faster ramp once already moving).
MIN_STEP = 0.15
MAX_STEP = 0.35

# Throttle ramp, tuned against the physical bot: the old uniform stepping (using
# MIN_STEP/MAX_STEP from a stop) put the bot at ~0.55 after 3 presses and had it
# saturated at the 1.0 ceiling by the 5th-6th -- but the bot doesn't actually
# move until the 3rd fwd press (lower values don't clear the motor's deadband).
# So: stay at 0 for the first two presses, jump straight to the deadband-clearing
# floor on the 3rd (matching where the old stepping landed by press 3), then ramp
# up to the 1.0 speed limit (matching where the old stepping had already
# saturated by press 6) over further presses -- 6-8 presses stop-to-max overall.
THROTTLE_DEAD_PRESSES = 2   # presses with the bot held at 0 before the floor kicks in
THROTTLE_FLOOR = 0.55       # value assigned on the press that first clears the deadband
THROTTLE_MAX = 1.0          # speed limit / ceiling
THROTTLE_RAMP_PRESSES = 7   # further presses from floor to max (2 dead + 1 floor + 7 = 10 total)
THROTTLE_RAMP_STEP = (THROTTLE_MAX - THROTTLE_FLOOR) / THROTTLE_RAMP_PRESSES

# A ramp/decel press changes the *target* throttle instantly, but jumping the
# motor's actual output straight there (e.g. the 0 -> 0.55 floor jump) is an
# abrupt jolt. Smooth it out into small steps paced by wall-clock time (via
# tick(), called every mainloop iteration) instead of one big jump per press.
THROTTLE_SMOOTH_INTERVAL_S = 0.05
THROTTLE_SMOOTH_STEP = 0.05

# Steering authority shrinks as throttle climbs toward THROTTLE_MAX -- a full
# hard-over turn at a stop is fine, but the same input at top speed should only
# be a gentle nudge. STEERING_MAX is the full-lock value available at a stop;
# it scales linearly down to STEERING_MIN_FRACTION of that (25%) by the time
# throttle reaches THROTTLE_MAX.
STEERING_MAX = 1.0
STEERING_MIN_FRACTION = 0.25

# Steering ramps up from center the same way throttle ramps up from a stop: a
# fixed number of presses (no dead presses/floor jump -- steering has no
# deadband to clear) spread evenly from 0 to STEERING_MAX.
STEERING_RAMP_PRESSES = 10
STEERING_RAMP_STEP = STEERING_MAX / STEERING_RAMP_PRESSES


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
        self.recording = False
        self._steering_raw = 0.0  # stepped toward the D-pad target, before the speed-based clamp
        self._steering_press_count = 0
        self._steering_press_dir = 0
        self._throttle_target = 0.0
        self._throttle_press_count = 0
        self._throttle_press_dir = 0
        self._last_smooth_time = time.monotonic()
        self._record_key_was_down = False
        self._last_record_press_time = None

    def process_command(self, command):
        """command is a bot_gamepad.GamepadState (buttons + axes dicts)."""
        buttons = command.buttons

        self._handle_record_toggle(buttons)

        if buttons.get(KEY_STOP, False):
            # Immediate stop/zero, bypassing the eased ramp and the smoothing below.
            self.throttle = 0.0
            self._throttle_target = 0.0
            self.steering = 0.0
            self._steering_raw = 0.0
            self._throttle_press_count = 0
            self._throttle_press_dir = 0
            self._steering_press_count = 0
            self._steering_press_dir = 0
            return

        if buttons.get(KEY_DECEL, False):
            self._drop_throttle_step()
            self.steering = 0.0
            self._steering_raw = 0.0
            self._steering_press_count = 0
            self._steering_press_dir = 0
            return

        up = buttons.get(KEY_UP, False)
        down = buttons.get(KEY_DOWN, False)
        left = buttons.get(KEY_LEFT, False)
        right = buttons.get(KEY_RIGHT, False)

        throttle_target = float(up) - float(down)
        steering_target = float(right) - float(left)

        self._update_throttle_target(throttle_target)
        self._update_steering_raw(steering_target)
        self.steering = self._clamp_steering(self._steering_raw)

    def _handle_record_toggle(self, buttons):
        key_down = buttons.get(KEY_RECORD_TOGGLE, False)
        if key_down and not self._record_key_was_down:
            # Fires once per physical press, not while held or on release.
            now = time.monotonic()
            if (self._last_record_press_time is not None
                    and now - self._last_record_press_time <= RECORD_QUICK_CLICK_WINDOW_S):
                self.recording = False
            else:
                self.recording = not self.recording
            self._last_record_press_time = now
        self._record_key_was_down = key_down

    def tick(self):
        """Nudge the actual throttle output toward _throttle_target, at most once
        every THROTTLE_SMOOTH_INTERVAL_S. Call this every mainloop iteration
        (independent of new gamepad events) so a ramp/decel step's target change
        arrives gradually instead of snapping the motor straight there. Also
        re-clamps steering against the current throttle, so steering authority
        shrinks in step as the throttle ramp climbs -- not just on the next
        steering key press."""
        now = time.monotonic()
        if now - self._last_smooth_time < THROTTLE_SMOOTH_INTERVAL_S:
            return
        self._last_smooth_time = now

        if self.throttle < self._throttle_target:
            self.throttle = min(self._throttle_target, self.throttle + THROTTLE_SMOOTH_STEP)
        elif self.throttle > self._throttle_target:
            self.throttle = max(self._throttle_target, self.throttle - THROTTLE_SMOOTH_STEP)

        self.steering = self._clamp_steering(self._steering_raw)

    def _update_steering_raw(self, target):
        direction = 0 if target == 0.0 else (1 if target > 0.0 else -1)

        if direction == 0:
            # Released -- ease back down to center and reset the press count so
            # the next left/right press starts the ramp over.
            self._steering_press_count = 0
            self._steering_press_dir = 0
            self._steering_raw = _step_toward(self._steering_raw, 0.0)
            return

        if direction != self._steering_press_dir:
            # First press from center, or a direction reversal -- restart the count.
            self._steering_press_count = 0
            self._steering_press_dir = direction

        self._steering_press_count += 1
        self._steering_raw = direction * min(STEERING_MAX, STEERING_RAMP_STEP * self._steering_press_count)

    def _clamp_steering(self, value):
        limit = self._max_steering()
        return max(-limit, min(limit, value))

    def _max_steering(self):
        fraction_of_top_speed = min(1.0, abs(self.throttle) / THROTTLE_MAX)
        return STEERING_MAX * (1.0 - fraction_of_top_speed * (1.0 - STEERING_MIN_FRACTION))

    def _update_throttle_target(self, target):
        direction = 0 if target == 0.0 else (1 if target > 0.0 else -1)

        if direction == 0:
            # Released -- ease back down to a stop and reset the press count so
            # the next fwd/back press starts the dead-press count over.
            self._throttle_press_count = 0
            self._throttle_press_dir = 0
            self.throttle = _step_toward(self.throttle, 0.0)
            self._throttle_target = self.throttle
            return

        if direction != self._throttle_press_dir:
            # First press from a stop, or a direction reversal -- restart the count.
            self._throttle_press_count = 0
            self._throttle_press_dir = direction

        self._throttle_press_count += 1
        self._throttle_target = direction * self._throttle_magnitude(self._throttle_press_count)

    def _drop_throttle_step(self):
        """Move the target back down to the previous ramp press's value; tick()
        eases the actual throttle output down to meet it."""
        self._throttle_press_count = max(0, self._throttle_press_count - 1)
        if self._throttle_press_count == 0:
            self._throttle_press_dir = 0
        self._throttle_target = self._throttle_press_dir * self._throttle_magnitude(self._throttle_press_count)

    @staticmethod
    def _throttle_magnitude(count):
        if count <= THROTTLE_DEAD_PRESSES:
            return 0.0
        ramp_presses_done = count - THROTTLE_DEAD_PRESSES - 1
        return min(THROTTLE_MAX, THROTTLE_FLOOR + THROTTLE_RAMP_STEP * ramp_presses_done)
