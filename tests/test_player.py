import io

import pytest

from dj_librarian import player


class FakeProcess:
    """ffplay: the status lines that it prints, before the sound starts and then with its audio clock."""

    output = b""

    def __init__(self, *args, **kwargs):
        self.running = True
        self.stderr = io.BytesIO(FakeProcess.output)

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.running = False

    def wait(self, timeout=None):
        pass


class NowThread:
    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def ffplay(monkeypatch, tmp_path):
    monkeypatch.setattr(player.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(player.threading, "Thread", NowThread)
    audio = tmp_path / "track.wav"
    audio.write_bytes(b"")
    p = player.Player()
    p.ffplay = "ffplay"
    return p, str(audio)


def test_position_follows_the_clock_of_ffplay(monkeypatch, ffplay):
    p, audio = ffplay
    clock = iter([100.0, 101.0, 102.0])
    monkeypatch.setattr(player.time, "monotonic", lambda: next(clock))
    FakeProcess.output = b"nan    :  0.000 fd=   0\rnan M-A:    nan fd=   0\r   61.50 M-A: -0.000 fd=   0\r"

    assert p.play(audio, 60.0)  # ffplay shows 61.5 at 100

    assert p.position() == 62.5  # at 101
    p.pause()  # at 102
    assert p.paused and p.position() == 63.5


def test_position_waits_for_the_sound(ffplay):
    p, audio = ffplay
    FakeProcess.output = b"nan M-A:    nan fd=   0\r"

    p.play(audio, 63.5)

    assert p.position() == 63.5


def test_play_a_missing_file(tmp_path):
    p = player.Player()
    p.ffplay = "ffplay"

    assert not p.play(str(tmp_path / "missing.mp3"), 0.0)
    assert p.path == ""
