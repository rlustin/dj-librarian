"""Play a track in the background (ffplay, no window) during the questions in the terminal."""

import shutil
import subprocess
import time

from dj_librarian.library import on_disk

SEEK_SECONDS = 30


class Player:
    def __init__(self, enabled: bool = True) -> None:
        self.ffplay = shutil.which("ffplay") if enabled else None
        self.process: subprocess.Popen | None = None
        self.path = ""
        self.offset = self.started_at = 0.0
        self.paused = False

    @property
    def available(self) -> bool:
        return self.ffplay is not None

    def play(self, path: str, start: float) -> bool:
        """False if dj-librarian cannot play the file (for example, its drive is not mounted)."""
        self.stop()
        path = on_disk(path) if self.ffplay else None
        self.path, self.paused = path or "", False
        if not path:
            return False
        self.offset, self.started_at = max(start, 0.0), time.monotonic()
        self.process = subprocess.Popen(
            [self.ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", "-ss", f"{self.offset:.2f}", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True

    @property
    def ended(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def position(self) -> float:
        return self.offset if self.paused else self.offset + time.monotonic() - self.started_at

    def seek(self, delta: float) -> None:
        if self.path:
            self.play(self.path, self.position() + delta)

    def pause(self) -> None:
        """Pause, or play again from the same position."""
        if not self.path:
            return
        if self.paused:
            self.play(self.path, self.offset)
        else:
            self.offset = self.position()
            self.stop()
            self.paused = True

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
