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
PLAYBACK_VOLUME = 0.9  # 0-1.0, passed to pw-play's --volume for every clip

_STOP = object()  # sentinel put on the queue to shut the worker thread down


class TrackbotAudio:
    """Background sound-clip player with a play-now/queue-behind API.

    play(path) stops whatever's currently playing, drops anything queued, and
    plays path immediately. queue(path) instead appends path to play after
    everything ahead of it finishes, without disturbing current playback.
    """

    def __init__(self, player_cmd=DEFAULT_PLAYER_CMD):
        self._player_cmd = player_cmd
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._current_proc = None
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def play(self, path):
        """Stop current playback, clear the queue, and play path now."""
        self.stop()
        self.queue(path)

    def queue(self, path):
        """Append path to play once everything ahead of it finishes."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"No such sound file: {path}")
        self._queue.put(str(path))

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
            path = self._queue.get()
            if path is _STOP:
                return

            with self._lock:
                try:
                    self._current_proc = subprocess.Popen(
                        [self._player_cmd, "--volume", str(PLAYBACK_VOLUME), path],
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
    audio.play(sys.argv[1])
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
