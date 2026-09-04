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

# 4 direction arrows
BTN_UP = ecodes.KEY_C
BTN_DOWN = ecodes.KEY_D
BTN_LEFT = ecodes.KEY_E
BTN_RIGHT = ecodes.KEY_F

# X, Y, A, B buttons
BTN_A_STOP = ecodes.KEY_G
BTN_X_STRAIGHT = ecodes.KEY_H           # Zero steering
BTN_L_RECORD_TOGGLE = ecodes.KEY_K      # Video recording on/off
BTN_R_FOLLOW_TOGGLE = ecodes.KEY_M      # Ball-following (Mode.FOLLOW_BALL) on/off

# A press of a toggle button (recording, follow-ball) within this long of its own
# previous press counts as a "quick click": instead of toggling again, it forces
# that toggle off. So a single press toggles normally, but two or more rapid
# presses always end up off, regardless of parity. See _QuickClickToggle.
QUICK_CLICK_WINDOW_S = 0.6

# Holding STOP continuously this long requests a clean program shutdown. Checked
# in check_shutdown_hold(), which needs the gamepad's *live* button state (not
# just new events) since this device doesn't send repeat events while held --
# see run_trackbot.py's mainloop, which calls it every iteration with
# controller.state.buttons.
STOP_HOLD_SHUTDOWN_S = 3.0

# Throttle and steering are both a ratchet: each press of the relevant key steps
# the target up by one rung (see _update_throttle_target/_update_steering_raw),
# and releasing the key just holds at the current rung rather than decaying back
# toward zero. Only an explicit opposite-direction press, DECEL, or STOP moves it
# back down -- otherwise a release between taps (which is how repeated presses
# necessarily arrive) would keep undoing progress, and it'd take holding the key
# down (for the device's own key-repeat events) to ever get anywhere.

# Throttle ramp, tuned against the physical bot: the bot doesn't actually move
# until the 3rd fwd press (lower values don't clear the motor's deadband). So:
# stay at 0 for the first two presses, jump straight to the deadband-clearing
# floor on the 3rd, then ramp up to the 1.0 speed limit over further presses --
# 20 presses (steps) stop-to-max overall.
THROTTLE_DEAD_PRESSES = 2    # presses with the bot held at 0 before the floor kicks in
THROTTLE_FLOOR = 0.35        # value assigned on the press that first clears the deadband (0.55 * 0.7)
THROTTLE_MAX = 1.0           # speed limit / ceiling
THROTTLE_RAMP_PRESSES = 17   # further presses from floor to max (2 dead + 1 floor + 17 = 20 total)
THROTTLE_RAMP_STEP = (THROTTLE_MAX - THROTTLE_FLOOR) / THROTTLE_RAMP_PRESSES

# A ramp/decel press changes the *target* throttle instantly, but jumping the
# motor's actual output straight there (e.g. the 0 -> THROTTLE_FLOOR floor jump)
# is an abrupt jolt. Smooth it out into small steps paced by wall-clock time (via
# tick(), called every mainloop iteration) instead of one big jump per press.
THROTTLE_SMOOTH_INTERVAL_S = 0.5
THROTTLE_SMOOTH_STEP = 0.5

STEERING_MAX = 1.0

# Steering ramps up from center the same way throttle ramps up from a stop: a
# fixed number of presses (no dead presses/floor jump -- steering has no
# deadband to clear) spread evenly from 0 to STEERING_MAX.
STEERING_RAMP_PRESSES = 20
STEERING_RAMP_STEP = STEERING_MAX / STEERING_RAMP_PRESSES

# The naive differential-drive mix (left = throttle + steering, right = throttle
# - steering) raises one track and lowers the other by equal amounts, which can
# drive the lowered track to zero or into reverse -- an unintended spin rather
# than a turn. Instead, BotCommandHandler.left/right only ever *lower* the
# inside track toward zero (the outside track always stays at self.throttle,
# never raised above it), capped so the two tracks can never differ by more
# than MOTOR_DIFF_MAX_STEPS throttle steps, regardless of how much steering is
# applied.
MOTOR_DIFF_MAX_STEPS = 2
THROTTLE_TOTAL_PRESSES = THROTTLE_DEAD_PRESSES + 1 + THROTTLE_RAMP_PRESSES  # zero to full throttle
THROTTLE_STEP = THROTTLE_MAX / THROTTLE_TOTAL_PRESSES
MOTOR_DIFF_MAX = MOTOR_DIFF_MAX_STEPS * THROTTLE_STEP


def _reduce_toward_zero(value, amount):
    """Move value toward 0 by amount, without crossing past 0."""
    if value > 0:
        return max(0.0, value - amount)
    if value < 0:
        return min(0.0, value + amount)
    return 0.0


class _QuickClickToggle:
    """A single press toggles on/off; a press within QUICK_CLICK_WINDOW_S of the
    previous one instead forces off, so a burst of quick clicks always ends off
    regardless of how many presses it contains."""

    def __init__(self):
        self.on = False
        self._was_down = False
        self._last_press_time = None

    def update(self, key_down):
        """Call once per process_command() with the button's current down state;
        returns the (possibly updated) on/off state."""
        if key_down and not self._was_down:
            # Fires once per physical press, not while held or on release.
            now = time.monotonic()
            if (self._last_press_time is not None
                    and now - self._last_press_time <= QUICK_CLICK_WINDOW_S):
                self.on = False
            else:
                self.on = not self.on
            self._last_press_time = now
        self._was_down = key_down
        return self.on

    def force_off(self):
        self.on = False

    def force_on(self):
        self.on = True


class BotCommandHandler:
    """Owns the mapping from raw gamepad state to bot behavior.

    throttle/steering ratchet toward the D-pad's current up/down/left/right state
    (-1.0..1.0 each) one rung per press rather than jumping straight there. A
    caller in Mode.MANUAL reads left/right (not throttle/steering directly) to
    feed into BotMotor.drive_lr().
    """

    def __init__(self):
        self.throttle = 0.0
        self.steering = 0.0
        self._steering_raw = 0.0  # stepped toward the D-pad target, before the speed-based clamp
        self._steering_press_count = 0
        self._steering_press_dir = 0
        self._throttle_target = 0.0
        self._throttle_press_count = 0
        self._throttle_press_dir = 0
        self._last_smooth_time = time.monotonic()
        self._recording_toggle = _QuickClickToggle()
        self._follow_toggle = _QuickClickToggle()
        self.shutdown_requested = False
        self._stop_hold_start = None

    @property
    def recording(self):
        return self._recording_toggle.on

    @property
    def follow_ball(self):
        return self._follow_toggle.on

    def begin_recording(self):
        """Force recording on, e.g. for a deferred --record-preview auto-start.
        Leaves it toggle-able (including quick-click-off) afterward, same as if
        the record button itself had just been pressed."""
        self._recording_toggle.force_on()

    def check_shutdown_hold(self, buttons):
        """Call every mainloop iteration with the gamepad's live button state
        (e.g. controller.state.buttons, not just process_command()'s events) --
        sets shutdown_requested once STOP has been held continuously for
        STOP_HOLD_SHUTDOWN_S."""
        if buttons.get(BTN_A_STOP, False):
            if self._stop_hold_start is None:
                self._stop_hold_start = time.monotonic()
            elif time.monotonic() - self._stop_hold_start >= STOP_HOLD_SHUTDOWN_S:
                self.shutdown_requested = True
        else:
            self._stop_hold_start = None

    def process_command(self, command):
        """command is a bot_gamepad.GamepadState (buttons + axes dicts)."""
        buttons = command.buttons

        self._recording_toggle.update(buttons.get(BTN_L_RECORD_TOGGLE, False))
        self._follow_toggle.update(buttons.get(BTN_R_FOLLOW_TOGGLE, False))

        if buttons.get(BTN_A_STOP, False):
            # Immediate stop/zero, bypassing the eased ramp and the smoothing below.
            # Also drops back out of FOLLOW_BALL -- an emergency stop should hand
            # control back to the human, not leave the bot driving itself.
            self.throttle = 0.0
            self._throttle_target = 0.0
            self.steering = 0.0
            self._steering_raw = 0.0
            self._throttle_press_count = 0
            self._throttle_press_dir = 0
            self._steering_press_count = 0
            self._steering_press_dir = 0
            self._follow_toggle.force_off()
            return

        if buttons.get(BTN_X_STRAIGHT, False):
            # self._drop_throttle_step()
            self.steering = 0.0
            self._steering_raw = 0.0
            self._steering_press_count = 0
            self._steering_press_dir = 0
            return

        up = buttons.get(BTN_UP, False)
        down = buttons.get(BTN_DOWN, False)
        left = buttons.get(BTN_LEFT, False)
        right = buttons.get(BTN_RIGHT, False)

        throttle_target = float(up) - float(down)
        steering_target = float(right) - float(left)

        self._update_throttle_target(throttle_target)
        self._update_steering_raw(steering_target)
        self.steering = self._steering_raw

    @property
    def left(self):
        return self._motor_lr()[0]

    @property
    def right(self):
        return self._motor_lr()[1]

    def _motor_lr(self):
        """Differential-drive mix for BotMotor.drive_lr(): the outside track always
        stays at self.throttle (never raised above it); the inside track (picked by
        steering's sign) is lowered toward zero by one THROTTLE_STEP per steering
        press, capped at MOTOR_DIFF_MAX_STEPS presses -- so the first couple of
        steering presses have an immediately noticeable effect, and the two tracks
        never differ by more than MOTOR_DIFF_MAX_STEPS throttle steps no matter how
        many more presses follow (steering's own 20-press ramp is finer than this,
        but past MOTOR_DIFF_MAX_STEPS presses it no longer changes the motor mix)."""
        diff_magnitude = min(MOTOR_DIFF_MAX_STEPS, self._steering_press_count) * THROTTLE_STEP
        inside = _reduce_toward_zero(self.throttle, diff_magnitude)
        if self.steering >= 0:
            return self.throttle, inside  # steering right -> right track is inside
        return inside, self.throttle  # steering left -> left track is inside

    def tick(self):
        """Nudge the actual throttle output toward _throttle_target, at most once
        every THROTTLE_SMOOTH_INTERVAL_S. Call this every mainloop iteration
        (independent of new gamepad events) so a ramp/decel step's target change
        arrives gradually instead of snapping the motor straight there."""
        now = time.monotonic()
        if now - self._last_smooth_time < THROTTLE_SMOOTH_INTERVAL_S:
            return
        self._last_smooth_time = now

        if self.throttle < self._throttle_target:
            self.throttle = min(self._throttle_target, self.throttle + THROTTLE_SMOOTH_STEP)
        elif self.throttle > self._throttle_target:
            self.throttle = max(self._throttle_target, self.throttle - THROTTLE_SMOOTH_STEP)

    def _update_steering_raw(self, target):
        direction = 0 if target == 0.0 else (1 if target > 0.0 else -1)

        if direction == 0:
            # Released -- ratchet holds at the current rung (see the module-level
            # comment); only an opposite-direction press or STOP brings it back.
            return

        if direction != self._steering_press_dir:
            # First press from center, or a direction reversal -- restart the count.
            self._steering_press_count = 0
            self._steering_press_dir = direction

        self._steering_press_count += 1
        self._steering_raw = direction * min(STEERING_MAX, STEERING_RAMP_STEP * self._steering_press_count)

    def _update_throttle_target(self, target):
        """ target is the desired throttle direction as negative (-1.0), 0, or positive (1.0) """
        direction = 0 if target == 0.0 else (1 if target > 0.0 else -1)

        if direction == 0:
            # Released -- ratchet holds at the current rung (see the module-level
            # comment); only DECEL or STOP brings the throttle back down.
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
