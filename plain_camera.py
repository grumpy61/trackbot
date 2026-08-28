#!/usr/bin/env python3
"""
plain_camera.py

Live-preview-only fallback for when the IMX500 AI camera isn't available -- e.g. a
plain Pi Camera v2 with no on-sensor object detection. Shows the camera feed but
never returns detections, so ball-following can't run against it; it exists purely
so run_trackbot.py still has something to look at.

Matches enough of YellowBallTracker's interface (start/tick/stop) that
run_trackbot.py's mainloop() doesn't need to know which camera is actually active.
"""

from libcamera import Transform

from picamera2 import Picamera2

from video_recorder import VideoRecorder


class PlainCameraViewer:
    """Opens a plain camera purely to show a live preview; tick() never finds a ball."""

    def __init__(self, show_preview=True, record_preview=False):
        self.show_preview = show_preview
        self.picam2 = None
        self.video_recorder = VideoRecorder(enabled=record_preview)

    def start(self):
        self.picam2 = Picamera2()
        config = self.picam2.create_preview_configuration(
            transform=Transform(hflip=True, vflip=True)  # camera is mounted upside down
        )
        self.picam2.start(config, show_preview=self.show_preview)
        self.video_recorder.start(self.picam2)

    def tick(self):
        """No AI detection available on a plain camera. Still pull a frame so we
        pace with the camera's frame rate like YellowBallTracker.tick() does."""
        if self.picam2 is not None:
            self.video_recorder.tick()
            self.picam2.capture_metadata()
        return None

    def stop(self):
        if self.picam2 is not None:
            self.video_recorder.stop()
            self.picam2.stop()
