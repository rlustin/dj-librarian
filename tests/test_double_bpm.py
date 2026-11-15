from argparse import Namespace

import pytest
from pyrekordbox import AnlzFile

from dj_librarian import checks, store, writer
from dj_librarian.commands import double_bpm

import anlz
from helpers import add_analysed, add_grid, sound

HALF = anlz.grid(85)


@pytest.fixture
def library(db, analysed):
    """Jungle at 170 BPM, and three Jungle tracks at 85 BPM. Only "half" has analysis files."""
    for n in range(checks.BPM_MIN_GENRE_TRACKS):
        add_analysed(db, analysed, f"j{n}", sound(0, n), "Jungle", f"Jungle {n}", BPM=17000)
    for n, (id, title) in enumerate([("half", "Half"), ("no-files", "No files"), ("live", "Live set")]):
        content = add_analysed(db, analysed, id, sound(0, 20 + n), "Jungle", title, BPM=8500, Length=60)
        if id == "half":
            files = add_grid(db, content, HALF)
    return files


def _args(**fields) -> Namespace:
    return Namespace(**({"apply": False, "yes": True, "track": None, "exclude": ["live"]} | fields))


def _grid(dat) -> list:
    return AnlzFile.parse_file(dat).get_tag("PQTZ").content.entries


def test_double_bpm_shows_the_changes_and_writes_nothing(db, library, capsys):
    dat, _ = library
    before = dat.read_bytes()

    double_bpm.run(_args())

    assert dat.read_bytes() == before and db.get_content(ID="half").BPM == 8500
    output = capsys.readouterr().out
    assert "1 track has half the BPM of its genre." in output
    assert " 85.00 -> 170.00 BPM  Artist - Half  (Jungle, median 170)" in output
    assert "excluded with --exclude (1):\n  Artist - Live set" in output
    assert "dj-librarian does not change these tracks (1):\n  Artist - No files: no analysis files" in output
    assert "To make these changes: dj-librarian double-bpm --apply" in output


def test_double_bpm_apply_doubles_the_bpm_and_the_grid(db, library, capsys):
    dat, ext = library

    double_bpm.run(_args(apply=True))

    assert db.get_content(ID="half").BPM == 17000
    assert len(_grid(dat)) == 2 * len(HALF) and {e.tempo for e in _grid(dat)} == {17000}
    backup = writer.backups()[0]
    assert sorted(store.read_json(backup / writer.ANALYSIS_MANIFEST, {}).values()) == [str(dat), str(ext)]
    assert "written: 1 track" in capsys.readouterr().out


def test_double_bpm_of_one_track(db, library, capsys):
    double_bpm.run(_args(track="no files", exclude=[]))

    output = capsys.readouterr().out
    assert "0 tracks have half the BPM of their genre" in output and "Artist - No files: no analysis files" in output
    assert "Live set" not in output and "Artist - Half" not in output


def test_double_bpm_writes_nothing_if_you_do_not_confirm(db, library, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda question: "n")

    double_bpm.run(_args(apply=True, yes=False))

    assert db.get_content(ID="half").BPM == 8500 and writer.backups() == []
    assert "cancelled. dj-librarian wrote nothing." in capsys.readouterr().out
