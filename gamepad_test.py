#!/usr/bin/env python3
"""
gamepad_test.py - A simple Python script to test Bluetooth gamepad input on Linux using the evdev library.
This script scans for connected input devices, identifies gamepads or joysticks, and listens for
"""
import evdev
from evdev import InputDevice, categorize, ecodes
import sys

def find_gamepad():
    """Scans system input devices for gamepads or joysticks."""
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    gamepads = []
    
    print("Available input devices:")
    for device in devices:
        print(f" -> Path: {device.path} | Name: {device.name}")

        # Typical controllers contain joystick or gamepad keywords in their subsystem capabilities
        lower_name = device.name.lower()
        if "gamepad" in lower_name or "cinco" in lower_name or "controller" in lower_name or "joystick" in lower_name:
            gamepads.append(device)
            
    return gamepads

def monitor_gamepad(device_path):
    """Listens to the selected gamepad device and prints input data."""
    try:
        device = InputDevice(device_path)
        print(f"\nSuccessfully connected to: {device.name} ({device.path})")
        print("Press buttons or move sticks to test. Press Ctrl+C to exit.\n")
        
        # Loop over incoming hardware events
        for event in device.read_loop():
            # Button presses (Digital Inputs)
            if event.type == ecodes.EV_KEY:
                button_ev = categorize(event)
                # event.value == 1 (pressed), 0 (released), 2 (held down)
                state = "PRESSED" if event.value == 1 else "RELEASED" if event.value == 0 else "HELD"
                print(f"[BUTTON] Code: {event.code} ({button_ev.keycode}) | State: {state}")
                
            # Analog Stick & D-Pad movements (Absolute Axes)
            elif event.type == ecodes.EV_ABS:
                # event.code corresponds to axis ID, event.value is the position integer
                print(f"[AXIS/DPAD] Axis Code: {event.code} | Value: {event.value}")
                
    except KeyboardInterrupt:
        print("\nExiting controller test application.")
    except PermissionError:
        print("\n[ERROR] Permission denied. Try running the script with 'sudo'.")
    except Exception as e:
        print(f"\n[ERROR] Connection lost or issue encountered: {e}")

if __name__ == "__main__":
    detected_pads = find_gamepad()
    
    if not detected_pads:
        print("\n[!] No Bluetooth gamepads found. Ensure your controller is connected and paired.")
        sys.exit(1)
        
    print(f"\nFound {len(detected_pads)} potential controller(s).")
    # Default to the first found controller
    chosen_device = detected_pads[0].path
    monitor_gamepad(chosen_device)

