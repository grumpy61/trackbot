#!/usr/bin/env python3
"""
trackbot_audio.py

Plays sound clips (e.g. alerts, status chirps) through the system's default
audio sink via pw-play, from a background thread so callers never block on
playback. TrackbotAudio owns a queue: play() interrupts whatever's playing and
clears anything queued, queue() instead waits its turn behind them.

If a clip plays here (is_playing() goes True, pw-play exits cleanly) but
nothing is actually audible, that's very likely the system's PipeWire output
profile pointed at a non-functional digital passthrough rather than the real
analog output -- see `wpctl status` -- not a bug in this module.
"""

import queue
import subprocess
import threading
import time
from pathlib import Path

DEFAULT_PLAYER_CMD = "pw-play"
PLAYBACK_VOLUME = 0.9  # 0-1.0, the initial value of self.volume (see set_volume())
PAUSE_SOUND_PATH = Path(__file__).parent / "sounds" / "silentonesec.wav"  # 1s of silence

_STOP = object()  # sentinel put on the queue to shut the worker thread down


class TrackbotAudio:
    """Background sound-clip player with a play-now/queue-behind API.

    play(path) stops whatever's currently playing, drops anything queued, and
    plays path immediately. queue(path) instead appends path to play after
    everything ahead of it finishes, without disturbing current playback.

    Pass the *same* playback_lock instance to both a TrackbotAudio and a
    TrackbotSpeech to guarantee their playback never overlaps -- whichever
    grabs it first plays to completion while the other waits its turn.
    """

    def __init__(self, player_cmd=DEFAULT_PLAYER_CMD, playback_lock=None):
        self._player_cmd = player_cmd
        self._playback_lock = playback_lock or threading.Lock()
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._current_proc = None
        self.volume = PLAYBACK_VOLUME  # 0.0-1.0, applied to each clip as it starts -- see set_volume()
        # Separate volume for ball-found/ball-lost clips (BALL_SOUND_PATHS/
        # LOST_BALL_SOUND_PATHS in run_trackbot.py) -- pass volume=self.ball_volume
        # to queue()/play() for those specifically; everything else keeps using
        # self.volume. See set_ball_volume().
        self.ball_volume = PLAYBACK_VOLUME
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def set_volume(self, value):
        """Set the volume (0.0-1.0, clamped, then rounded to the nearest 0.1 --
        so repeated +/- steps always land exactly on 0.0, 0.1, 0.2, ..., 1.0,
        never drifting off by floating-point noise) applied to clips from now
        on (that don't get their own volume= override -- see queue()/play()).
        Already-playing/queued clips started before this call keep whatever
        volume they were launched with."""
        self.volume = round(max(0.0, min(1.0, value)), 1)
        return self.volume

    def set_ball_volume(self, value):
        """Same as set_volume(), but for self.ball_volume (ball-found/ball-lost
        clips) instead of the general self.volume."""
        self.ball_volume = round(max(0.0, min(1.0, value)), 1)
        return self.ball_volume

    def play(self, path, pause_after=False, volume=None):
        """Stop current playback, clear the queue, and play path now. If
        pause_after, queue a 1s silence clip right behind it."""
        self.stop()
        self.queue(path, pause_after=pause_after, volume=volume)

    def queue(self, path, pause_after=False, volume=None):
        """Append path to play once everything ahead of it finishes. volume
        (0.0-1.0), if given, overrides self.volume for this clip only -- e.g.
        pass self.ball_volume for a ball-found/ball-lost clip. If pause_after,
        also queue a 1s silence clip right behind it, so whatever plays next
        (from this call or a later one) doesn't run straight on."""
        self._queue_one(path, volume)
        if pause_after:
            self._queue_one(PAUSE_SOUND_PATH, volume)

    def _queue_one(self, path, volume=None):
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"No such sound file: {path}")
        self._queue.put((str(path), volume))

    def stop(self):
        """Stop current playback (if any) and clear the queue."""
        with self._lock:
            self._drain_queue_locked()
            proc = self._current_proc
        if proc is not None:
            proc.terminate()

    def is_playing(self):
        """True if a clip is actively playing right now (not counting queued ones)."""
        with self._lock:
            return self._current_proc is not None and self._current_proc.poll() is None

    def queued_count(self):
        """Number of clips waiting behind whatever's currently playing."""
        return self._queue.qsize()

    def is_busy(self):
        """True if something is playing or waiting in the queue."""
        return self.is_playing() or self.queued_count() > 0

    def wait_for_sound(self, timeout=0):
        """Block for at most `timeout` seconds while something is playing or
        queued, polling via sleep(). Default timeout=0 means don't wait at all
        -- just check and return immediately. Returns True if nothing was (or
        is now) busy, False if still busy when the timeout ran out."""
        deadline = time.monotonic() + timeout
        while self.is_busy():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        return True

    def close(self):
        """Stop playback and shut down the background worker thread."""
        self.stop()
        self._queue.put(_STOP)
        self._worker.join(timeout=2)

    def _drain_queue_locked(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _run(self):
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            path, volume = item
            if volume is None:
                volume = self.volume

            with self._playback_lock:
                with self._lock:
                    try:
                        self._current_proc = subprocess.Popen(
                            [self._player_cmd, "--volume", str(volume), path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                    except OSError as e:
                        print(f"[TrackbotAudio] Failed to start {self._player_cmd} for {path}: {e}")
                        self._current_proc = None
                        continue

                self._current_proc.wait()

                with self._lock:
                    self._current_proc = None


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} sound1.wav [sound2.wav ...]")
        sys.exit(1)

    audio = TrackbotAudio()
    audio.queue(sys.argv[1])
    for extra in sys.argv[2:]:
        audio.queue(extra)

    try:
        while audio.is_busy():
            print(f"playing={audio.is_playing()} queued={audio.queued_count()}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        audio.stop()
    finally:
        audio.close()
