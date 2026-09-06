#!/usr/bin/env python3
"""
trackbot_window.py

A small always-visible desktop window ("Trackbot") for status text and control
buttons, built on Tkinter (already part of the standard library -- no extra
dependencies). TrackbotWindow doesn't run its own blocking mainloop; call
tick() once per iteration of the caller's own loop (e.g. run_trackbot.py's
mainloop()) to pump pending GUI events without blocking anything else.

Currently shows a mode-status label pinned to the upper-right corner, a Quit
button (with a confirmation dialog) in the lower-right, and a virtual gamepad
(D-pad + X/A/B/Y + triggers) filling most of the window. The virtual gamepad's
buttons update self.virtual_buttons (keyed by the same evdev keycodes
bot_commands.py uses) on press/release; run_trackbot.py's mainloop merges that
into the real gamepad's live button state each iteration and feeds it through
BotCommandHandler.process_command() exactly as a real press would be, so a
virtual press behaves identically to the matching physical button. B and Y
have no bot_commands.py mapping yet, so they're visual/pressable only.
"""

import tkinter as tk
from tkinter import messagebox

from bot_commands import (
    BTN_A_STOP,
    BTN_DOWN,
    BTN_LEFT,
    BTN_L_RECORD_TOGGLE,
    BTN_RIGHT,
    BTN_R_FOLLOW_TOGGLE,
    BTN_UP,
    BTN_X_STRAIGHT,
)

WINDOW_TITLE = "Trackbot"
WINDOW_SIZE = "800x300"

ROUND_BUTTON_DIAMETER = 56
ROUND_BUTTON_FILL = "#dddddd"
ROUND_BUTTON_PRESSED_FILL = "#aaaaaa"

GAMEPAD_PLATE_COLOR = "#4d4d4d"  # darker-grey "body" drawn behind the buttons
GAMEPAD_PLATE_MARGIN = 20        # gap between the buttons and the plate's edge
GAMEPAD_PLATE_RADIUS = 24        # corner radius of the plate


class TrackbotWindow:
    """Owns the Trackbot status window. Not thread-safe -- tick() (and any
    other method) must be called from the same thread that constructed this."""

    def __init__(self, title=WINDOW_TITLE):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(WINDOW_SIZE)
        self.quit_requested = False
        # Closing the window (the OS/native X button) asks the same "Do you want
        # to quit?" confirmation as the Quit button, via the same handler --
        # setting this protocol stops Tk from destroying the window on its own,
        # so a "No" just leaves everything running exactly as it was.
        self.root.protocol("WM_DELETE_WINDOW", self._on_quit_clicked)

        self.mode_label = tk.Label(
            self.root, text="Mode: --", font=("TkDefaultFont", 12, "bold"),
        )
        self.mode_label.place(relx=1.0, y=8, x=-8, anchor="ne")

        self.video_label = tk.Label(
            self.root, text="Video: --", font=("TkDefaultFont", 12, "bold"),
        )
        self.video_label.place(relx=1.0, y=36, x=-8, anchor="ne")

        self.quit_button = tk.Button(self.root, text="Quit", command=self._on_quit_clicked)
        self.quit_button.place(relx=1.0, rely=1.0, x=-8, y=-8, anchor="se")

        self.trigger_buttons = {}  # "left"/"right" -> tk.Button
        self.dpad_buttons = {}   # "up"/"down"/"left"/"right" -> tk.Button
        self.abxy_buttons = {}   # "X"/"A"/"B"/"Y" -> tk.Canvas (round buttons)
        self.virtual_buttons = {}  # evdev keycode -> bool, mirrors bot_gamepad.GamepadState.buttons
        self._build_gamepad()

    def _bind_virtual_button(self, widget, keycode):
        """Make widget set/clear keycode in self.virtual_buttons on press/release
        (add="+" so this doesn't replace any existing binding, e.g. a round
        button's own press/release visual feedback)."""
        self.virtual_buttons.setdefault(keycode, False)
        widget.bind("<ButtonPress-1>", lambda event: self.virtual_buttons.__setitem__(keycode, True), add="+")
        widget.bind("<ButtonRelease-1>", lambda event: self.virtual_buttons.__setitem__(keycode, False), add="+")

    def _build_gamepad(self):
        """Left/Right Trigger buttons on top, D-pad (plus-sign layout) below-left,
        X/A/B/Y (diamond layout, round buttons, clockwise from X at the top)
        below-right -- anchored in the lower-left of the window (with a margin)
        so the rest of the window stays free for other use. Each button (other
        than B/Y, which have no bot_commands.py mapping) drives the same evdev
        keycode a real gamepad press would, via self.virtual_buttons.

        Each trigger button lives in the *same* sub-frame as the group it sits
        above (left_col/right_col), so grid's default centering centers it over
        just that group's own width -- not over that group plus the padx gap
        used to separate the two columns, which would pull it off-center."""
        gamepad = tk.Frame(self.root, bg=GAMEPAD_PLATE_COLOR)
        gamepad.place(relx=0.0, rely=1.0, x=20, y=-20, anchor="sw")

        left_col = tk.Frame(gamepad, bg=GAMEPAD_PLATE_COLOR)
        left_col.grid(row=0, column=0, padx=(0, 60))
        right_col = tk.Frame(gamepad, bg=GAMEPAD_PLATE_COLOR)
        right_col.grid(row=0, column=1)

        left_trigger = tk.Button(left_col, text="Left Trigger", width=13)
        left_trigger.grid(row=0, column=0, pady=(0, 10))
        self._bind_virtual_button(left_trigger, BTN_L_RECORD_TOGGLE)
        self.trigger_buttons["left"] = left_trigger

        right_trigger = tk.Button(right_col, text="Right Trigger", width=13)
        right_trigger.grid(row=0, column=0, pady=(0, 10))
        self._bind_virtual_button(right_trigger, BTN_R_FOLLOW_TOGGLE)
        self.trigger_buttons["right"] = right_trigger

        dpad = tk.Frame(left_col, bg=GAMEPAD_PLATE_COLOR)
        dpad.grid(row=1, column=0)
        dpad_spec = {
            "up": (0, 1, "▲", BTN_UP),
            "left": (1, 0, "◀", BTN_LEFT),
            "right": (1, 2, "▶", BTN_RIGHT),
            "down": (2, 1, "▼", BTN_DOWN),
        }
        for name, (row, col, arrow, keycode) in dpad_spec.items():
            button = tk.Button(dpad, text=arrow, width=3, height=1, font=("TkDefaultFont", 14))
            button.grid(row=row, column=col, padx=2, pady=2)
            self._bind_virtual_button(button, keycode)
            self.dpad_buttons[name] = button

        # Squish the X/A/B/Y diamond to the D-pad's own height (measured, so this
        # stays correct regardless of theme/font) rather than its natural ~3
        # diameters tall: X and B only need to clear Y/A by ROUND_BUTTON_DIAMETER
        # in *diagonal* (not vertical) distance, since Y/A sit off to the side --
        # so the vertical gap between rows can shrink well below one diameter
        # before anything actually overlaps. This also makes left_col and
        # right_col the same total height, which is what keeps the two trigger
        # buttons (and the D-pad/diamond centers) aligned with each other.
        self.root.update_idletasks()
        abxy_height = dpad.winfo_reqheight()
        half_spread = ROUND_BUTTON_DIAMETER / 2 + 30  # horizontal center-to-Y/A distance
        abxy_width = int(2 * half_spread + ROUND_BUTTON_DIAMETER)

        abxy = tk.Frame(right_col, width=abxy_width, height=abxy_height, bg=GAMEPAD_PLATE_COLOR)
        abxy.grid(row=1, column=0)
        abxy.grid_propagate(False)  # keep the explicit squished size -- children are place()'d, not gridded

        cx, cy = abxy_width / 2, abxy_height / 2
        radius = ROUND_BUTTON_DIAMETER / 2
        # Diamond layout, clockwise from X at the top: X, A, B, Y.
        abxy_positions = {
            "X": (cx, radius),
            "A": (cx + half_spread, cy),
            "B": (cx, abxy_height - radius),
            "Y": (cx - half_spread, cy),
        }
        # B and Y have no bot_commands.py mapping yet, so they stay visual-only.
        abxy_keycodes = {"X": BTN_X_STRAIGHT, "A": BTN_A_STOP}
        for name, (x, y) in abxy_positions.items():
            button = self._make_round_button(abxy, name)
            button.place(x=x, y=y, anchor="center")
            if name in abxy_keycodes:
                self._bind_virtual_button(button, abxy_keycodes[name])
            self.abxy_buttons[name] = button

        self._build_gamepad_plate(gamepad)

    def _build_gamepad_plate(self, gamepad):
        """Draw a rounded-rectangle "body" behind the gamepad controls, like a
        real gamepad's case. Sized from gamepad's own measured footprint plus a
        margin, and placed at the same anchor -- then lowered so it sits behind
        the (already-placed) controls in the stacking order."""
        self.root.update_idletasks()
        margin = GAMEPAD_PLATE_MARGIN
        plate_w = gamepad.winfo_reqwidth() + 2 * margin
        plate_h = gamepad.winfo_reqheight() + 2 * margin

        plate = tk.Canvas(self.root, width=plate_w, height=plate_h,
                           highlightthickness=0, bg=self.root["bg"])
        plate.place(relx=0.0, rely=1.0, x=20 - margin, y=-20 + margin, anchor="sw")
        self._rounded_rect(plate, 1, 1, plate_w - 1, plate_h - 1,
                            GAMEPAD_PLATE_RADIUS, fill=GAMEPAD_PLATE_COLOR, outline="")
        # Canvas overrides lower() for canvas-*item* stacking (tag_lower), so call
        # the base widget-stacking implementation directly to push this whole
        # window behind gamepad (already placed, and thus already stacked above).
        tk.Misc.lower(plate)

    @staticmethod
    def _rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
        """Draw a filled rounded rectangle: a smoothed polygon through each
        corner (Tkinter has no native rounded-rectangle shape)."""
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kwargs)

    def _make_round_button(self, parent, label):
        """A round, pressable (visual feedback only) button drawn on a Canvas --
        Tkinter's own Button widget can't render a circle."""
        size = ROUND_BUTTON_DIAMETER
        canvas = tk.Canvas(parent, width=size, height=size, highlightthickness=0, bg=GAMEPAD_PLATE_COLOR)
        circle = canvas.create_oval(2, 2, size - 2, size - 2, fill=ROUND_BUTTON_FILL, outline="black")
        canvas.create_text(size / 2, size / 2, text=label, font=("TkDefaultFont", 13, "bold"))
        canvas.bind("<ButtonPress-1>", lambda event: canvas.itemconfig(circle, fill=ROUND_BUTTON_PRESSED_FILL))
        canvas.bind("<ButtonRelease-1>", lambda event: canvas.itemconfig(circle, fill=ROUND_BUTTON_FILL))
        return canvas

    def set_mode(self, mode_name):
        """Update the mode-status text in the upper-right corner."""
        self.mode_label.config(text=f"Mode: {mode_name}")

    def set_video_status(self, status):
        """Update the video-status text below the mode status. status is
        typically one of "On", "Off", "Recording"."""
        self.video_label.config(text=f"Video: {status}")

    def _on_quit_clicked(self):
        if messagebox.askyesno(
            "Quit Trackbot", "Do you want to quit?", default=messagebox.NO, parent=self.root,
        ):
            self.quit_requested = True

    def tick(self):
        """Process pending GUI events without blocking. Call once per iteration
        of the caller's own loop -- this window doesn't run its own mainloop."""
        self.root.update_idletasks()
        self.root.update()

    def close(self):
        self.root.destroy()


if __name__ == "__main__":
    import itertools
    import time

    window = TrackbotWindow()
    modes = itertools.cycle(["IDLE", "MANUAL", "FOLLOW_BALL"])
    video_statuses = itertools.cycle(["On", "Off", "Recording"])
    last_switch = time.monotonic()

    try:
        while True:
            if time.monotonic() - last_switch >= 2.0:
                last_switch = time.monotonic()
                window.set_mode(next(modes))
                window.set_video_status(next(video_statuses))
            window.tick()
            if window.quit_requested:
                break
            time.sleep(0.05)
    except (KeyboardInterrupt, tk.TclError):
        pass
    finally:
        window.close()
