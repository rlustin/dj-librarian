from dj_librarian import tagging, writer
from dj_librarian.proposals import Proposal

from helpers import track


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
