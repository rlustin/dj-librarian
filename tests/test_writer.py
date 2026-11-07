from dj_librarian import store, tagging, writer
from dj_librarian.proposals import Proposal

from helpers import add_content, add_cue, cues, track


def _proposal(t, genre_write=None, comment="", cues=None, previous=None):
    plan = tagging.CuePlan(positions={}, rekordbox_chorus=None, writes=cues or {}, previous=previous or {})
    return Proposal(
        track=t, analysis={}, bpm_check="match", key_ours=None, key_check="match",
        genre_guess=tagging.GenreGuess(None, 0.0, ""), genre_action="approved", genre_write=genre_write,
        genre_pending=False, claude=None, energy=5, moods=["Dark"], comment=comment, cues=plan,
    )


def test_changes_only_has_the_differences():
    t = track(genre="Jungle", comment="E5 Dark")

    assert writer.changes(_proposal(t, "Jungle", "E5 Dark"), set(writer.PARTS)) == {}
    assert writer.changes(_proposal(t, "Grime", "E5 Dark"), set(writer.PARTS)) == {"genre": "Grime"}
    assert writer.changes(_proposal(t, "Grime", "E6 Dark"), {"comment"}) == {"comment": "E6 Dark"}


def test_changes_removes_a_genre():
    proposal = _proposal(track(genre="Bass", comment="E5 Dark"), "", "E5 Dark")

    assert writer.changes(proposal, {"genre"}) == {"genre": ""}
    assert writer.changes(_proposal(track(comment="E5 Dark"), "", "E5 Dark"), {"genre"}) == {}


def test_changes_does_not_write_cues_again_at_almost_the_same_time():
    proposal = _proposal(track(comment=""), cues={"A": ("Start", 1.0)}, previous={"A": ("Start", 1.001)})
    assert writer.changes(proposal, {"cues"}) == {}

    proposal = _proposal(track(comment=""), cues={"A": ("Start", 1.0)}, previous={"A": ("Start", 1.5)})
    assert writer.changes(proposal, {"cues"}) == {"cues": {"A": ("Start", 1.0)}}


def test_backup_and_restore(tmp_path):
    library = tmp_path / "rekordbox"
    library.mkdir()
    (library / "master.db").write_bytes(b"before")
    analysis = tmp_path / "ANLZ0000.DAT"
    analysis.write_bytes(b"grid before")

    backup = writer.backup(library / "master.db")
    writer.backup_analysis(backup, [analysis])
    (library / "master.db").write_bytes(b"after")
    (library / "master.db-wal").write_bytes(b"wal after")
    analysis.write_bytes(b"grid after")
    current = writer.restore(backup, library / "master.db")

    assert (library / "master.db").read_bytes() == b"before"
    assert not (library / "master.db-wal").exists()
    assert analysis.read_bytes() == b"grid before"

    writer.restore(current, library / "master.db")  # undo

    assert (library / "master.db").read_bytes() == b"after"
    assert (library / "master.db-wal").read_bytes() == b"wal after"
    assert analysis.read_bytes() == b"grid after"


def test_two_backups_in_the_same_second(tmp_path):
    (tmp_path / "master.db").write_bytes(b"db")

    assert writer.backup(tmp_path / "master.db") != writer.backup(tmp_path / "master.db")


def test_apply_replaces_the_cues_of_dj_librarian_and_keeps_yours(db):
    content = add_content(db, "1")
    add_cue(db, content, "A", 1.0, "yours")
    add_cue(db, content, "B", 16.0, "generated")
    store.write_json(store.GENERATED_CUES, ["generated"])
    proposal = _proposal(track("1"), cues={"B": ("Intro end", 32.0), "C": ("Full energy", 64.0)},
                         previous={"B": ("Intro end", 16.0)})

    writer.apply(db, [proposal], {"cues"})

    found = cues(db, "1")
    assert found["A"] == ("yours", "", 1000)
    assert {slot: (name, msec) for slot, (_, name, msec) in found.items() if slot != "A"} == {
        "B": ("Intro end", 32000), "C": ("Full energy", 64000)}
    assert set(store.read_json(store.GENERATED_CUES, [])) == {found["B"][0], found["C"][0]}


def test_apply_gives_a_name_to_your_cue_without_a_name(db):
    content = add_content(db, "1")
    add_cue(db, content, "A", 8.0, "yours")
    proposal = _proposal(track("1"))
    proposal.cues.labels = {"A": "Start"}

    writer.apply(db, [proposal], {"cues"})

    assert cues(db, "1") == {"A": ("yours", "Start", 8000)}
    assert store.read_json(store.GENERATED_CUES, []) == []  # The cue stays yours


def test_apply_creates_a_genre_and_keeps_a_record_of_the_guesses(db):
    add_content(db, "1")
    add_content(db, "2")
    guess = _proposal(track("1"), genre_write="Jungle", comment="E5 Dark")
    guess.genre_action = "fill"
    approved = _proposal(track("2"), genre_write="Jungle")

    writer.apply(db, [guess, approved], {"genre", "comment"})

    assert [g.Name for g in db.get_genre().all()] == ["Jungle"]
    assert db.get_content(ID="1").Genre.Name == "Jungle" and db.get_content(ID="1").Commnt == "E5 Dark"
    assert db.get_content(ID="2").Genre.Name == "Jungle"
    assert store.read_json(store.GUESSED_GENRES, {}) == {"1": "Jungle"}  # Only the guess of dj-librarian


def test_apply_removes_a_genre(db):
    add_content(db, "1", genre=db.add_genre("Bass"))
    store.write_json(store.GUESSED_GENRES, {"1": "Bass"})

    writer.apply(db, [_proposal(track("1", genre="Bass"), genre_write="")], {"genre"})

    assert db.get_content(ID="1").GenreID is None
    assert store.read_json(store.GUESSED_GENRES, {}) == {}
