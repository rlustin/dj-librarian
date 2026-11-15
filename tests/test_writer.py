import shutil

import pytest

from dj_librarian import claude_genres, store, tagging, writer
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


def test_backup_and_restore(home):
    library = home / "rekordbox"
    library.mkdir()
    (library / "master.db").write_bytes(b"before")
    analysis = home / "ANLZ0000.DAT"
    analysis.write_bytes(b"grid before")

    backup = writer.backup(library / "master.db", "check-grids")
    writer.backup_analysis(backup, [analysis])
    (library / "master.db").write_bytes(b"after")
    (library / "master.db-wal").write_bytes(b"wal after")
    analysis.write_bytes(b"grid after")
    current = writer.restore(backup, library / "master.db")

    assert (library / "master.db").read_bytes() == b"before"
    assert not (library / "master.db-wal").exists()
    assert analysis.read_bytes() == b"grid before"
    assert writer.backup_command(backup) == "check-grids"
    assert writer.backup_command(current) == "restore"

    writer.restore(current, library / "master.db")  # undo

    assert (library / "master.db").read_bytes() == b"after"
    assert (library / "master.db-wal").read_bytes() == b"wal after"
    assert analysis.read_bytes() == b"grid after"


def test_restore_puts_back_the_records_of_the_writes_and_keeps_your_answers(home):
    (home / "master.db").write_bytes(b"db")
    store.write_json(store.GUESSED_GENRES, {"1": "House"})
    store.write_json(store.GENRE_DECISIONS, {"2": "Techno"})

    backup = writer.backup(home / "master.db", "apply")
    store.write_json(store.GUESSED_GENRES, {"1": "House", "3": "Dub"})
    store.write_json(store.GENERATED_CUES, ["cue-uuid"])
    store.write_json(store.GENRE_DECISIONS, {"2": "Techno", "4": "Footwork"})
    writer.restore(backup, home / "master.db")

    assert store.read_json(store.GUESSED_GENRES, None) == {"1": "House"}
    assert not store.GENERATED_CUES.exists()  # dj-librarian wrote no cues before the backup
    assert store.read_json(store.GENRE_DECISIONS, None) == {"2": "Techno", "4": "Footwork"}
    assert store.read_json(backup / writer.RECORDS_DIR / store.GENRE_DECISIONS.name, None) == {"2": "Techno"}


def test_restore_of_a_backup_without_records_keeps_the_records(home):
    old = store.BACKUP_DIR / "2026-10-25_202245"
    old.mkdir(parents=True)
    (old / "master.db").write_bytes(b"old")
    (home / "master.db").write_bytes(b"db")
    store.write_json(store.GUESSED_GENRES, {"1": "House"})

    writer.restore(old, home / "master.db")

    assert (home / "master.db").read_bytes() == b"old"
    assert store.read_json(store.GUESSED_GENRES, None) == {"1": "House"}


def test_two_backups_in_the_same_second(home):
    (home / "master.db").write_bytes(b"db")

    assert writer.backup(home / "master.db", "apply") != writer.backup(home / "master.db", "apply")


def _backups(count: int) -> list[str]:
    names = [f"2026-11-{day:02d}_120000" for day in range(1, count + 1)]
    for name in names:
        (store.BACKUP_DIR / name).mkdir(parents=True)
        (store.BACKUP_DIR / name / "master.db").write_bytes(b"db")
    return names


def test_delete_old_backups_keeps_the_first_and_the_last_ones(home, monkeypatch):
    names = _backups(writer.KEEP_BACKUPS + 3)
    read = []
    monkeypatch.setattr(claude_genres, "_backup_urls", lambda backup: read.append(backup.name) or {})

    cleanup = writer.delete_old_backups()

    assert [b.name for b in cleanup.deleted] == read == names[1:3]
    assert cleanup.stopped_at is None and cleanup.error is None
    assert [b.name for b in writer.backups()] == names[:1] + names[3:]
    assert writer.delete_old_backups() == writer.Cleanup()


def test_delete_old_backups_deletes_nothing_with_few_backups(home):
    _backups(writer.KEEP_BACKUPS + 1)

    assert writer.delete_old_backups() == writer.Cleanup()
    assert len(writer.backups()) == writer.KEEP_BACKUPS + 1


def test_the_deletion_stops_at_a_backup_that_it_cannot_read_and_tries_again_later(home, monkeypatch):
    names = _backups(writer.KEEP_BACKUPS + 3)
    damaged = {names[1]}

    def read(backup):
        if backup.name in damaged:
            raise ValueError("damaged")
        return {"7": f"https://artist.bandcamp.com/track/{backup.name}"}

    monkeypatch.setattr(claude_genres, "_backup_urls", read)

    cleanup = writer.delete_old_backups()

    assert cleanup.deleted == [] and cleanup.stopped_at.name == names[1]
    damaged.clear()  # For example, a network share that was not available for a moment
    cleanup = writer.delete_old_backups()

    assert [b.name for b in cleanup.deleted] == names[1:3]
    assert store.read_json(store.SOURCE_URLS, None) == {
        "backups": names[1:3], "urls": {"7": f"https://artist.bandcamp.com/track/{names[1]}"}}  # The oldest link


def test_the_cleanup_saves_the_links_of_each_backup_at_once(home, monkeypatch):
    names = _backups(writer.KEEP_BACKUPS + 3)

    def read(backup):
        if backup.name == names[2]:
            raise KeyboardInterrupt
        return {}

    monkeypatch.setattr(claude_genres, "_backup_urls", read)

    with pytest.raises(KeyboardInterrupt):
        writer.delete_old_backups()

    assert claude_genres.unread_backups(writer.old_backups()) == [store.BACKUP_DIR / names[2]]


def test_delete_old_backups_keeps_a_backup_with_another_name(home, monkeypatch):
    _backups(writer.KEEP_BACKUPS + 3)
    (store.BACKUP_DIR / "before-the-party").mkdir()
    monkeypatch.setattr(claude_genres, "_backup_urls", lambda backup: {})

    writer.delete_old_backups()

    assert [b.name for b in writer.kept_backups()] == ["before-the-party"]
    assert "before-the-party" not in [b.name for b in writer.backups()]


def test_the_first_backup_keeps_the_original_grids_of_the_deleted_backups(home, monkeypatch):
    library = home / "rekordbox"
    library.mkdir()
    (library / "master.db").write_bytes(b"original")
    one, two = home / "ANLZ1.DAT", home / "ANLZ2.DAT"
    one.write_bytes(b"grid 1 original")
    two.write_bytes(b"grid 2 original")
    monkeypatch.setattr(claude_genres, "_backup_urls", lambda backup: {})
    monkeypatch.setattr(writer, "KEEP_BACKUPS", 1)

    first = writer.backup(library / "master.db", "apply")
    (library / "master.db").write_bytes(b"after apply")
    writer.backup_analysis(writer.backup(library / "master.db", "check-grids"), [one])
    one.write_bytes(b"grid 1 new")
    writer.backup_analysis(writer.backup(library / "master.db", "check-grids"), [one, two])
    one.write_bytes(b"grid 1 newer")
    two.write_bytes(b"grid 2 new")
    writer.backup(library / "master.db", "apply")
    deleted = writer.delete_old_backups().deleted

    assert len(deleted) == 2
    writer.restore(first, library / "master.db")
    assert (library / "master.db").read_bytes() == b"original"
    assert one.read_bytes() == b"grid 1 original"
    assert two.read_bytes() == b"grid 2 original"


def _grid_backups(home, monkeypatch, unreadable=()):
    """The first backup, then 2 check-grids backups of one grid, then a last backup."""
    library = home / "rekordbox"
    library.mkdir()
    (library / "master.db").write_bytes(b"db")
    grid = home / "ANLZ1.DAT"
    grid.write_bytes(b"grid original")
    monkeypatch.setattr(writer, "KEEP_BACKUPS", 1)
    unreadable_paths = []

    def read(backup):
        if backup in unreadable_paths:
            raise ValueError("damaged")
        return {}

    monkeypatch.setattr(claude_genres, "_backup_urls", read)
    first = writer.backup(library / "master.db", "apply")
    made = []
    for value in (b"grid new", b"grid newer"):
        made.append(writer.backup(library / "master.db", "check-grids"))
        writer.backup_analysis(made[-1], [grid])
        grid.write_bytes(value)
    writer.backup(library / "master.db", "apply")
    unreadable_paths.extend(made[n] for n in unreadable)
    return library, grid, first, made


def test_a_backup_that_it_cannot_read_stops_the_grids_of_the_newer_backups(home, monkeypatch):
    library, grid, first, made = _grid_backups(home, monkeypatch, unreadable=[0])

    cleanup = writer.delete_old_backups()

    assert (cleanup.deleted, cleanup.stopped_at) == ([], made[0])
    assert len(store.read_json(first / writer.ANALYSIS_MANIFEST, {})) == 1  # Only the grid of made[0]
    writer.restore(first, library / "master.db")
    assert grid.read_bytes() == b"grid original"


def test_a_damaged_manifest_stops_the_grids_of_the_newer_backups(home, monkeypatch):
    _, _, first, made = _grid_backups(home, monkeypatch)
    (made[0] / writer.ANALYSIS_MANIFEST).write_text("{")

    cleanup = writer.delete_old_backups()

    assert (cleanup.deleted, cleanup.stopped_at) == ([], made[0])
    assert not (first / writer.ANALYSIS_MANIFEST).exists()


def test_a_backup_without_its_analysis_files_does_not_stop_the_deletion(home, monkeypatch):
    library, grid, first, made = _grid_backups(home, monkeypatch)
    for path in (made[0] / writer.ANALYSIS_DIR).iterdir():
        path.unlink()

    cleanup = writer.delete_old_backups()

    assert (cleanup.deleted, cleanup.stopped_at) == (made, None)
    assert not (store.BACKUP_DIR / ".deleting").exists()
    writer.restore(first, library / "master.db")
    assert grid.read_bytes() == b"grid new"  # The oldest copy that stays


def test_the_first_backup_stays_the_first_with_another_name(home, monkeypatch):
    library, grid, first, made = _grid_backups(home, monkeypatch)
    renamed = first.rename(store.BACKUP_DIR / "before-dj-librarian")

    deleted = writer.delete_old_backups().deleted

    assert writer.first_backup() == renamed
    assert deleted == made
    writer.restore(renamed, library / "master.db")
    assert grid.read_bytes() == b"grid original"


def test_a_backup_with_another_name_gives_its_grids_to_the_first_backup(home, monkeypatch):
    library, grid, first, made = _grid_backups(home, monkeypatch)
    kept = made[0].rename(store.BACKUP_DIR / "keep-me")

    cleanup = writer.delete_old_backups()

    assert cleanup.deleted == made[1:]
    assert kept.exists()
    writer.restore(first, library / "master.db")
    assert grid.read_bytes() == b"grid original"


def test_a_backup_with_another_name_waits_for_the_older_backups(home, monkeypatch):
    _, _, first, made = _grid_backups(home, monkeypatch)
    monkeypatch.setattr(writer, "KEEP_BACKUPS", 3)  # made[0] stays in the last backups
    made[1].rename(store.BACKUP_DIR / "keep-me")

    assert writer._grid_sources(writer.old_backups(), first, writer.backups()) == []


def test_a_failure_of_the_deletion_shows_an_error(home, monkeypatch):
    _, _, first, made = _grid_backups(home, monkeypatch)

    def fail(path):
        raise PermissionError(f"no permission: {path}")

    monkeypatch.setattr(writer.shutil, "rmtree", fail)

    cleanup = writer.delete_old_backups()

    assert "no permission" in cleanup.error
    assert not any(path.exists() for path in made)  # In the trash folder, which the next write deletes
    assert writer.backups() == [first, writer.backups()[-1]]


def test_a_damaged_first_backup_stops_the_deletion_and_the_error_names_it(home, monkeypatch):
    _, _, first, made = _grid_backups(home, monkeypatch)
    (first / writer.ANALYSIS_MANIFEST).write_text("{")

    cleanup = writer.delete_old_backups()

    assert cleanup.deleted == [] and cleanup.stopped_at is None
    assert str(first) in cleanup.error
    assert all(path.exists() for path in made)


def test_a_backup_from_before_the_first_backup_does_not_give_its_grids(home, monkeypatch):
    library, grid, first, _ = _grid_backups(home, monkeypatch)
    older = store.BACKUP_DIR / "2026-01-01_000000"
    older.mkdir()
    writer.backup_analysis(older, [grid])  # A grid from before the master.db of the first backup
    store.write_json(older / writer.BACKUP_INFO, {"made": older.name})
    older.rename(store.BACKUP_DIR / "very-old")

    writer.delete_old_backups()

    writer.restore(first, library / "master.db")
    assert grid.read_bytes() == b"grid original"


def test_a_damaged_backup_json_does_not_stop_a_backup(home):
    (home / "master.db").write_bytes(b"db")
    first = writer.backup(home / "master.db", "apply")
    (first / writer.BACKUP_INFO).write_text("{")

    writer.backup(home / "master.db", "apply")

    assert writer.backup_command(first) is None
    assert writer.first_backup() == first


def test_a_full_disk_stops_the_deletion(home, monkeypatch):
    _, _, first, made = _grid_backups(home, monkeypatch)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(writer.shutil, "copy2", full)

    cleanup = writer.delete_old_backups()

    assert "No space left" in cleanup.error and str(first) in cleanup.error
    assert cleanup.deleted == []
    assert all(path.exists() for path in made)


def test_the_first_backup_of_an_older_library_gets_the_mark(home):
    old = store.BACKUP_DIR / "2026-10-25_202245"
    old.mkdir(parents=True)
    (home / "master.db").write_bytes(b"db")

    writer.backup(home / "master.db", "apply")
    old.rename(store.BACKUP_DIR / "before-dj-librarian")

    assert writer.first_backup() == store.BACKUP_DIR / "before-dj-librarian"


def test_the_first_backup_gets_the_grids_of_a_backup_with_links_that_it_cannot_read(home, monkeypatch):
    library, grid, first, made = _grid_backups(home, monkeypatch, unreadable=[0])

    assert writer.delete_old_backups().stopped_at == made[0]
    shutil.rmtree(made[0])  # As the warning tells you
    writer.delete_old_backups()

    writer.restore(first, library / "master.db")
    assert grid.read_bytes() == b"grid original"


def test_restore_skips_an_analysis_file_without_its_folder(home):
    folder = home / "PIONEER" / "USBANLZ" / "abc"
    folder.mkdir(parents=True)
    (folder / "ANLZ0000.DAT").write_bytes(b"grid")
    (home / "master.db").write_bytes(b"db")
    backup = writer.backup(home / "master.db", "check-grids")
    writer.backup_analysis(backup, [folder / "ANLZ0000.DAT"])
    (folder / "ANLZ0000.DAT").unlink()
    folder.rmdir()

    assert writer.grids_without_folder(backup) == [str(folder / "ANLZ0000.DAT")]
    writer.restore(backup, home / "master.db")

    assert not folder.exists()


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
