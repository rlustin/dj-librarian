from argparse import Namespace

import pytest

from dj_librarian import store, writer
from dj_librarian.commands import apply

from helpers import cues, fill_and_review, library_copy, opened


def _args(**fields) -> Namespace:
    return Namespace(**({"playlist": None, "track": None, "only": None, "db": None, "yes": True} | fields))


@pytest.fixture
def library(db, analysed):
    fill_and_review(db, analysed)
    return db


def test_apply_writes_the_proposals_and_keeps_a_record(library, capsys):
    apply.run(_args())

    fill, review = library.get_content(ID="fill"), library.get_content(ID="review")
    assert fill.Genre.Name == "Jungle" and fill.Commnt.startswith("E6 ")
    assert review.Genre.Name == "Grime"  # The change waits for your review
    start = cues(library, "fill")["A"]
    assert start[1:] == ("Start", 500)
    assert store.read_json(store.GUESSED_GENRES, None) == {"fill": "Jungle"}
    assert start[0] in store.read_json(store.GENERATED_CUES, [])
    assert len(writer.backups()) == 1
    output = capsys.readouterr().out
    assert "14 tracks will change: genre 1, comment 14, cues 14 (on 14 tracks)" in output
    assert "genre:   (empty)  ->  Jungle" in output
    assert "1 genre change waits for review" in output
    assert "to undo: dj-librarian restore" in output


def test_apply_finds_nothing_to_change_after_a_write(library, capsys):
    apply.run(_args())
    capsys.readouterr()

    apply.run(_args())

    assert capsys.readouterr().out.endswith("nothing to change\n")
    assert len(writer.backups()) == 1


def test_apply_writes_nothing_if_you_do_not_confirm(library, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda question: "n")

    apply.run(_args(yes=False))

    assert library.get_content(ID="fill").GenreID is None
    assert writer.backups() == []
    assert "cancelled. dj-librarian wrote nothing." in capsys.readouterr().out


def test_apply_writes_only_the_parts_that_you_choose(library):
    apply.run(_args(only="genre"))

    fill = library.get_content(ID="fill")
    assert fill.Genre.Name == "Jungle" and fill.Commnt is None
    assert cues(library, "fill") == {}


def test_apply_writes_only_the_track_that_you_choose(library):
    apply.run(_args(only="comment", track="fill"))

    assert library.get_content(ID="fill").Commnt.startswith("E6 ")
    assert library.get_content(ID="j0").Commnt is None


def test_apply_stops_at_an_unknown_part(library):
    with pytest.raises(SystemExit, match="unknown parts: tags"):
        apply.run(_args(only="genre,tags"))


def test_apply_to_a_test_copy_does_not_change_the_library_or_the_records(library, home, tmp_path):
    copy = library_copy(home, tmp_path)

    apply.run(_args(db=str(copy)))

    assert library.get_content(ID="fill").GenreID is None
    with opened(copy) as written:
        assert written.get_content(ID="fill").Genre.Name == "Jungle"
    assert not store.GUESSED_GENRES.exists() and not store.GENERATED_CUES.exists()
    assert writer.backups() == []
