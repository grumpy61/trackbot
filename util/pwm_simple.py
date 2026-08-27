#!/usr/bin/env python3
"""
Simple command-line PWM control for Raspberry Pi 5

Uses rpi_hardware_pwm.HardwarePWM on chip 0 -- the interface proven working in
setup_pwm.py and bot_motor.py, in place of this script's old hand-rolled sysfs
writes (which pointed channels 2/3 at pwmchip2, which doesn't exist on this
system, and never exported a channel that wasn't already exported).

Usage:
  python3 pwm_simple.py <channel> <frequency_hz> <duty_cycle_%>
  python3 pwm_simple.py off <channel>

Examples:
  python3 pwm_simple.py 0 1000 50    # PWM channel 0 (GPIO 12) at 1kHz, 50% duty
  python3 pwm_simple.py 2 10000 75   # PWM channel 2 (GPIO 18) at 10kHz, 75% duty
"""

import sys
from pathlib import Path

from rpi_hardware_pwm import HardwarePWM, HardwarePWMException

PWM_CHIP = 0  # all 4 channels live on chip 0 -- see bot_motor.py


class SimplePWM:
    """Thin CLI wrapper around HardwarePWM for one-off channel testing."""

    def _pre_zero_duty(self, channel):
        """HardwarePWM's own docs: changing period fails with "write error:
        Invalid argument" unless duty_cycle is already 0. A fresh HardwarePWM
        instance assumes duty_cycle starts at 0, which is wrong if the channel
        was left running (nonzero) by a previous invocation of this script --
        so zero it directly first."""
        duty_path = Path(f"/sys/class/pwm/pwmchip{PWM_CHIP}/pwm{channel}/duty_cycle")
        if duty_path.exists():
            try:
                duty_path.write_text("0")
            except OSError:
                pass

    def setup(self, channel, freq_hz, duty_percent):
        """Setup PWM channel"""
        if channel not in (0, 1, 2, 3):
            print(f"Error: Invalid channel {channel} (0-3)", file=sys.stderr)
            return False

        if not (1 <= freq_hz <= 1_000_000):
            print("Error: Frequency must be 1-1,000,000 Hz", file=sys.stderr)
            return False

        if not (0 <= duty_percent <= 100):
            print("Error: Duty cycle must be 0-100%", file=sys.stderr)
            return False

        self._pre_zero_duty(channel)
        try:
            pwm = HardwarePWM(pwm_channel=channel, hz=freq_hz, chip=PWM_CHIP)
            pwm.start(duty_percent)
        except HardwarePWMException as e:
            print(f"Error: {e}", file=sys.stderr)
            return False

        print(f"PWM{channel}: {freq_hz} Hz, {duty_percent}% duty cycle", file=sys.stderr)
        return True

    def disable(self, channel):
        """Disable PWM channel"""
        if channel not in (0, 1, 2, 3):
            print(f"Error: Invalid channel {channel}", file=sys.stderr)
            return False

        self._pre_zero_duty(channel)
        try:
            pwm = HardwarePWM(pwm_channel=channel, hz=1000, chip=PWM_CHIP)
            pwm.stop()
        except HardwarePWMException as e:
            print(f"Error: {e}", file=sys.stderr)
            return False

        return True


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--help":
        print(__doc__)
        sys.exit(0)

    if len(sys.argv) == 3 and sys.argv[1] == "off":
        # Disable mode: pwm_simple.py off <channel>
        channel = int(sys.argv[2])
        pwm = SimplePWM()
        if pwm.disable(channel):
            print(f"PWM{channel} disabled")
        else:
            sys.exit(1)

    elif len(sys.argv) == 4:
        # Setup mode: pwm_simple.py <channel> <freq> <duty>
        try:
            channel = int(sys.argv[1])
            freq = int(sys.argv[2])
            duty = float(sys.argv[3])

            pwm = SimplePWM()
            if pwm.setup(channel, freq, duty):
                sys.exit(0)
            else:
                sys.exit(1)
        except ValueError:
            print("Error: Invalid arguments", file=sys.stderr)
            print(__doc__)
            sys.exit(1)

    else:
        print("Usage: python3 pwm_simple.py <channel> <frequency_hz> <duty_%>")
        print("       python3 pwm_simple.py off <channel>")
        print("       python3 pwm_simple.py --help")
        sys.exit(1)


if __name__ == "__main__":
    main()
