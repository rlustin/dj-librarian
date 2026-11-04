"""Live status block that updates in place, for long runs with parallel jobs."""

import shutil
import sys
import threading
import time
from collections import deque


class LiveStatus:
    """Thread-safe. Workers call start, activity and finish. The main thread calls render.

    In a terminal, dj-librarian draws the block again in place. If the output goes to a file,
    dj-librarian prints one plain line for each event.
    """

    def __init__(self, title: str, total_jobs: int, total_items: int, item_name: str = "tracks") -> None:
        self.title, self.total_jobs, self.total_items, self.item_name = title, total_jobs, total_items, item_name
        self.lock = threading.Lock()
        self.running: dict[int, dict] = {}
        self.done_jobs = self.done_items = self.failed = 0
        self.recent: deque[str] = deque(maxlen=5)
        self.started = time.monotonic()
        self.tty = sys.stdout.isatty()
        self.drawn = 0

    def start(self, job: int, label: str, items: int) -> None:
        with self.lock:
            self.running[job] = {"label": label, "items": items, "started": time.monotonic(), "activity": "starts"}
        self._log(f"started: {label}")

    def activity(self, job: int, text: str) -> None:
        with self.lock:
            if job in self.running:
                self.running[job]["activity"] = text
                label = self.running[job]["label"]
            else:
                return
        self._log(f"{label}: {text}")

    def finish(self, job: int, results: list[str], error: str | None = None) -> None:
        with self.lock:
            info = self.running.pop(job, {"label": "?", "items": 0})
            self.done_jobs += 1
            self.done_items += info["items"]
            if error:
                self.failed += 1
                self.recent.appendleft(f"failed: {info['label']}: {error}")
            for line in results:
                self.recent.appendleft(line)
        self._log(f"failed: {info['label']}: {error}" if error else f"done: {info['label']}")

    def render(self) -> None:
        if not self.tty:
            return
        width = shutil.get_terminal_size((120, 20)).columns
        with self.lock:
            lines = self._lines()
        out = [f"\x1b[{self.drawn}F"] if self.drawn else []
        out += ["\x1b[2K" + line[: width - 1] + "\n" for line in lines]
        # Clear the old lines if the block is now shorter
        out += ["\x1b[2K\n"] * max(self.drawn - len(lines), 0)
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.drawn = max(len(lines), self.drawn)

    def _lines(self) -> list[str]:
        elapsed = time.monotonic() - self.started
        share = self.done_items / self.total_items if self.total_items else 1
        bar = "█" * int(24 * share) + "░" * (24 - int(24 * share))
        eta = f" · ~{_duration(elapsed / share * (1 - share))} left" if 0 < share < 1 else ""
        failed = f" · {self.failed} failed" if self.failed else ""
        lines = [
            self.title,
            (f"  {bar} {self.done_items}/{self.total_items} {self.item_name} ({share:.0%}) · "
             f"{self.done_jobs}/{self.total_jobs} calls{failed} · {_duration(elapsed)} elapsed{eta}"),
        ]
        if self.running:
            lines.append("  now:")
            now = time.monotonic()
            for info in sorted(self.running.values(), key=lambda i: i["started"]):
                lines.append(f"    {info['label']} · {_duration(now - info['started'])} · {info['activity']}")
        if self.recent:
            lines.append("  latest:")
            lines += [f"    {line}" for line in self.recent]
        return lines

    def _log(self, line: str) -> None:
        if not self.tty:
            print(f"[{_duration(time.monotonic() - self.started)}] {line}", flush=True)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}"


class Steps:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.tty = sys.stdout.isatty()
        self.name: str | None = None
        self.started = 0.0
        self.last_count = 0.0

    def step(self, name: str) -> None:
        self.done()
        self.name, self.started = name, time.monotonic()
        if not self.quiet:
            print(f"{name}...", end="" if self.tty else "\n", flush=True)

    def count(self, done: int, total: int) -> None:
        now = time.monotonic()
        if self.quiet:
            return
        if self.tty and (now - self.last_count > 0.2 or done == total):
            print(f"\r\x1b[2K{self.name}... {done}/{total}", end="", flush=True)
            self.last_count = now
        elif not self.tty and (done % 500 == 0 or done == total):
            print(f"  {done}/{total}", flush=True)

    def done(self) -> None:
        if self.name and self.tty and not self.quiet:
            print(f"\r\x1b[2K{self.name}... done ({time.monotonic() - self.started:.1f}s)", flush=True)
        self.name = None
