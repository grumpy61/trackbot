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
# just new/repeat events) since measuring a continuous hold needs "is it down
# right now" sampled every iteration -- the device's own HELD events (see
# bot_gamepad.py) are throttled to a fixed cadence, too coarse to time a hold
# by themselves. See run_trackbot.py's mainloop, which calls it every iteration
# with effective_buttons (the real gamepad merged with the virtual window's).
STOP_HOLD_SHUTDOWN_S = 3.0

# Throttle is a ratchet: each press of the relevant key steps the target up by
# one rung (see _update_throttle_target), and releasing the key just holds at
# the current rung rather than decaying back toward zero. Only an explicit
# opposite-direction press, DECEL, or STOP moves it back down -- otherwise a
# release between taps (which is how repeated presses necessarily arrive)
# would keep undoing progress, and it'd take holding the key down (for the
# device's own key-repeat events) to ever get anywhere. Steering is not
# a ratchet -- see the comment above STEERING_TABLE below.

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
THROTTLE_SMOOTH_INTERVAL_S = 0.1
THROTTLE_SMOOTH_STEP = 0.5

THROTTLE_TOTAL_PRESSES = THROTTLE_DEAD_PRESSES + 1 + THROTTLE_RAMP_PRESSES  # zero to full throttle

# Steering is a direct level, not a ratchet: self.steering is -1.0 (left) or 1.0
# (right), set the instant LEFT/RIGHT is pressed -- no press-counting, no ramp-up.
# It holds that direction after release, until the other direction is pressed or
# X/STRAIGHT or STOP recenters it.

# While moving, steering keeps both tracks driving: the outside track is raised
# by half of a speed gap ("diff") and the inside track sits the full gap below
# it, but never below a minimum ("min inside", a forward speed that still clears
# the motor deadband) so a turn never stalls the bot or spins it in place -- only
# a turn from a dead stop pivots. Both values depend on the throttle level, and
# are interpolated from this table by _steering_params(): each row is
# (throttle magnitude, diff, min_inside). Below the first / above the last row
# the nearest row's values are used. Tune slow/medium/fast turning here.
STEERING_TABLE = (
    (0.35, 0.75, 0.25),  # slow    -- just above the deadband floor
    (0.65, 0.85, 0.20),  # medium
    (1.00, 0.85, 0.25),  # fast    -- outside track is capped at 1.0, so the inside can't drop as far
)


def _steering_params(throttle):
    """Return (diff, min_inside) for this throttle level, linearly interpolated
    between the STEERING_TABLE rows."""
    t = abs(throttle)
    if t <= STEERING_TABLE[0][0]:
        return STEERING_TABLE[0][1], STEERING_TABLE[0][2]
    for (t0, d0, m0), (t1, d1, m1) in zip(STEERING_TABLE, STEERING_TABLE[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0)
            return d0 + f * (d1 - d0), m0 + f * (m1 - m0)
    return STEERING_TABLE[-1][1], STEERING_TABLE[-1][2]

# Drive power applied to the tracks is self.throttle times this (clamped to
# +/-1.0), so the bot keeps moving forward through turns instead of bogging down
# once the inside track is slowed. Applied before the steering mix in _motor_lr.
# Note the throttle ramp itself (THROTTLE_FLOOR/RAMP) is unchanged.
SPEED_GAIN = 1.3

# Fixed pivot power applied to the outside track when steering is held at a
# dead stop (throttle == 0), where there is no forward speed to keep. Set to the
# same floor used to first clear the drive motor's deadband (see THROTTLE_FLOOR
# above), so a pivot reliably starts moving right away.
STEERING_PIVOT_SPEED = THROTTLE_FLOOR * 1.5


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

    throttle ratchets toward the D-pad's up/down state (-1.0..1.0) one rung per
    press rather than jumping straight there and holds when released; steering
    is a direct level (-1.0/0.0/1.0) tracking left/right's current held state,
    with no ramp and no hold-after-release. A caller in Mode.MANUAL reads
    left/right (not throttle/steering directly) to feed into BotMotor.drive_lr().
    """

    def __init__(self):
        self.throttle = 0.0
        self.steering = 0.0  # -1.0 (left), 0.0 (straight), or 1.0 (right); holds after release
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
        """command is a bot_gamepad.GamepadState (buttons + axes dicts).
            Sets the bot's throttle and steering based on the current button states.
            Does NOT actually send commands to the motors; it only updates the internal state."""
        buttons = command.buttons

        self._recording_toggle.update(buttons.get(BTN_L_RECORD_TOGGLE, False))
        self._follow_toggle.update(buttons.get(BTN_R_FOLLOW_TOGGLE, False))

        if buttons.get(BTN_A_STOP, False):
            # Immediate stop/zero, bypassing the eased ramp and the smoothing.
            # Also drops back out of FOLLOW_BALL -- an emergency stop should hand
            # control back to the human, not leave the bot driving itself.
            self.stop_all()
            return

        if buttons.get(BTN_X_STRAIGHT, False):
            self.steering = 0.0
            return

        up = buttons.get(BTN_UP, False)
        down = buttons.get(BTN_DOWN, False)
        left = buttons.get(BTN_LEFT, False)
        right = buttons.get(BTN_RIGHT, False)

        throttle_target = float(up) - float(down)
        self._update_throttle_target(throttle_target)

        # Steering holds its last direction when the button is released (only
        # X/STRAIGHT or STOP recenters it); zeroing it on release made taps of
        # LEFT/RIGHT cancel themselves out before the bot could turn.
        if right and not left:
            self.steering = 1.0
        elif left and not right:
            self.steering = -1.0

    def stop_all(self):
        """Immediately zero throttle and steering (no easing), and leave
        FOLLOW_BALL. Used by the STOP button and when the gamepad drops out."""
        self.throttle = 0.0
        self._throttle_target = 0.0
        self.steering = 0.0
        self._throttle_press_count = 0
        self._throttle_press_dir = 0
        self._follow_toggle.force_off()

    def drive_state(self):
        """Snapshot of the manual-drive values, for change logging: (throttle_target,
        throttle, presses, steering, left, right)."""
        left, right = self._motor_lr()
        return (round(self._throttle_target, 3), round(self.throttle, 3),
                self._throttle_press_count, self.steering, round(left, 3), round(right, 3))

    @property
    def left(self):
        return self._motor_lr()[0]

    @property
    def right(self):
        return self._motor_lr()[1]

    def _motor_lr(self):
        """Differential-drive mix for BotMotor.drive_lr(). While moving, steering
        keeps BOTH tracks driving: the outside track is raised by half of the
        throttle-dependent diff and the inside track sits the full diff below it,
        so the average speed is kept. Clamped at full power (the outside caps at
        1.0), and the inside track never drops below the throttle-dependent min
        inside, so forward motion continues through the turn instead of stalling
        or spinning. diff and min inside come from _steering_params().

        At a dead stop (throttle == 0) there is no forward motion to preserve, so
        steering pivots the bot at a fixed STEERING_PIVOT_SPEED instead."""
        drive = max(-1.0, min(1.0, self.throttle * SPEED_GAIN))

        if self.steering == 0.0:
            return drive, drive

        if self.throttle == 0.0:        # Pivot in place
            reverse_pivot = -0.5 * STEERING_PIVOT_SPEED
            if self.steering > 0:
                return STEERING_PIVOT_SPEED, reverse_pivot  # steering right -> left track pivots, right stays still
            return reverse_pivot, STEERING_PIVOT_SPEED  # steering left -> right track pivots, left stays still

        # Differential-drive mix for normal driving with steering
        sign = 1.0 if drive > 0 else -1.0
        mag = abs(drive)
        diff, min_inside = _steering_params(self.throttle)
        outside = min(1.0, mag + diff / 2)
        inside = max(min(min_inside, outside), outside - diff)
        outside, inside = sign * outside, sign * inside
        if self.steering > 0:
            return outside, inside  # steering right -> right track is inside
        return inside, outside  # steering left -> left track is inside

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
