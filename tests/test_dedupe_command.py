import csv
from argparse import Namespace

import pytest

from dj_librarian import dedupe, proposals, store, writer
from dj_librarian.commands import dedupe as command

from helpers import add_analysed, library_copy, opened, sound


@pytest.fixture
def library(db, analysed, tmp_path):
    """Return a function that adds a track. Its file is in a temporary folder, or nowhere if `on_disk` is False."""

    def add(id, place, embedding, title, genre=None, plays=0, on_disk=True):
        path = tmp_path / ("music" if on_disk else "gone") / place
        if on_disk:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
        return add_analysed(db, analysed, id, embedding, genre, title, FolderPath=str(path), DJPlayCount=str(plays))

    return add


def _two_copies(add):
    """Two copies of "Pressure": dj-librarian keeps the one with the plays. A missing file that you did not move."""
    copy = sound(0, 0)
    add("kept", "Release/pressure.mp3", copy, "Pressure", plays=3)
    extra = add("extra", "Set/pressure.mp3", copy, "Pressure", "Jungle")
    add("solo", "Release/other.mp3", sound(1, 1), "Other")
    add("lost", "Old/lost.mp3", sound(2, 2), "Lost", on_disk=False)
    return extra


def _args(tmp_path, **fields) -> Namespace:
    defaults = {"apply": False, "yes": True, "limit": 10, "out": str(tmp_path / "dedupe.csv"), "db": None}
    return Namespace(**(defaults | fields))


def _rows(tmp_path) -> list[tuple[str, str, str]]:
    with open(tmp_path / "dedupe.csv", newline="") as f:
        return [(row["id"], row["role"], row["genre"]) for row in csv.DictReader(f)]


def _playlist(db, name) -> list[str]:
    playlist = db.get_playlist(Name=name).first()
    return sorted(s.ContentID for s in db.get_playlist_songs(PlaylistID=playlist.ID)) if playlist else []


def test_dedupe_shows_the_copies_and_writes_nothing(db, library, tmp_path, capsys):
    _two_copies(library)

    command.run(_args(tmp_path))

    assert _rows(tmp_path) == [("kept", "keep", ""), ("extra", "remove", "Jungle")]
    assert db.get_playlist(Name=dedupe.DUPLICATES_PLAYLIST).first() is None
    output = capsys.readouterr().out
    assert "1 files are missing, and no other entry has the same file." in output
    assert "Artist - Lost" in output
    assert "Artist - Pressure  (keep: more plays)" in output
    assert "genre: the kept copy gets your genre Jungle" in output
    assert "1 copies to remove, 0 tracks with two different genres." in output
    assert "To make these changes: dj-librarian dedupe --apply" in output


def test_dedupe_apply_merges_the_copies(db, library, tmp_path, capsys):
    extra = _two_copies(library)
    friday = db.create_playlist("Friday")
    db.add_to_playlist(friday, extra)

    command.run(_args(tmp_path, apply=True))

    assert _playlist(db, "Friday") == ["kept"]
    assert _playlist(db, dedupe.DUPLICATES_PLAYLIST) == ["extra"]
    assert proposals.load_genre_decisions() == {"kept": "Jungle"}  # The next `apply` writes it
    assert len(writer.backups()) == 1
    output = capsys.readouterr().out
    assert "1 kept copies get the genre of another copy at the next `dj-librarian apply`." in output
    assert "to undo: dj-librarian restore" in output


def test_dedupe_merges_a_moved_file_with_its_old_entry(db, library, tmp_path, capsys):
    library("old", "Artist/song.mp3", sound(0, 0), "Song", plays=9, on_disk=False)
    library("new", "Artist/song.mp3", sound(1, 1), "Song")

    command.run(_args(tmp_path, apply=True))

    assert _playlist(db, dedupe.DUPLICATES_PLAYLIST) == ["old"]
    assert db.get_content(ID="new").DJPlayCount == "9"
    output = capsys.readouterr().out
    assert "Artist - Song  (keep: the file of the other entry is missing)" in output
    assert "1 of them have an entry with a missing file" in output
    assert "files are missing" not in output


def test_dedupe_waits_with_the_copies_of_a_moved_file(db, library, tmp_path, capsys):
    library("old", "Artist/song.mp3", sound(0, 0), "Song", on_disk=False)
    library("new", "Artist/song.mp3", sound(1, 1), "Song")
    library("copy", "Set/song.mp3", sound(1, 1), "Song")

    command.run(_args(tmp_path))

    assert _rows(tmp_path) == [("new", "keep", ""), ("old", "remove", "")]
    assert "1 tracks have a missing entry and more than one copy on the disk." in capsys.readouterr().out


def test_dedupe_writes_nothing_if_you_do_not_confirm(db, library, tmp_path, monkeypatch, capsys):
    _two_copies(library)
    monkeypatch.setattr("builtins.input", lambda question: "n")

    command.run(_args(tmp_path, apply=True, yes=False))

    assert db.get_playlist(Name=dedupe.DUPLICATES_PLAYLIST).first() is None
    assert writer.backups() == [] and proposals.load_genre_decisions() == {}
    assert "cancelled. dj-librarian wrote nothing." in capsys.readouterr().out


def test_dedupe_to_a_test_copy_does_not_change_the_library_or_your_answers(db, library, home, tmp_path):
    _two_copies(library)
    copy = library_copy(home, tmp_path)

    command.run(_args(tmp_path, apply=True, db=str(copy)))

    assert db.get_playlist(Name=dedupe.DUPLICATES_PLAYLIST).first() is None
    with opened(copy) as written:
        assert _playlist(written, dedupe.DUPLICATES_PLAYLIST) == ["extra"]
    assert not store.GENRE_DECISIONS.exists() and writer.backups() == []


def test_dedupe_without_copies(library, tmp_path, capsys):
    library("one", "Release/one.mp3", sound(0, 0), "One")
    library("two", "Release/two.mp3", sound(1, 1), "Two")

    command.run(_args(tmp_path))

    assert capsys.readouterr().out.endswith("no duplicates\n")
