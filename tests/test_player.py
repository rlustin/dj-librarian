from dj_librarian import player


class FakeProcess:
    def __init__(self, *args, **kwargs):
        self.running = True

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.running = False

    def wait(self, timeout=None):
        pass


def test_pause_keeps_the_position(monkeypatch, tmp_path):
    clock = iter([100.0, 110.0, 200.0, 205.0])
    monkeypatch.setattr(player.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(player.subprocess, "Popen", FakeProcess)
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"")
    p = player.Player()
    p.ffplay = "ffplay"

    assert p.play(str(audio), 60.0)  # started at 100
    p.pause()  # at 110: position 70
    assert p.paused and p.position() == 70.0
    p.pause()  # plays again from 70, at 200
    assert not p.paused and p.position() == 75.0  # at 205


def test_play_a_missing_file(tmp_path):
    p = player.Player()
    p.ffplay = "ffplay"

    assert not p.play(str(tmp_path / "missing.mp3"), 0.0)
    assert p.path == ""
