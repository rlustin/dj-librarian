import csv
from argparse import Namespace
from collections import Counter

import pytest

from dj_librarian import dedupe
from dj_librarian.commands import check

from helpers import add_analysed, sound, two_genres


@pytest.fixture
def library(db, analysed):
    """Grime and Jungle, a Grime track that sounds like Jungle, and two copies of a track with two spellings of Dub."""
    two_genres(db, analysed, 10)
    add_analysed(db, analysed, "odd", sound(0, 20), "Grime", "Odd one")
    copy = sound(2, 21)
    add_analysed(db, analysed, "copy1", copy, "Dub", "Copy")
    add_analysed(db, analysed, "copy2", copy, "dub", "Copy")
    return db


def _run(tmp_path, **fields) -> list[dict]:
    out = tmp_path / "check.csv"
    check.run(Namespace(**({"genre": None, "limit": 10, "out": str(out)} | fields)))
    with open(out, newline="") as f:
        return list(csv.DictReader(f))


def test_check_shows_what_looks_wrong(library, tmp_path, capsys):
    rows = _run(tmp_path)

    assert Counter(row["kind"] for row in rows) == {"duplicate": 1, "genre name": 1, "outlier": 1, "genre": 2}
    assert next(row for row in rows if row["kind"] == "outlier")["id"] == "odd"
    output = capsys.readouterr().out
    assert ("Duplicates: 1 pair of copies of the same track, 1 of them with different genres. "
            "1 to merge with `dj-librarian dedupe`.") in output
    assert "Artist - Copy [Dub]  =  Artist - Copy [dub]" in output
    assert "Other versions with the same sound: 0 pairs" in output
    assert "same name, other spelling: Dub (1), dub (1)" in output
    assert "Tracks that do not sound like their genre: 1." in output
    assert "Artist - Odd one: Grime. Nearest: Jungle 10" in output
    assert f"5 rows -> {tmp_path / 'check.csv'}" in output


def test_check_shows_the_copies_that_wait_for_their_removal(library, tmp_path, capsys):
    playlist = library.create_playlist(dedupe.DUPLICATES_PLAYLIST)
    library.add_to_playlist(playlist, library.get_content(ID="copy2"))
    library.commit()

    _run(tmp_path)

    output = capsys.readouterr().out
    assert f'1 merged, in the playlist "{dedupe.DUPLICATES_PLAYLIST}": remove them from the collection' in output
    assert "to merge with `dj-librarian dedupe`" not in output


def test_check_limits_the_lines_but_not_the_csv(library, tmp_path, capsys):
    rows = _run(tmp_path, limit=0)

    assert len(rows) == 5
    assert "... and 1 more (see" in capsys.readouterr().out


def test_check_of_a_genre_lists_its_tracks(library, tmp_path, capsys):
    rows = _run(tmp_path, genre="grime")

    assert [row["kind"] for row in rows] == ["crate"] * 11
    assert rows[-1]["id"] == "odd"  # The least typical sound
    output = capsys.readouterr().out
    assert "Grime: 11 tracks." in output
    assert "! " in next(line for line in output.splitlines() if "Odd one" in line)


def test_check_of_an_unknown_genre_shows_the_genres(library, tmp_path):
    with pytest.raises(SystemExit, match='no track has the genre "Techno". Genres: Dub, Grime, Jungle, dub'):
        _run(tmp_path, genre="Techno")
