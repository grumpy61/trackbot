#!/usr/bin/env python3
"""
PWM Control for Raspberry Pi 5
Interactive control of the 4 hardware PWM channels, all on chip 0:
  PWM0: GPIO12
  PWM1: GPIO13
  PWM2: GPIO18
  PWM3: GPIO19

Status reads go straight to sysfs (safe, non-mutating). Writes (enable, frequency,
duty cycle) go through rpi_hardware_pwm.HardwarePWM -- the interface proven working
in setup_pwm.py and bot_motor.py -- instead of this script's old hand-rolled sysfs
writes (which pointed channels 2/3 at pwmchip2, which doesn't exist on this system).
HardwarePWM instances are created lazily, only on the first write to a channel:
constructing one always resets that channel's period and zeroes its duty cycle, so
doing it eagerly for all 4 channels at startup (just to show status) would clobber
whatever was already running.
"""

import sys
from pathlib import Path

from rpi_hardware_pwm import HardwarePWM, HardwarePWMException


class PWMControl:
    """Control PWM channels on Raspberry Pi 5"""

    def __init__(self):
        self.pwm_base = Path("/sys/class/pwm")
        self.pwm_channels = {
            0: {"name": "PWM0", "gpio": 12, "chip": 0, "channel": 0},
            1: {"name": "PWM1", "gpio": 13, "chip": 0, "channel": 1},
            2: {"name": "PWM2", "gpio": 18, "chip": 0, "channel": 2},
            3: {"name": "PWM3", "gpio": 19, "chip": 0, "channel": 3},
        }
        self.pwm_state = {}
        self._hw = {}  # ch_num -> HardwarePWM, created lazily on first write
        self._init_pwm_channels()

    def _init_pwm_channels(self):
        """Read-only: load current state, don't touch hardware."""
        for ch_num, ch_info in self.pwm_channels.items():
            chip = ch_info["chip"]
            channel = ch_info["channel"]
            chip_path = self.pwm_base / f"pwmchip{chip}" / f"pwm{channel}"

            if chip_path.exists():
                self.pwm_state[ch_num] = {
                    "path": chip_path,
                    "enabled": self._read_sysfs(chip_path / "enable"),
                    "frequency": self._get_frequency(chip_path),
                    "duty_cycle": self._read_sysfs(chip_path / "duty_cycle"),
                    "duty_percent": self._get_duty_percent(chip_path) or 0.0,
                }
            else:
                self.pwm_state[ch_num] = {"path": chip_path, "error": "Channel not found"}

    def _read_sysfs(self, path):
        """Read value from sysfs file"""
        try:
            with open(path, 'r') as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _refresh_state(self, ch_num):
        """Re-read a channel's state from sysfs after a write."""
        chip_path = self.pwm_state[ch_num]["path"]
        self.pwm_state[ch_num].update({
            "enabled": self._read_sysfs(chip_path / "enable"),
            "frequency": self._get_frequency(chip_path),
            "duty_cycle": self._read_sysfs(chip_path / "duty_cycle"),
        })

    def _get_frequency(self, chip_path):
        """Calculate frequency from period"""
        period = self._read_sysfs(chip_path / "period")
        if period and period > 0:
            return int(1_000_000_000 / period)  # Convert ns to Hz
        return None

    def _get_duty_percent(self, chip_path):
        """Get duty cycle as percentage"""
        period = self._read_sysfs(chip_path / "period")
        duty = self._read_sysfs(chip_path / "duty_cycle")

        if period and duty is not None and period > 0:
            return (duty / period) * 100
        return None

    def _get_hw(self, ch_num, hz=None):
        """Lazily create/reuse the HardwarePWM instance for this channel."""
        if ch_num not in self._hw:
            info = self.pwm_channels[ch_num]
            # Seed frequency from whatever's already running, if anything, so we
            # don't clobber an active channel's period the moment we first touch it.
            current_hz = self.pwm_state[ch_num].get("frequency")
            self._hw[ch_num] = HardwarePWM(
                pwm_channel=info["channel"], hz=hz or current_hz or 1000, chip=info["chip"]
            )
        return self._hw[ch_num]

    def enable_pwm(self, ch_num):
        """Enable PWM channel, resuming its last-set duty cycle"""
        if ch_num not in self.pwm_state:
            print(f"Invalid channel: {ch_num}")
            return False

        try:
            hw = self._get_hw(ch_num)
            hw.start(self.pwm_state[ch_num].get("duty_percent", 0.0))
        except HardwarePWMException as e:
            print(f"Error: {e}")
            return False

        self._refresh_state(ch_num)
        return True

    def disable_pwm(self, ch_num):
        """Disable PWM channel"""
        if ch_num not in self.pwm_state:
            print(f"Invalid channel: {ch_num}")
            return False

        try:
            self._get_hw(ch_num).stop()
        except HardwarePWMException as e:
            print(f"Error: {e}")
            return False

        self._refresh_state(ch_num)
        return True

    def set_frequency(self, ch_num, freq_hz):
        """Set PWM frequency in Hz"""
        if ch_num not in self.pwm_state:
            print(f"Invalid channel: {ch_num}")
            return False

        if freq_hz <= 0:
            print("Frequency must be positive")
            return False

        try:
            self._get_hw(ch_num, hz=freq_hz).change_frequency(freq_hz)
        except HardwarePWMException as e:
            print(f"Error: {e}")
            return False

        self._refresh_state(ch_num)
        return True

    def set_duty_cycle_percent(self, ch_num, percent):
        """Set duty cycle as percentage (0-100)"""
        if ch_num not in self.pwm_state:
            print(f"Invalid channel: {ch_num}")
            return False

        if not (0 <= percent <= 100):
            print("Duty cycle must be between 0 and 100")
            return False

        try:
            self._get_hw(ch_num).change_duty_cycle(percent)
        except HardwarePWMException as e:
            print(f"Error: {e}")
            return False

        self.pwm_state[ch_num]["duty_percent"] = percent
        self._refresh_state(ch_num)
        return True

    def set_duty_cycle_ns(self, ch_num, duty_ns):
        """Set duty cycle in nanoseconds"""
        if ch_num not in self.pwm_state:
            print(f"Invalid channel: {ch_num}")
            return False

        period = self.pwm_state[ch_num].get("frequency")
        period_ns = (1_000_000_000 / period) if period else self._read_sysfs(
            self.pwm_state[ch_num]["path"] / "period"
        )
        if not period_ns or duty_ns > period_ns:
            print("Duty cycle cannot exceed period")
            return False

        return self.set_duty_cycle_percent(ch_num, (duty_ns / period_ns) * 100)

    def print_status(self, ch_num=None):
        """Print status of PWM channels"""
        channels = [ch_num] if ch_num is not None else self.pwm_channels.keys()

        for ch in channels:
            if ch not in self.pwm_state:
                continue

            state = self.pwm_state[ch]
            info = self.pwm_channels[ch]

            print(f"\n{info['name']} (GPIO {info['gpio']})")
            print("-" * 50)

            if "error" in state:
                print(f"  Error: {state['error']}")
            else:
                enabled = "✓ Enabled" if state["enabled"] else "✗ Disabled"
                print(f"  Status: {enabled}")

                chip_path = state["path"]
                period = self._read_sysfs(chip_path / "period")
                duty = self._read_sysfs(chip_path / "duty_cycle")
                freq = self._get_frequency(chip_path)
                duty_percent = self._get_duty_percent(chip_path)

                if freq:
                    print(f"  Frequency: {freq} Hz")
                if period:
                    print(f"  Period: {period} ns")
                if duty is not None:
                    print(f"  Duty Cycle: {duty} ns", end="")
                    if duty_percent is not None:
                        print(f" ({duty_percent:.1f}%)")
                    else:
                        print()


def interactive_menu(pwm):
    """Interactive menu for PWM control"""
    while True:
        print("\n" + "=" * 50)
        print("PWM Control Menu")
        print("=" * 50)
        print("1. Show status of all PWM channels")
        print("2. Enable PWM channel")
        print("3. Disable PWM channel")
        print("4. Set frequency")
        print("5. Set duty cycle (percent)")
        print("6. Set duty cycle (nanoseconds)")
        print("7. Exit")
        print("=" * 50)

        choice = input("Select option (1-7): ").strip()

        if choice == '1':
            pwm.print_status()

        elif choice == '2':
            try:
                ch = int(input("Enter channel (0-3): "))
                if pwm.enable_pwm(ch):
                    print(f"✓ PWM{ch} enabled")
                else:
                    print(f"✗ Failed to enable PWM{ch}")
            except ValueError:
                print("Invalid input")

        elif choice == '3':
            try:
                ch = int(input("Enter channel (0-3): "))
                if pwm.disable_pwm(ch):
                    print(f"✓ PWM{ch} disabled")
                else:
                    print(f"✗ Failed to disable PWM{ch}")
            except ValueError:
                print("Invalid input")

        elif choice == '4':
            try:
                ch = int(input("Enter channel (0-3): "))
                freq = int(input("Enter frequency in Hz: "))
                if pwm.set_frequency(ch, freq):
                    print(f"✓ PWM{ch} frequency set to {freq} Hz")
                else:
                    print(f"✗ Failed to set frequency")
            except ValueError:
                print("Invalid input")

        elif choice == '5':
            try:
                ch = int(input("Enter channel (0-3): "))
                duty = float(input("Enter duty cycle (0-100%): "))
                if pwm.set_duty_cycle_percent(ch, duty):
                    print(f"✓ PWM{ch} duty cycle set to {duty}%")
                else:
                    print(f"✗ Failed to set duty cycle")
            except ValueError:
                print("Invalid input")

        elif choice == '6':
            try:
                ch = int(input("Enter channel (0-3): "))
                duty_ns = int(input("Enter duty cycle in nanoseconds: "))
                if pwm.set_duty_cycle_ns(ch, duty_ns):
                    print(f"✓ PWM{ch} duty cycle set to {duty_ns} ns")
                else:
                    print(f"✗ Failed to set duty cycle")
            except ValueError:
                print("Invalid input")

        elif choice == '7':
            print("Exiting...")
            break

        else:
            print("Invalid option")


def main():
    print("Raspberry Pi 5 PWM Control")
    print("Initializing PWM channels...")

    pwm = PWMControl()

    # Check if any channels are available
    has_channels = any("error" not in ch for ch in pwm.pwm_state.values())
    if not has_channels:
        print("Error: No PWM channels found. Make sure PWM is enabled in raspi-config")
        sys.exit(1)

    # Show initial status
    pwm.print_status()

    # Start interactive menu
    interactive_menu(pwm)


if __name__ == "__main__":
    main()
