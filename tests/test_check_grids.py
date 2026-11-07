import pytest

from dj_librarian import beats, library
from dj_librarian.commands.check_grids import _other_level

from helpers import track


def test_other_level_uses_the_analysis():
    result = {"families": [[8.6, 95.0, 0.2, 1], [6.0, 190.0, 0.2, 3]]}

    double = _other_level(result, beats.Proposal(fit=8.6, bpm=95.0, phase=0.2, downbeat=1), 2)

    assert (double.bpm, double.downbeat) == (190.0, 3)


def test_other_level_without_analysis_takes_every_other_beat():
    result = {"families": [[8.6, 95.0, 0.2, 1]]}

    half = _other_level(result, beats.Proposal(fit=8.6, bpm=95.0, phase=0.2, downbeat=1), 0.5)

    assert half.bpm == pytest.approx(47.5) and half.phase == 0.2 and half.fit == 0.0


def test_a_written_grid_is_not_reported_again(tmp_path):
    from dj_librarian import store
    from dj_librarian.commands.check_grids import _answer, _key, mark_written

    dat, audio = tmp_path / "ANLZ0000.DAT", tmp_path / "track.mp3"
    dat.write_bytes(b"grid")
    audio.write_bytes(b"audio")
    store.write_json(store.GRID_DECISIONS, {"1": {"key": "old", "bpm": 95.0, "phase": 0.1, "downbeat": 0}})

    file = library.find_files([track("1", path=str(audio))])["1"]
    mark_written({"1": (dat, file)})

    decision = store.read_json(store.GRID_DECISIONS, {})["1"]
    assert decision["key"] == _key(dat, file) and decision["keep"]
    assert _answer(decision) == "written by dj-librarian"
