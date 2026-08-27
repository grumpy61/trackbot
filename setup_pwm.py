#!/usr/bin/env python3

import sys
import time
from rpi_hardware_pwm import HardwarePWM

# Configuration Map for Raspberry Pi 5 
# On the Pi 5, the rp1 controller block maps:
# Ch 0 -> GPIO 12  |  Ch 1 -> GPIO 13  |  Ch 2 -> GPIO 18  |  Ch 3 -> GPIO 19
PWM_MAPPING = {
    0: {"pin": 12, "channel": 0},
    1: {"pin": 13, "channel": 1},
    2: {"pin": 18, "channel": 2},
    3: {"pin": 19, "channel": 3}
}

def initialize_pwm(frequency_hz=400):
    """
    Initializes all 4 hardware PWM channels on the Raspberry Pi 5.
    Defaults to a 400Hz frequency for zk-bm1 motor controller.
    """
    pwm_controllers = {}
    
    print(f"--- Initializing 4-Channel Hardware PWM (Base Freq: {frequency_hz}Hz) ---")

    use_chip = 0
    for pwm_id, config in PWM_MAPPING.items():
        try:
            # Chip 2 is the standard hardware PWM identifier on the Pi 5 Bookworm / Trixie OS
            pwm_instance = HardwarePWM(pwm_channel=config["channel"], hz=frequency_hz, chip=use_chip)
            
            # Start at a safe 0% duty cycle
            pwm_instance.start(0.0)
            
            pwm_controllers[pwm_id] = pwm_instance
            print(f"Successfully mapped PWM {pwm_id} to GPIO {config['pin']} (Channel {config['channel']})")
            
        except Exception as e:
            print(f"Exception occurred: pwm ID {pwm_id} : {e}", file=sys.stderr)
            print(f"Error initializing PWM {pwm_id} on GPIO {config['pin']}: {e}", file=sys.stderr)
            print("Make sure you appended the 'dtoverlay=pwm,pin=...' commands to /boot/firmware/config.txt", file=sys.stderr)
            
    return pwm_controllers

# Self-contained testing block if script is executed directly
if __name__ == "__main__":
    try:
        pwm = initialize_pwm(frequency_hz=400) # Test at 400Hz

        if len(pwm) == 0:
            print("No PWM channels initialized. Nothing to test.")
            sys.exit(1)

        left_duty_cycle = 35.0
        right_duty_cycle = 45.0
        sleepy_time = 1.5  # seconds

        print(f"Motor A {sleepy_time} seconds forward")
        pwm[0].change_duty_cycle(left_duty_cycle)
        pwm[1].change_duty_cycle(0.0)
        pwm[2].change_duty_cycle(0.0)
        if len(pwm) > 3:
            pwm[3].change_duty_cycle(0.0)

        time.sleep(sleepy_time)

        print(f"Motor B {sleepy_time} seconds forward")
        pwm[0].change_duty_cycle(0.0)
        pwm[1].change_duty_cycle(0.0)
        pwm[2].change_duty_cycle(right_duty_cycle)
        if len(pwm) > 3:
            pwm[3].change_duty_cycle(0.0)

        time.sleep(sleepy_time)

        print(f"Motor A {sleepy_time} seconds back")
        pwm[0].change_duty_cycle(0.0)
        pwm[1].change_duty_cycle(left_duty_cycle)
        pwm[2].change_duty_cycle(0.0)
        if len(pwm) > 3:
            pwm[3].change_duty_cycle(0.0)

        time.sleep(sleepy_time)

        print(f"Motor B {sleepy_time} seconds back")
        pwm[0].change_duty_cycle(0.0)
        pwm[1].change_duty_cycle(0.0)
        pwm[2].change_duty_cycle(0.0)
        if len(pwm) > 3:
            pwm[3].change_duty_cycle(right_duty_cycle)

        time.sleep(sleepy_time)

        print("Test complete.")
        
    except KeyboardInterrupt:
        print("\nTest interrupted by user.")
    finally:
        print("Cleaning up channels...")
        if 'pwm' in locals():
            for pwm_id in pwm:
                pwm[pwm_id].stop()
        print("Done.")
