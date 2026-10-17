#!/usr/bin/env python3
"""
run_trackbot.py

High-level main loop for trackbot. Owns the operating mode and ticks the
active behavior each cycle -- currently ball-following via YellowBallTracker.

Extension points for future work:
    BTGamepadController  -- bluetooth gamepad input, see bot_gamepad.py
    SensorHub   -- onboard sensor processing (read() is a stub for now)
    Mode switching -- --start-mode picks the base mode (idle/manual) to fall back
                      to; Mode.FOLLOW_BALL is layered on top of that at runtime via
                      the follow-ball button (BTN_R_FOLLOW_TOGGLE, see bot_commands.py)

Usage:
    python3 run_trackbot.py --model ssd --color-check --color-space lab --show-preview
"""

import argparse
import random
import subprocess
import sys
import threading
import time
import tkinter as tk
from enum import Enum, auto
from pathlib import Path

from bot_commands import (
    BotCommandHandler, STOP_HOLD_SHUTDOWN_S, SPEED_GAIN, THROTTLE_FLOOR, THROTTLE_RAMP_STEP,
    BTN_B_RANDOM_SOUNDS,
)
from bot_gamepad import BTGamepadController, GamepadState
from bot_motor import BotMotor
from plain_camera import PlainCameraViewer
from track_yellow_ball import YellowBallTracker
from trackbot_audio import TrackbotAudio
from trackbot_window import TrackbotWindow


class Mode(Enum):
    IDLE = auto()
    FOLLOW_BALL = auto()
    MANUAL = auto()


class SensorHub:
    """Placeholder for onboard sensor processing (e.g. bump/IR/ultrasonic).
    read() always reports nothing until sensors are wired up."""

    def read(self):
        return None  # TODO: read and return sensor state


FOLLOW_TARGET_SIZE_FRAC = 0.05  # fallback target size, used until FOLLOW_SIZE_HISTORY_MIN_SAMPLES is reached

# Forward drive while chasing: any forward command is at least FOLLOW_MIN_THROTTLE
# (the drive power that first clears the motors' deadband -- manual driving's
# first moving throttle step times its gain), scaling up to FOLLOW_MAX_THROTTLE
# for a ball that looks very far. The max is the old 0.4 cap plus four manual
# throttle steps, for a more aggressive chase.
FOLLOW_MIN_THROTTLE = THROTTLE_FLOOR * SPEED_GAIN
FOLLOW_MAX_THROTTLE = 0.4 + 4 * THROTTLE_RAMP_STEP

# Once the tracker has seen at least this many accepted detections, "far away"
# is judged against the sizes it has actually seen: the ball counts as far (so
# the bot drives forward) while it's smaller than FOLLOW_FAR_FRACTION_OF_MAX of
# the largest size seen so far (tracker.max_size_frac). Below this many samples
# there isn't enough size history to judge against yet, so
# _follow_ball_throttle_steering() falls back to the fixed FOLLOW_TARGET_SIZE_FRAC.
FOLLOW_SIZE_HISTORY_MIN_SAMPLES = 10
FOLLOW_FAR_FRACTION_OF_MAX = 0.66

# Ball within this much of center (normalized dx, 0..1) is treated as "centered"
# -- no steering correction at all, rather than constantly micro-steering at the
# slightest offset. Beyond the deadband, steering is rescaled back up to still
# reach full lock at the frame edge (dx = +/-1), so there's no jump at the edge
# of the zone. dx = +/-1 spans center-to-edge (half the frame width) each way,
# so this threshold value doubles as the deadband's total width as a fraction
# of the FULL frame width -- 0.20 here means a zone ~20% of the screen wide.
FOLLOW_CENTER_DEADBAND = 0.20

# Played when a new ball is detected -- one is picked at random each time so it
# doesn't get repetitive. Populated at startup by load_ball_sounds() below, from
# every .wav file found in ./sounds/BALL_FOUND_SOUNDS_DIR.
BALL_SOUND_PATHS = []
BALL_FOUND_SOUNDS_DIR = "ballfound"
BALL_FOUND_SOUNDS_DEFAULT = ["./sounds/ball.wav"]  # used if that folder is missing/empty

# Played once after a previously-tracked ball goes missing for a while -- one is
# picked at random, and it only plays once per "loss episode" (see ball_lost_time/
# lost_ball_sound_played in mainloop()), not repeatedly for as long as it stays lost.
# Populated at startup by load_ball_sounds() below, from every .wav file found in
# ./sounds/BALL_LOST_SOUNDS_DIR.
LOST_BALL_SOUND_PATHS = []
BALL_LOST_SOUNDS_DIR = "balllost"
BALL_LOST_SOUNDS_DEFAULT = ["./sounds/noball.wav"]  # used if that folder is missing/empty


def _load_sounds_into(paths_list, folder, default_paths):
    """Clear and repopulate `paths_list` (a module-level list, mutated in place
    so callers that already hold a reference to it keep seeing the update) with
    every .wav file found in ./sounds/<folder>, sorted. Falls back to
    `default_paths` if that folder is missing or has no .wav files in it, so
    the list is never left empty."""
    found = sorted(str(p) for p in (Path("./sounds") / folder).glob("*.wav"))
    paths_list.clear()
    paths_list.extend(found if found else default_paths)


def load_ball_sounds():
    """(Re)populate BALL_SOUND_PATHS and LOST_BALL_SOUND_PATHS from
    ./sounds/BALL_FOUND_SOUNDS_DIR and ./sounds/BALL_LOST_SOUNDS_DIR respectively."""
    _load_sounds_into(BALL_SOUND_PATHS, BALL_FOUND_SOUNDS_DIR, BALL_FOUND_SOUNDS_DEFAULT)
    _load_sounds_into(LOST_BALL_SOUND_PATHS, BALL_LOST_SOUNDS_DIR, BALL_LOST_SOUNDS_DEFAULT)

# Change in audio.volume per step while BTN_B_RANDOM_SOUNDS is held and UP/DOWN
# is pressed (see command_handler.consume_volume_delta() in mainloop()).
VOLUME_STEP = 0.10

# Played on demand, once per press of BTN_B_RANDOM_SOUNDS -- one is picked at random.
# Populated at startup (from RANDOM_SOUNDS_FOLDERS[0]) by load_random_sounds()
# below, from every .wav file found in that folder (under ./sounds/) -- drop a
# file in there to add it, no code change needed. Holding BTN_B_RANDOM_SOUNDS
# for RANDOM_SOUNDS_RELOAD_HOLD_S (see bot_commands.py) cycles to the next
# folder in this list via cycle_random_sounds_folder(), wrapping back to the
# first after the last. Add more folder names here.
RANDOM_SOUNDS_BUTTON_PATHS = []
RANDOM_SOUNDS_FOLDERS = ["random", "nokings"]
_random_sounds_folder_index = 0

# Never one of the random picks in RANDOM_SOUNDS_BUTTON_PATHS -- each folder can
# have its own copy of this exact filename, and if present it's played once
# right after that folder is (re)loaded instead, as an audible "now using this
# folder" cue. See load_random_sounds().
USING_THIS_FOLDER_SOUND_NAME = "usingthisfolder.wav"


RANDOM_SOUNDS_EXTENSIONS = ("*.wav", "*.mp3")  # pw-play (via TrackbotAudio) handles both fine


def load_random_sounds(folder=None, audio=None):
    """Clear and repopulate RANDOM_SOUNDS_BUTTON_PATHS with every file matching
    RANDOM_SOUNDS_EXTENSIONS found in ./sounds/<folder> --
    RANDOM_SOUNDS_FOLDERS[_random_sounds_folder_index] (the current folder) if
    `folder` isn't given -- except USING_THIS_FOLDER_SOUND_NAME, which is
    excluded from the list and, if present and `audio` is given, queued once
    instead right after the reload. Mutates RANDOM_SOUNDS_BUTTON_PATHS in
    place (clear + extend) rather than rebinding the name, so callers that
    already hold a reference to it (e.g. random.choice(RANDOM_SOUNDS_BUTTON_PATHS)
    in mainloop()) see the update too. Returns the folder name actually loaded."""
    if folder is None:
        folder = RANDOM_SOUNDS_FOLDERS[_random_sounds_folder_index]
    directory = Path("./sounds") / folder
    found = (path for pattern in RANDOM_SOUNDS_EXTENSIONS for path in directory.glob(pattern))
    RANDOM_SOUNDS_BUTTON_PATHS.clear()
    RANDOM_SOUNDS_BUTTON_PATHS.extend(
        str(path) for path in sorted(found)
        if path.name != USING_THIS_FOLDER_SOUND_NAME
    )
    if audio is not None:
        using_this_folder_path = directory / USING_THIS_FOLDER_SOUND_NAME
        if using_this_folder_path.is_file():
            audio.queue(str(using_this_folder_path))
    return folder


def cycle_random_sounds_folder(audio=None):
    """Advance to the next folder in RANDOM_SOUNDS_FOLDERS (wrapping around) and
    reload RANDOM_SOUNDS_BUTTON_PATHS from it. Returns the new folder's name."""
    global _random_sounds_folder_index
    _random_sounds_folder_index = (_random_sounds_folder_index + 1) % len(RANDOM_SOUNDS_FOLDERS)
    return load_random_sounds(audio=audio)

BALL_SOUND_COOLDOWN_MIN_S = 3.0  # don't replay the sound sooner than this after the last one...
BALL_SOUND_COOLDOWN_MAX_S = 8.0  # ...up to this long, picked at random each time (see _random_ball_sound_cooldown)


def _random_ball_sound_cooldown():
    return random.uniform(BALL_SOUND_COOLDOWN_MIN_S, BALL_SOUND_COOLDOWN_MAX_S)


def _random_choice_excluding(paths, previous):
    """random.choice(paths), but never `previous` again if paths has more than
    one entry -- so the same sound never plays twice in a row. Falls back to
    plain random.choice() (previous included) when paths has only one entry,
    so a single-sound list still works."""
    candidates = [p for p in paths if p != previous] if len(paths) > 1 else paths
    return random.choice(candidates)


GAMEPAD_CONNECTED_SOUND_PATH = "./sounds/gamepadconnected.wav"
GAMEPAD_DISCONNECTED_SOUND_PATH = "./sounds/gamepadnotfound.wav"

VIDEO_ON_SOUND_PATH = "./sounds/videoison.wav"
VIDEO_OFF_SOUND_PATH = "./sounds/videoisoff.wav"

FOLLOWING_ON_SOUND_PATH = "./sounds/followingison.wav"
FOLLOWING_OFF_SOUND_PATH = "./sounds/followingisoff.wav"

ETHERNET_CONNECTED_SOUND_PATH = "./sounds/ethernetconnected.wav"
WIFI_CONNECTED_SOUND_PATH = "./sounds/wificonnected.wav"  # fallback for a wifi network not in NETWORK_NAME_SOUND_PATHS
NO_NETWORK_SOUND_PATH = "./sounds/nonetworksfound.wav"

# If a connected wifi network's name (case-insensitively) matches one of these,
# play that clip instead of the generic WIFI_CONNECTED_SOUND_PATH.
NETWORK_NAME_SOUND_PATHS = {
    "mercury": "./sounds/mercury.wav",
    "trackbot": "./sounds/trackbot.wav",
    "jupiter": "./sounds/jupiter.wav",
    "mars": "./sounds/mars.wav",
    "cinco": "./sounds/cinco.wav",
    "venus": "./sounds/venus.wav",
}

SHUTDOWN_SOUND_PATH = "./sounds/shuttingdown.wav"
READY_SOUND_PATH = "./sounds/readytoroll.wav"  # queued right after the startup gamepad-status announcement


def _follow_ball_steering(dx):
    if abs(dx) <= FOLLOW_CENTER_DEADBAND:
        return 0.0
    sign = 1.0 if dx > 0.0 else -1.0
    return sign * (abs(dx) - FOLLOW_CENTER_DEADBAND) / (1.0 - FOLLOW_CENTER_DEADBAND)


def _follow_ball_throttle_steering(result, tracker):
    """First-pass proportional control law: steer toward the ball's dx (once outside
    the FOLLOW_CENTER_DEADBAND), drive forward while the ball looks smaller (farther)
    than the target size, and stop (no reverse) once it's at/above it. Needs tuning
    on real hardware.

    The target size is FOLLOW_FAR_FRACTION_OF_MAX of the largest size the tracker
    has seen (tracker.max_size_frac) once there have been at least
    FOLLOW_SIZE_HISTORY_MIN_SAMPLES detections -- so "far" is relative to how big
    the ball has actually looked, not a fixed guess -- else the fixed
    FOLLOW_TARGET_SIZE_FRAC. Forward throttle then runs from FOLLOW_MIN_THROTTLE
    (just at the target) up to FOLLOW_MAX_THROTTLE (ball tiny)."""
    steering = _follow_ball_steering(result.dx)
    if tracker.size_sample_count >= FOLLOW_SIZE_HISTORY_MIN_SAMPLES:
        target_size = FOLLOW_FAR_FRACTION_OF_MAX * tracker.max_size_frac
    else:
        target_size = FOLLOW_TARGET_SIZE_FRAC
    if result.size_frac >= target_size:
        return 0.0, steering
    gap = (target_size - result.size_frac) / target_size  # 0..1, bigger = farther
    throttle = FOLLOW_MIN_THROTTLE + (FOLLOW_MAX_THROTTLE - FOLLOW_MIN_THROTTLE) * gap
    return throttle, steering


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
    parser.add_argument("--start-mode", choices=["idle", "manual"], default="manual",
                         help="Base operating mode to start in (Mode.FOLLOW_BALL isn't a start "
                              "mode -- it's toggled at runtime with the follow-ball button)")
    parser.add_argument("--record-preview", action=argparse.BooleanOptionalAction, default=False,
                         help="Record camera video to ~/Videos/Trackbot (default: off)")
    parser.add_argument("--upside-down", action=argparse.BooleanOptionalAction, default=True,
                         help="Camera is mounted upside down, so flip h/v (default: on; "
                              "pass --no-upside-down if it's mounted right-side up)")
    return parser.parse_args()


# --record-preview starts the encoder/muxer this long after the mainloop begins
# running, rather than during tracker.start() -- starting it before the camera
# and mainloop have settled was observed to hang the whole program.
RECORD_AUTOSTART_DELAY_S = 1.0

# Pause per MANUAL/IDLE loop pass. Kept short: gamepad events are only read once
# per pass, so this is the floor on input latency.
MAINLOOP_IDLE_SLEEP_S = 0.01


def mainloop(tracker, motor, start_mode="manual", debug=False, audio=None, network_state=None):
    if audio is None:
        audio = TrackbotAudio()
    window = TrackbotWindow()
    window_closed = False
    controller = BTGamepadController(verbose=debug)
    command_handler = BotCommandHandler()
    sensors = SensorHub()
    base_mode = Mode[start_mode.upper()]  # the mode to fall back to when FOLLOW_BALL is off
    window.set_mode(base_mode.name)
    window.set_video_status("Recording" if tracker.video_recorder.recording else "Off")
    last_manual_msg = None  # last MANUAL-mode drive message printed, so it isn't repeated when unchanged
    last_follow_drive_msg = None  # last FOLLOW_BALL "motor.drive(...)" message, so it isn't repeated when unchanged
    last_follow_manual_msg = None  # last FOLLOW_BALL "ball not found" message, so it isn't repeated when unchanged
    last_idle_msg = None  # last IDLE message, so it isn't repeated every iteration while idle
    last_recording_state = tracker.video_recorder.recording
    last_follow_state = command_handler.follow_ball
    last_mode = base_mode  # for logging entering/leaving ball tracking mode below
    mainloop_start_time = time.monotonic()
    pending_record_autostart = tracker.video_recorder.enabled  # --record-preview was passed
    ready_sound_played = False  # see "Play once video autostart has resolved" below
    ball_was_found = False
    last_ball_sound_time = -BALL_SOUND_COOLDOWN_MAX_S  # so the very first detection can play
    next_ball_sound_cooldown = _random_ball_sound_cooldown()
    ball_lost_time = None  # monotonic time the ball was last seen going missing, or None
    ball_lost_sound_delay = None  # this episode's randomly-picked delay before playing a "lost" sound
    lost_ball_sound_played = False  # so it only plays once per loss episode, not repeatedly
    # Last pick from each of BALL_SOUND_PATHS/LOST_BALL_SOUND_PATHS/RANDOM_SOUNDS_BUTTON_PATHS,
    # so _random_choice_excluding() never repeats the same one twice in a row.
    last_ball_found_sound_path = None
    last_lost_ball_sound_path = None
    last_random_sounds_button_path = None
    last_drive_state = None  # last logged (throttle/steering/left/right) snapshot, to log only changes
    last_effective_buttons = {}  # last (real gamepad | virtual window) merged button state we acted on
    gamepad_in_use = False  # real gamepad has sent input since it (re)connected -- arms the dropout fail-safe

    # Seeded from _log_network_status()'s own startup result (if given) so the
    # first periodic re-check below doesn't immediately re-announce the same
    # thing again; (set(), []) if it wasn't given one (e.g. a test harness).
    last_network_types, last_network_wifi_names = network_state or (set(), [])
    last_network_check_time = time.monotonic()
    network_check_thread = None  # background nmcli re-check in flight, if any
    network_check_result = []  # its (connected_types, connected, wifi_names), once done

    # Announce the gamepad's connection state as of startup, then again on every
    # change (BTGamepadController.poll() reconnects/disconnects automatically).
    # queue() (not play()) so this waits behind the network-status announcement
    # (also queued, in _log_network_status()) instead of cutting it off if it's
    # still playing.
    gamepad_was_connected = controller.device is not None
    window.set_gamepad_status("Connected" if gamepad_was_connected else "Not connected")
    print(f"[mainloop] Gamepad {'connected' if gamepad_was_connected else 'not connected'} at startup")
    audio.queue(GAMEPAD_CONNECTED_SOUND_PATH if gamepad_was_connected else GAMEPAD_DISCONNECTED_SOUND_PATH,
                pause_after=True)

    # PlainCameraViewer.tick() never finds anything -- no AI camera is attached, so
    # FOLLOW_BALL can't do anything but spam "ball not found". Block the toggle from
    # taking effect in that case rather than silently failing to drive.
    follow_ball_available = not isinstance(tracker, PlainCameraViewer)

    def debug_print(msg):
        # No dedup here -- callers that need "only when changed" (most of them)
        # track their own last-printed value and only call this when it differs;
        # a single shared last-message check doesn't work once more than one
        # call site can fire in the same iteration (e.g. [drive] every iteration
        # alongside a mode-specific message), since each would see the other's
        # text as "different" and both would re-print every iteration forever.
        if debug:
            print(msg)

    try:
        while True:
            controller.poll()  # updates controller.state.buttons as a side effect; return value unused below

            # Merge the real gamepad's live buttons with the virtual gamepad's
            # (window.virtual_buttons, keyed by the same evdev keycodes) so a
            # window button press drives BotCommandHandler exactly like a real
            # one would. Re-run process_command() either when the combined state
            # actually changed (a genuinely new press/release), or when the real
            # gamepad reported repeat_codes -- its own PRESSED/HELD (autorepeat)
            # events for this cycle, per gamepad_test.py -- so holding a button
            # down acts like tapping it repeatedly, driven by the hardware's own
            # repeat events rather than a synthetic timer (though throttled to a
            # fixed cadence, not the hardware's actual repeat rate -- see
            # HELD_REPEAT_INTERVAL_S in bot_gamepad.py). (The virtual window
            # gamepad has no such repeat signal, so holding it down there doesn't
            # auto-repeat -- only a real gamepad's own held buttons do.)
            if any(controller.state.buttons.values()) or controller.state.repeat_codes:
                gamepad_in_use = True  # the real gamepad (not just the window) has actually given input
            effective_buttons = dict(controller.state.buttons)
            for keycode, pressed in window.virtual_buttons.items():
                if pressed:
                    effective_buttons[keycode] = True
            repeat_codes = controller.state.repeat_codes
            if effective_buttons != last_effective_buttons or repeat_codes:
                last_effective_buttons = effective_buttons
                # A quick tap can press AND release within one poll() batch, so the
                # live state already shows it released and the press would be lost
                # entirely. Show process_command() the press first, then the real
                # (released) state so its press/release edge detection resets.
                # BTN_B_RANDOM_SOUNDS is excluded: it's a modifier (UP/DOWN mean
                # volume, not throttle, while it's down -- see process_command),
                # so replaying a vanished B-tap as "B held" for one call and "B
                # released" for the next let a held DOWN fall through to a real
                # throttle change on the second call -- moving the bot even though
                # B logically never stopped being held for this interaction.
                missed_taps = {
                    c for c in repeat_codes
                    if not effective_buttons.get(c, False) and c != BTN_B_RANDOM_SOUNDS
                }
                if missed_taps:
                    tap_buttons = dict(effective_buttons)
                    tap_buttons.update({c: True for c in missed_taps})
                    command_handler.process_command(GamepadState(buttons=tap_buttons))
                command_handler.process_command(GamepadState(buttons=effective_buttons))
            command_handler.tick()  # ease throttle toward its target, independent of new events

            drive_state = command_handler.drive_state()
            if drive_state != last_drive_state:
                last_drive_state = drive_state
                target, throttle_now, presses, steer, left_now, right_now = drive_state
                debug_print(f"[drive] throttle={throttle_now:.3f} (target={target:.3f}, presses={presses}) "
                            f"steering={steer:+.0f} -> left={left_now:.3f} right={right_now:.3f}")

            gamepad_connected = controller.device is not None
            if gamepad_connected != gamepad_was_connected:
                gamepad_was_connected = gamepad_connected
                if not window_closed:
                    try:
                        window.set_gamepad_status("Connected" if gamepad_connected else "Not connected")
                    except tk.TclError:
                        pass  # window was closed; tick() below notices and stops touching it
                print(f"[mainloop] Gamepad {'connected' if gamepad_connected else 'disconnected'}")
                audio.queue(GAMEPAD_CONNECTED_SOUND_PATH if gamepad_connected else GAMEPAD_DISCONNECTED_SOUND_PATH)
                if not gamepad_connected:
                    # Fail-safe only if the gamepad was actually the thing driving:
                    # then leave FOLLOW_BALL and stop the bot rather than keep going
                    # on its last command -- e.g. it went out of range mid-drive. If
                    # it was never used (e.g. the bot is being driven from the
                    # window's virtual buttons), leave things be. BTGamepadController
                    # keeps trying to reconnect on its own in the background (fast
                    # device-node rescan plus an active bluetoothctl connect retry --
                    # see RECONNECT_INTERVAL_S/BT_RECONNECT_INTERVAL_S in
                    # bot_gamepad.py), so driving resumes as soon as it's back.
                    if gamepad_in_use:
                        print("[mainloop] Gamepad lost while in use -> leaving follow mode, "
                              "throttle and steering zeroed")
                        command_handler.stop_all()
                    gamepad_in_use = False

            # Uses live button state (effective_buttons), not just new/repeat events,
            # since measuring a continuous hold needs to sample "is it down right
            # now" every iteration -- the real gamepad's own HELD events are
            # throttled to once per HELD_REPEAT_INTERVAL_S (see bot_gamepad.py), too
            # coarse to time a hold by themselves, and the virtual window has no
            # event/repeat concept at all, just a live pressed/not-pressed dict.
            command_handler.check_shutdown_hold(effective_buttons)
            if command_handler.shutdown_requested:
                print(f"[mainloop] 'A' button held for {STOP_HOLD_SHUTDOWN_S:g}s -> shutting down")
                break

            command_handler.check_random_sounds_reload_hold(effective_buttons)
            if command_handler.consume_random_sounds_reload_request():
                folder = cycle_random_sounds_folder(audio=audio)
                print(f"[mainloop] 'B' button held -> reloaded RANDOM_SOUNDS_BUTTON_PATHS "
                      f"from ./sounds/{folder} ({len(RANDOM_SOUNDS_BUTTON_PATHS)} files)")

            # Start video recording if the auto-start delay has passed
            if pending_record_autostart and time.monotonic() - mainloop_start_time >= RECORD_AUTOSTART_DELAY_S:
                pending_record_autostart = False
                print(f"[mainloop] --record-preview: starting video recording "
                      f"({RECORD_AUTOSTART_DELAY_S:g}s after mainloop start)")
                command_handler.begin_recording()

            # Announce changes in video recording state
            if command_handler.recording != last_recording_state:
                last_recording_state = command_handler.recording
                if command_handler.recording:
                    tracker.video_recorder.resume()
                    print(f"[mainloop] Video recording ON -> {tracker.video_recorder.current_path}")
                    audio.queue(VIDEO_ON_SOUND_PATH)
                    window.set_video_status("Recording")
                else:
                    tracker.video_recorder.pause()
                    print(f"[mainloop] Video recording OFF -> {tracker.video_recorder.current_path}")
                    audio.queue(VIDEO_OFF_SOUND_PATH)
                    window.set_video_status("Off")

            # Play once video autostart has resolved -- either it just fired above,
            # or (if --record-preview wasn't passed) there was never anything
            # pending, so this fires right away on the very first iteration.
            if not ready_sound_played and time.monotonic() - mainloop_start_time >= RECORD_AUTOSTART_DELAY_S:
                ready_sound_played = True
                audio.queue(READY_SOUND_PATH)

            # B button: play a random sound on demand, once per press
            if command_handler.consume_random_sound_request():
                last_random_sounds_button_path = _random_choice_excluding(
                    RANDOM_SOUNDS_BUTTON_PATHS, last_random_sounds_button_path)
                audio.queue(last_random_sounds_button_path)

            # Drives the SEGMENT_SECONDS file rollover. tracker.tick() only does this in
            # FOLLOW_BALL mode, so without this call a recording made in MANUAL/IDLE
            # never rolled over and grew into one multi-GB file (seen: 2.6 GB / 2.4 h).
            tracker.video_recorder.tick()

            # Announce changes in ball-following state
            if command_handler.follow_ball != last_follow_state:
                last_follow_state = command_handler.follow_ball
                if command_handler.follow_ball and not follow_ball_available:
                    print("[mainloop] Right trigger pressed -> FOLLOW_BALL requested, but no AI camera "
                          f"available -- staying in {base_mode.name}")
                elif command_handler.follow_ball:
                    print("[mainloop] Ball-following enabled")
                    audio.queue(FOLLOWING_ON_SOUND_PATH)
                else:
                    print("[mainloop] Ball-following disabled")
                    audio.queue(FOLLOWING_OFF_SOUND_PATH)

            mode = Mode.FOLLOW_BALL if (command_handler.follow_ball and follow_ball_available) else base_mode

            # B held + UP/DOWN: volume modifier (see process_command). Adjusts
            # audio.ball_volume (BALL_SOUND_PATHS/LOST_BALL_SOUND_PATHS) while in
            # FOLLOW_BALL, else the general audio.volume everywhere else.
            volume_delta = command_handler.consume_volume_delta()
            if volume_delta:
                if mode is Mode.FOLLOW_BALL:
                    new_volume = audio.set_ball_volume(audio.ball_volume + volume_delta * VOLUME_STEP)
                    label = "ball_volume"
                else:
                    new_volume = audio.set_volume(audio.volume + volume_delta * VOLUME_STEP)
                    label = "volume"
                print(f"[mainloop] 'B' + {'UP' if volume_delta > 0 else 'DOWN'} -> {label}={new_volume:.2f}")
                audio.queue(f"./sounds/volume{round(new_volume * 100)}.wav")

            # Always log the actual mode boundary being crossed (not just the button
            # press above), so this stays correct regardless of what triggers a change.
            if mode != last_mode:
                if mode is Mode.FOLLOW_BALL:
                    print("[mainloop] Entering ball tracking mode")
                    # Fresh start each time -- don't let a stale "lost the ball"
                    # timer from a previous follow-mode session carry over.
                    ball_was_found = False
                    ball_lost_time = None
                    lost_ball_sound_played = False
                else:
                    print(f"[mainloop] Leaving ball tracking mode -> {mode.name}")
                last_mode = mode
                window.set_mode(mode.name)

            if not window_closed:
                try:
                    window.tick()
                except tk.TclError:
                    # User closed the window -- keep driving, just stop touching it.
                    print("[mainloop] Trackbot window closed")
                    window_closed = True

            if window.quit_requested:
                print("[mainloop] Quit button confirmed -> shutting down")
                break

            # Re-check network status periodically and (re-)announce only on an
            # actual change -- e.g. wifi/ethernet finishing association well after
            # the startup check gave up and reported "no network" (see
            # NETWORK_RECHECK_INTERVAL_S), or a cable/network dropping out later.
            # The nmcli query runs on its own thread so a slow/hung call can't stall
            # the drive loop; its result is picked up on a later pass.
            if network_check_thread is not None and not network_check_thread.is_alive():
                network_check_thread = None
                if network_check_result:
                    connected_types, connected, wifi_names = network_check_result.pop()
                    if connected_types != last_network_types or wifi_names != last_network_wifi_names:
                        last_network_types, last_network_wifi_names = connected_types, wifi_names
                        _announce_network(audio, connected_types, connected, wifi_names)
            if (network_check_thread is None
                    and time.monotonic() - last_network_check_time >= NETWORK_RECHECK_INTERVAL_S):
                last_network_check_time = time.monotonic()
                network_check_thread = threading.Thread(
                    target=lambda: network_check_result.append(_check_network_once()), daemon=True)
                network_check_thread.start()

            sensors.read()  # TODO: react to sensor state (e.g. obstacle stop) once wired up

            if mode is Mode.FOLLOW_BALL:
                result = tracker.tick()
                if result is not None:
                    if not ball_was_found and time.monotonic() - last_ball_sound_time >= next_ball_sound_cooldown:
                        last_ball_sound_time = time.monotonic()
                        next_ball_sound_cooldown = _random_ball_sound_cooldown()
                        last_ball_found_sound_path = _random_choice_excluding(
                            BALL_SOUND_PATHS, last_ball_found_sound_path)
                        audio.queue(last_ball_found_sound_path, volume=audio.ball_volume)
                    ball_was_found = True
                    ball_lost_time = None
                    lost_ball_sound_played = False

                    throttle, steering = _follow_ball_throttle_steering(result, tracker)
                    follow_drive_msg = f"[mainloop] FOLLOW_BALL: motor.drive(throttle={throttle:.3f}, steering={steering:.3f})"
                    if follow_drive_msg != last_follow_drive_msg:  # only print on change
                        debug_print(follow_drive_msg)
                        last_follow_drive_msg = follow_drive_msg
                    motor.drive(throttle=throttle, steering=steering)
                else:
                    # No ball currently detected -- fall back to manual gamepad
                    # control (same as Mode.MANUAL) so the user can reposition or
                    # look around to reacquire one, instead of the bot just
                    # sitting stopped and unresponsive until it reappears.
                    if ball_was_found:
                        # Just lost it this frame -- start this loss episode's timer.
                        ball_lost_time = time.monotonic()
                        ball_lost_sound_delay = _random_ball_sound_cooldown()
                        lost_ball_sound_played = False
                    ball_was_found = False

                    if (ball_lost_time is not None and not lost_ball_sound_played
                            and time.monotonic() - ball_lost_time >= ball_lost_sound_delay):
                        lost_ball_sound_played = True
                        last_lost_ball_sound_path = _random_choice_excluding(
                            LOST_BALL_SOUND_PATHS, last_lost_ball_sound_path)
                        audio.queue(last_lost_ball_sound_path, volume=audio.ball_volume)

                    left, right = command_handler.left, command_handler.right
                    follow_manual_msg = (
                        f"[mainloop] FOLLOW_BALL: ball not found -> manual control "
                        f"motor.drive_lr(left={left:.3f}, right={right:.3f})"
                    )
                    if follow_manual_msg != last_follow_manual_msg:  # only print on change
                        debug_print(follow_manual_msg)
                        last_follow_manual_msg = follow_manual_msg
                    motor.drive_lr(left=left, right=right)
            elif mode is Mode.MANUAL:
                left, right = command_handler.left, command_handler.right
                manual_msg = (
                    f"[mainloop] MANUAL: motor.drive_lr(left={left:.3f}, right={right:.3f}) "
                    f"(throttle={command_handler.throttle:.3f}, steering={command_handler.steering:.3f})"
                )
                if manual_msg != last_manual_msg:  # this loop runs ~20x/s -- only print on change
                    debug_print(manual_msg)
                    last_manual_msg = manual_msg
                motor.drive_lr(left=left, right=right)
                time.sleep(MAINLOOP_IDLE_SLEEP_S)
            else:  # Mode.IDLE
                idle_msg = "[mainloop] IDLE: motor.stop()"
                if idle_msg != last_idle_msg:  # only print on change (i.e. once, on entering IDLE)
                    debug_print(idle_msg)
                    last_idle_msg = idle_msg
                motor.stop()
                time.sleep(MAINLOOP_IDLE_SLEEP_S)
    except KeyboardInterrupt:
        pass
    finally:
        motor.stop()
        print("[mainloop] Shutting down")
        # play(), not queue(): this is the one place interrupting is correct --
        # the program is exiting in a fixed 4s window regardless, so if some
        # other clip were still queued ahead of this, queue() could let the
        # shutdown announcement get silently skipped by the timeout below
        # without ever actually playing.
        audio.play(SHUTDOWN_SOUND_PATH)
        audio.wait_for_sound(timeout=4)
        motor.close()
        controller.close()
        tracker.stop()
        audio.close()
        if not window_closed:
            try:
                window.close()
            except tk.TclError:
                pass


NETWORK_CHECK_RETRY_INTERVAL_S = 1.0  # how often to re-check while waiting for a connection at startup
NETWORK_CHECK_TIMEOUT_S = 12.0  # give up waiting and announce whatever's true after this long

# Wifi (and even ethernet, e.g. DHCP) can still take longer than
# NETWORK_CHECK_TIMEOUT_S to finish associating after boot -- so the startup
# check alone can permanently announce "no network" even though the bot is
# online moments later. mainloop() re-checks every this many seconds and
# (re-)announces only if the result actually changed -- see _announce_network().
NETWORK_RECHECK_INTERVAL_S = 30.0


def _check_network_once():
    """Single best-effort nmcli query. Returns (connected_types, connected_desc,
    wifi_names) -- connected_types is a set of "wifi"/"ethernet" strings,
    connected_desc a list of human-readable "type device -> connection" strings
    for logging, wifi_names the connection name(s) of any connected wifi
    interface(s) (for matching against NETWORK_NAME_SOUND_PATHS). Never raises --
    a missing nmcli is just reported as no connection, not fatal."""
    connected_types = set()
    connected = []
    wifi_names = []
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            device, dev_type, state = parts[0], parts[1], parts[2]
            connection = ":".join(parts[3:])  # connection names could contain ':'
            if dev_type in ("wifi", "ethernet") and state == "connected":
                connected.append(f'{dev_type} {device} -> "{connection}"')
                connected_types.add(dev_type)
                if dev_type == "wifi":
                    wifi_names.append(connection)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[network] nmcli check failed: {e}", file=sys.stderr)
    return connected_types, connected, wifi_names


def _announce_network(audio, connected_types, connected, wifi_names):
    """Log the given network check result and, if audio is given, play a sound
    for each connected interface -- ethernet then wifi, queued one after the
    other, if both are connected -- or the no-network sound if neither is. A
    wifi connection whose network name matches NETWORK_NAME_SOUND_PATHS plays
    that clip instead of the generic wifi-connected one (see _wifi_sound_path)."""
    if connected:
        print(f"[network] Connected: {', '.join(connected)}")
    else:
        print("[network] No wifi/ethernet connection detected.")

    if audio is not None:
        # queue(), not play() -- this runs first in practice, but queue() still
        # can't ever cut off something already playing/queued, unlike play().
        # Both get queued (not elif) so both are announced when both are connected.
        if "ethernet" in connected_types:
            audio.queue(ETHERNET_CONNECTED_SOUND_PATH, pause_after=True)
        if "wifi" in connected_types:
            audio.queue(_wifi_sound_path(wifi_names), pause_after=True)
        if not connected_types:
            audio.queue(NO_NETWORK_SOUND_PATH, pause_after=True)


def _wifi_sound_path(wifi_names):
    """Return the NETWORK_NAME_SOUND_PATHS entry for the first connected wifi
    network name that matches (case-insensitively), or WIFI_CONNECTED_SOUND_PATH
    if none of them do."""
    for name in wifi_names:
        sound_path = NETWORK_NAME_SOUND_PATHS.get(name.strip().lower())
        if sound_path:
            return sound_path
    return WIFI_CONNECTED_SOUND_PATH


def _log_network_status(audio=None):
    """Best-effort: log which network interfaces (wifi/ethernet) are connected,
    to what, and the device's IP address(es) -- useful from the startup log for
    diagnosing "why can't I SSH/VNC in" without needing a monitor on the bot.
    See _announce_network() for what gets logged/played.

    This runs right at process start, which on a fresh boot can race NetworkManager
    still associating with wifi (or even ethernet's own DHCP) -- so a single
    immediate check can catch it mid-connect and wrongly report "no network" a
    moment before it actually connects. To reduce that false negative, retry
    every NETWORK_CHECK_RETRY_INTERVAL_S until something's connected or
    NETWORK_CHECK_TIMEOUT_S runs out, and announce whatever the final result is.
    Returns (connected_types, wifi_names) so the caller (mainloop()) can seed its
    own baseline for NETWORK_RECHECK_INTERVAL_S's later re-checks, rather than
    that first re-check always re-announcing this same result again."""
    deadline = time.monotonic() + NETWORK_CHECK_TIMEOUT_S
    connected_types, connected, wifi_names = _check_network_once()
    while not connected_types and time.monotonic() < deadline:
        time.sleep(NETWORK_CHECK_RETRY_INTERVAL_S)
        connected_types, connected, wifi_names = _check_network_once()

    _announce_network(audio, connected_types, connected, wifi_names)

    try:
        ip_result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        ips = ip_result.stdout.split()
        if ips:
            print(f"[network] IP address(es): {', '.join(ips)}")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[network] IP address check failed: {e}", file=sys.stderr)

    return connected_types, wifi_names


# Raised by IMX500(...) when no AI camera is attached. Checked explicitly below so an
# unrelated RuntimeError from YellowBallTracker/IMX500 setup doesn't get misread as
# "no IMX500 camera" and silently swallowed into a fallback.
IMX500_NOT_FOUND_ERROR = "IMX500: Requested camera dev-node not found"


def init(args):
    """Start the camera (IMX500 AI camera preferred, plain camera fallback) and the
    motor controller. Returns (tracker, motor); tracker is either a YellowBallTracker
    or, if no IMX500 is attached, a PlainCameraViewer (no detection, view-only)."""
    need_fallback = False
    try:
        tracker = YellowBallTracker(
            model=args.model,
            color_check=args.color_check,
            color_space=args.color_space,
            debug=args.debug,
            show_preview=args.show_preview,
            record_preview=args.record_preview,
            upside_down=args.upside_down,
        )
        tracker.start()
        # The very first tracker.tick() call after start() reliably blocks for a
        # few seconds (observed ~3s) waiting on the camera/inference pipeline's
        # first completed request -- every call after that is ~40ms. tick() is
        # only ever called from the mainloop's FOLLOW_BALL branch, so without
        # this warm-up call that multi-second stall (during which the whole
        # single-threaded mainloop, including the Tkinter window pump and the
        # camera's own frame delivery, is blocked) would hit the user the first
        # time they switch into ball-following -- looking exactly like the
        # program freezing with no video. Absorb it here instead, during
        # startup, before the window/gamepad loop is ever interactive.
        tracker.tick()
    except RuntimeError as e:
        if str(e) != IMX500_NOT_FOUND_ERROR:
            raise
        print(f"[init] IMX500 camera unavailable ({e}); falling back to plain camera preview.")
        need_fallback = True

    if need_fallback:
        try:
            # No ball detection on a plain camera, but still worth a look, so force
            # the preview on regardless of --show-preview; without it this path
            # shows nothing at all.
            tracker = PlainCameraViewer(show_preview=True, record_preview=args.record_preview)
            tracker.start()
        except Exception as e:
            print(f"[init] Plain camera fallback also failed: {e}", file=sys.stderr)
            raise

    motor = BotMotor()
    return tracker, motor


def main():
    # stdout is a pipe (not a terminal) when launched via start_trackbot.sh, which
    # tees it to a log file -- Python then block-buffers it and log lines don't show
    # up until exit. Flush on every newline instead.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    audio = TrackbotAudio()  # shared across the network-status check and mainloop()
    load_random_sounds(audio=audio)
    load_ball_sounds()
    network_state = _log_network_status(audio)
    args = get_args()
    tracker, motor = init(args)
    mainloop(tracker, motor, start_mode=args.start_mode, debug=args.debug, audio=audio, network_state=network_state)


if __name__ == "__main__":
    sys.exit(main() or 0)
