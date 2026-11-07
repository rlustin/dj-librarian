"""Play a track in the background (ffplay, no window) during the questions in the terminal."""

import re
import shutil
import subprocess
import threading
import time

from dj_librarian.library import on_disk

SEEK_SECONDS = 30
CLOCK = re.compile(rb"\s*(\d+\.\d+) M-A")  # the status line of ffplay starts with its audio clock


class Player:
    def __init__(self, enabled: bool = True) -> None:
        self.ffplay = shutil.which("ffplay") if enabled else None
        self.process: subprocess.Popen | None = None
        self.path = ""
        self.offset = 0.0
        self.paused = False
        self.clock: tuple[float, float] | None = None  # (time in the file, monotonic time) from ffplay

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
        self.offset, self.clock = max(start, 0.0), None
        self.process = subprocess.Popen(
            [self.ffplay, "-nodisp", "-autoexit", "-stats", "-loglevel", "quiet", "-ss", f"{self.offset:.2f}", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        threading.Thread(target=self._read_clock, args=(self.process,), daemon=True).start()
        return True

    def _read_clock(self, process: subprocess.Popen) -> None:
        """ffplay starts to play about 0.8 s after its start. Its clock includes the delay of the audio buffer."""
        rest = b""
        while chunk := process.stderr.read1(256):
            *lines, rest = re.split(rb"[\r\n]", rest + chunk)
            for line in lines:
                if (match := CLOCK.match(line)) and process is self.process:
                    self.clock = float(match[1]), time.monotonic()

    @property
    def ended(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def position(self) -> float:
        """The time in the file that you hear. Before the sound starts, the start position."""
        if self.paused or not self.clock:
            return self.offset
        played, at = self.clock
        return played if self.ended else played + time.monotonic() - at

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
