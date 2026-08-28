#!/usr/bin/env python3
"""
video_recorder.py

Records a Picamera2 instance's "main" stream to timestamped .mp4 segments under
/home/trackbot/Videos/Trackbot. Shared by YellowBallTracker and PlainCameraViewer
so both camera types record the same way.

Segments roll over to a new file every SEGMENT_SECONDS (call tick() once per frame
to drive this). At the start of each segment -- including the first -- recording is
skipped (with a logged error) if free disk space is below MIN_FREE_BYTES.
"""

import datetime
import shutil
import sys
import time
from pathlib import Path

from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

VIDEO_DIR = Path("/home/trackbot/Videos/Trackbot")
SEGMENT_SECONDS = 5 * 60  # restart into a new file after this long
MIN_FREE_BYTES = 50 * 1024 ** 3  # don't record if less than this much free space


class VideoRecorder:
    """enabled=False makes start()/stop()/tick() no-ops, so callers don't need to branch."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self._picam2 = None
        self._recording = False
        self._segment_start = None

    def start(self, picam2):
        if not self.enabled:
            return
        self._picam2 = picam2
        self._start_segment()

    def tick(self):
        """Call once per frame/loop iteration while running; rolls over into a new
        segment file every SEGMENT_SECONDS."""
        if not self.enabled or self._picam2 is None:
            return
        if time.monotonic() - self._segment_start >= SEGMENT_SECONDS:
            self._stop_segment()
            self._start_segment()

    def stop(self):
        if self._picam2 is not None:
            self._stop_segment()
            self._picam2 = None
            self._segment_start = None

    def _start_segment(self):
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(VIDEO_DIR).free
        if free_bytes < MIN_FREE_BYTES:
            print(
                f"[VideoRecorder] ERROR: only {free_bytes / 1024**3:.1f} GB free "
                f"(< {MIN_FREE_BYTES / 1024**3:.0f} GB) -- skipping video recording.",
                file=sys.stderr,
            )
            self._recording = False
            self._segment_start = time.monotonic()  # retry at the next segment boundary
            return

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = VIDEO_DIR / f"trackbot_{timestamp}.mp4"
        self._picam2.start_recording(H264Encoder(), FfmpegOutput(str(path)))
        self._recording = True
        self._segment_start = time.monotonic()
        print(f"[VideoRecorder] Recording to {path}")

    def _stop_segment(self):
        if self._recording:
            self._picam2.stop_recording()
            self._recording = False
