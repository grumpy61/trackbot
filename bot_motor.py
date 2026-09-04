#!/usr/bin/env python3
"""
bot_motor.py

Converts abstract throttle/steering commands (-1.0..1.0 each) into left/right
motor drive signals for a ZK-BM1 dual H-bridge driver via a differential-drive mix.

Drives the RP1 hardware PWM engine directly via rpi_hardware_pwm.HardwarePWM (chip +
channel), the approach proven working in setup_pwm.py's standalone forward/back test
-- gpiozero.PWMOutputDevice never actually toggled these pins.

Wiring (ZK-BM1), all on chip 0 (per setup_pwm.py's proven test grouping):
    IN1 (motor A / left,  forward) -- GPIO 12, PWM channel 0
    IN2 (motor A / left,  reverse) -- GPIO 13, PWM channel 1
    IN3 (motor B / right, forward) -- GPIO 18, PWM channel 2
    IN4 (motor B / right, reverse) -- GPIO 19, PWM channel 3

Per motor, the ZK-BM1 wants:
    forward: IN_fwd = PWM duty, IN_rev = 0
    reverse: IN_fwd = 0,        IN_rev = PWM duty
    stop:    both 0
    brake:   both 1 (100% duty)
"""

from rpi_hardware_pwm import HardwarePWM

PWM_CHIP = 0
PWM_HZ = 400  # matches setup_pwm.py's proven ZK-BM1 test frequency

LEFT_FWD_CHANNEL = 0   # GPIO 12
LEFT_REV_CHANNEL = 1   # GPIO 13
RIGHT_FWD_CHANNEL = 2  # GPIO 18
RIGHT_REV_CHANNEL = 3  # GPIO 19


def _clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, value))


class Motor:
    """One ZK-BM1 motor channel: a forward PWM channel and a reverse PWM channel."""

    def __init__(self, fwd_channel, rev_channel, reverse=False, hz=PWM_HZ, chip=PWM_CHIP):
        self._fwd = HardwarePWM(pwm_channel=fwd_channel, hz=hz, chip=chip)
        self._rev = HardwarePWM(pwm_channel=rev_channel, hz=hz, chip=chip)
        self._fwd.start(0.0)
        self._rev.start(0.0)
        self._reverse = reverse

    def set_speed(self, value):
        """value: -1.0 (full reverse) .. 1.0 (full forward)."""
        value = _clamp(value)
        if self._reverse:
            value = -value
        if value >= 0:
            self._fwd.change_duty_cycle(value * 100)
            self._rev.change_duty_cycle(0.0)
        else:
            self._fwd.change_duty_cycle(0.0)
            self._rev.change_duty_cycle(-value * 100)

    def stop(self):
        self._fwd.change_duty_cycle(0.0)
        self._rev.change_duty_cycle(0.0)

    def brake(self):
        self._fwd.change_duty_cycle(100.0)
        self._rev.change_duty_cycle(100.0)

    def close(self):
        self._fwd.stop()
        self._rev.stop()


class BotMotor:
    """Differential-drive motor control: throttle/steering -> left/right motor speeds."""

    def __init__(self, left_reverse=False, right_reverse=False):
        self.left = Motor(LEFT_FWD_CHANNEL, LEFT_REV_CHANNEL, reverse=left_reverse)
        self.right = Motor(RIGHT_FWD_CHANNEL, RIGHT_REV_CHANNEL, reverse=right_reverse)

    def drive(self, throttle=0.0, steering=0.0):
        """throttle: -1.0 (full reverse) .. 1.0 (full forward)
        steering: -1.0 (full left) .. 1.0 (full right)
        """
        throttle = _clamp(throttle)
        steering = _clamp(steering)
        self.left.set_speed(_clamp(throttle + steering))
        self.right.set_speed(_clamp(throttle - steering))

    def drive_lr(self, left=0.0, right=0.0):
        """Set left/right track speeds directly (already-mixed, e.g. by
        BotCommandHandler.left/right's asymmetric steering), bypassing the
        throttle +/- steering mix that drive() uses."""
        self.left.set_speed(_clamp(left))
        self.right.set_speed(_clamp(right))

    def stop(self):
        self.left.stop()
        self.right.stop()

    def brake(self):
        self.left.brake()
        self.right.brake()

    def close(self):
        self.left.close()
        self.right.close()


if __name__ == "__main__":
    import time

    motor = BotMotor()
    try:
        print("Forward...")
        motor.drive(throttle=0.3, steering=0.0)
        time.sleep(1)
        print("Turn right...")
        motor.drive(throttle=0.3, steering=0.1)
        time.sleep(1)
        print("Stop.")
        motor.stop()
    finally:
        motor.close()
