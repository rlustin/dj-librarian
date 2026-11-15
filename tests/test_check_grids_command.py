import csv
from argparse import Namespace

import pytest
from pyrekordbox import AnlzFile

from dj_librarian import library, store, writer
from dj_librarian.commands import check_grids

import anlz
from helpers import add_analysed, add_grid, sound

# Check results (beats.check) for a right grid, and for a grid at half the tempo of the audio
RIGHT = {"version": 1, "duration": 60.0, "grid_bpm": 170.0, "grid_first": 0.2, "grid_fit": 6.0, "grid_offset": -0.058,
         "same": [6.0, 170.0, 0.142, 0], "families": [[3.0, 85.0, 0.142, 0], [6.0, 170.0, 0.142, 0]]}
HALF = RIGHT | {"grid_bpm": 85.0, "grid_fit": 2.0, "same": [2.0, 85.0, 0.142, 0], "families": [[6.0, 170.0, 0.142, 0]]}


@pytest.fixture
def grids(db, analysed, tmp_path):
    """A right grid, a grid at half tempo, and a track without analysis files. The checks are in the cache already.

    Return the key of the half tempo grid and its DAT file.
    """
    keys = {}
    tracks = [("right", anlz.grid(170), RIGHT), ("half", anlz.grid(85), HALF), ("none", None, None)]
    for n, (id, beats, result) in enumerate(tracks):
        audio = tmp_path / "music" / f"{id}.mp3"
        audio.parent.mkdir(exist_ok=True)
        audio.write_bytes(b"audio")
        content = add_analysed(db, analysed, id, sound(0, n), title=id.capitalize(), FolderPath=str(audio),
                               BPM=beats[0][1] if beats else None)
        if beats:
            dat, _ = add_grid(db, content, beats)
            keys[id] = _key(db, id, dat)
            analysed.put_grid_check(id, keys[id], result)
    return keys["half"], db.get_anlz_dir(db.get_content(ID="half")) / "ANLZ0000.DAT"


def _key(db, id: str, dat) -> str:
    """The key of the grid and the audio file of the track, now."""
    return check_grids._key(dat, library.find_files(library.load_tracks(db, with_structure=False, only={id}))[id])


def _args(tmp_path, **fields) -> Namespace:
    defaults = {"playlist": None, "track": None, "workers": 1, "limit": 20, "out": str(tmp_path / "grids.csv"),
                "review": False, "apply": False, "yes": True, "no_play": True}
    return Namespace(**(defaults | fields))


def test_check_grids_shows_the_wrong_grids(grids, tmp_path, capsys):
    check_grids.run(_args(tmp_path))

    with open(tmp_path / "grids.csv", newline="") as f:
        assert [(row["id"], row["verdict"], row["better_bpm"]) for row in csv.DictReader(f)] == [
            ("right", "ok", ""), ("half", "wrong tempo", "170.0")]
    output = capsys.readouterr().out
    assert "3 tracks: 1 without an audio file or a rekordbox grid, 0 to check" in output
    assert "ok: 1, wrong tempo: 1" in output
    assert "wrong tempo  85.00 -> 170.00 BPM  Artist - Half" in output
    assert "Listen to the 1 better grids with clicks: dj-librarian check-grids --review" in output


def test_an_answer_for_an_old_grid_does_not_count(grids, tmp_path, capsys):
    store.write_json(store.GRID_DECISIONS, {"half": {"key": "an older grid", "keep": True}})

    check_grids.run(_args(tmp_path))

    assert "Listen to the 1 better grids" in capsys.readouterr().out


def test_check_grids_apply_writes_the_accepted_grid(db, grids, tmp_path, capsys):
    key, dat = grids
    store.write_json(store.GRID_DECISIONS, {"half": {"key": key, "bpm": 170.0, "phase": 0.142, "downbeat": 0,
                                                     "offset": -0.058}})

    check_grids.run(_args(tmp_path, apply=True))

    entries = AnlzFile.parse_file(dat).get_tag("PQTZ").content.entries
    assert {e.tempo for e in entries} == {17000} and len(entries) == pytest.approx(170, abs=1)  # 60 s at 170 BPM
    assert db.get_content(ID="half").BPM == 17000
    decision = store.read_json(store.GRID_DECISIONS, {})["half"]
    assert decision == {"key": _key(db, "half", dat), "keep": True, "written": True}  # The next check does not ask
    assert str(dat) in store.read_json(writer.backups()[0] / writer.ANALYSIS_MANIFEST, {}).values()
    assert "written: 1 tracks" in capsys.readouterr().out


def test_check_grids_review_without_open_grids(grids, tmp_path, capsys):
    key, _ = grids
    store.write_json(store.GRID_DECISIONS, {"half": {"key": key, "keep": True}})

    check_grids.run(_args(tmp_path, review=True))

    assert capsys.readouterr().out.endswith("no grid to review\n")


def test_check_grids_stops_if_no_track_matches(grids, tmp_path):
    with pytest.raises(SystemExit, match="no track matches 'unknown'"):
        check_grids.run(_args(tmp_path, track="unknown"))
