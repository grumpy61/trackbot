# trackbot
Code for ROV control and experiments

## Running

```
python3 run_trackbot.py --model ssd --color-check --color-space lab --show-preview
```

### Command-line options

| Option | Default | Description |
|---|---|---|
| `--model {nanodet,ssd}` | `ssd` | On-sensor model used for ball following |
| `--color-check` | off | Require a yellow color check on top of the class filter |
| `--color-space {hsv,lab}` | `hsv` | Color space used for the `--color-check` yellow test |
| `--show-preview` | off | Show a live preview window |
| `--debug` | off | Print verbose per-frame debug info |
| `--start-mode {idle,manual}` | `manual` | Base mode to start in. `FOLLOW_BALL` is never a start mode -- it's only reached at runtime via the **R** gamepad button below |
| `--record-preview` / `--no-record-preview` | off | Record camera video to `~/Videos/Trackbot`. Recording doesn't start immediately -- it kicks in 1s after the mainloop begins running (avoids a hang seen when starting it during camera init), through the same toggle as the **L** button, so a press of **L** afterward correctly turns it back off |
| `--upside-down` / `--no-upside-down` | on | Camera is mounted upside down, so flip h/v. Pass `--no-upside-down` if it's mounted right-side up |

## Gamepad controls

| Button | Action |
|---|---|
| **D-pad ↑** | Throttle forward -- a ratchet, +1 rung per press. Stays at 0 for the first 2 presses (below the motor's deadband), jumps to the floor on the 3rd, then ramps to full by the 20th. Releasing holds the current rung rather than decaying back down. |
| **D-pad ↓** | Throttle reverse -- same ratchet, mirrored. |
| **D-pad ←** | Steering left -- ratchet, +1 rung per press, evenly spaced, full lock at the 20th press. Releasing holds the current rung. |
| **D-pad →** | Steering right -- same ratchet, mirrored. |
| **A** | **Stop** -- instantly zeroes throttle and steering (bypassing the ramp/smoothing), resets both ratchets, and forces `FOLLOW_BALL` off. Holding it down continuously for 3s instead requests a clean program shutdown. |
| **X** | Zero steering only, instantly (throttle untouched). |
| **L** | Toggle video recording. A single press flips on/off; a press within 0.6s of the previous one forces it *off* regardless of the resulting parity, so a burst of quick clicks always ends up off. |
| **R** | Toggle `Mode.FOLLOW_BALL`. Same quick-click-forces-off behavior as recording. No-ops (with a console message) if no AI camera is available (plain-camera fallback). |

The left and right tracks are never mixed by simply adding/subtracting steering from throttle -- that can drive one track to zero or into reverse (an unintended spin) if steering is large relative to throttle. Instead, steering only ever *lowers* the inside track toward zero (the outside track always stays at the current throttle, never raised above it) by up to 10 throttle steps (half the full range), so full steering deflection can bring the inside track to a complete stop for a tight pivot turn, but it can never be driven into reverse.
