"""Helpers that more than one command uses."""

import csv
import os
import sys
from collections import Counter
from pathlib import Path

from dj_librarian import claude_genres, library, proposals, writer
from dj_librarian.cache import Cache
from dj_librarian.progress import Steps
from dj_librarian.terminal import warn


def quiet_stderr() -> None:
    """For worker processes: TensorFlow writes start-up logs from C++, also with TF_CPP_MIN_LOG_LEVEL."""
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stderr.fileno())


def track_name(t: library.Track) -> str:
    return f"{t.artist} - {t.title}"


def matches(t: library.Track, text: str) -> bool:
    """The value of --track: a content ID, or a part of 'artist - title'."""
    return t.id == text or text.lower() in track_name(t).lower()


def fmt_time(seconds: float | None) -> str:
    return "" if seconds is None else f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def neighbours(counter: Counter) -> str:
    return ", ".join(f"{g} {n}" for g, n in counter.most_common(3))


def confirm(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() == "y"


def write_csv(path: str, rows: list[dict], fields: list[str] | None = None) -> None:
    with open(path, "w", newline="") as f:
        out = csv.DictWriter(f, fieldnames=fields or list(rows[0]))
        out.writeheader()
        out.writerows(rows)


def select(db, args) -> tuple[list[proposals.Proposal], proposals.Context]:
    track_ids = None
    if getattr(args, "track", None):
        found = [t for t in library.load_tracks(db, with_structure=False) if matches(t, args.track)]
        if len(found) != 1:
            listing = "\n".join(f"  {t.id}  {track_name(t)}  ({t.path})" for t in found[:20])
            raise SystemExit(f"--track must match exactly one track, matched {len(found)}\n{listing}")
        track_ids = {found[0].id}
    selected, context = proposals.build(db, args.playlist, track_ids, steps=Steps())
    if not selected:
        raise SystemExit("none of the selected tracks has an analysis. Run `dj-librarian analyze` first.")
    return selected, context


def load_checks(db, steps: Steps):
    from dj_librarian import checks

    steps.step("Load library and analysis")
    analysis = Cache().all()
    tracks = [t for t in library.load_tracks(db, with_structure=False) if t.id in analysis]
    decisions = proposals.load_genre_decisions()
    yours = proposals.your_genres(tracks, proposals.load_guessed_genres(), decisions)
    current = {t.id: g for t in tracks if (g := t.genre if decisions.get(t.id) is None else decisions[t.id])}
    steps.step("Compare the tracks")
    report = checks.run(tracks, analysis, yours, current)
    steps.done()
    return tracks, analysis, yours, report


def write_target(db: library.MasterDatabase, test_copy: str | None, command: str) -> library.MasterDatabase:
    """The database to write: the test copy of --db, or the real library after a backup."""
    if test_copy:
        return writer.open_test_copy(test_copy)
    writer.ensure_rekordbox_closed()
    make_backup(command, db)
    return db


def make_backup(command: str, db: library.MasterDatabase, analysis: list[Path] | None = None) -> None:
    """Back up master.db, and the analysis files that the command changes. After the write, use clean_backups."""
    backup = writer.backup(writer.db_path(), command)
    if analysis is None:
        print(f"backup: {backup}")
    else:
        writer.backup_analysis(backup, analysis)
        print(f"backup (master.db and analysis files): {backup}")
    try:
        claude_genres.remember_urls(db, backup)
    except (OSError, ValueError) as error:  # Not a reason to stop: dj-librarian reads the backup later
        print(warn(f"dj-librarian cannot keep the Bandcamp links of the comments now: {error}"))


def clean_backups() -> None:
    """Delete the old backups. Do this after the write: a problem here must not stop a write that you confirmed."""
    try:
        if unread := claude_genres.unread_backups(writer.old_backups()):
            print(f"dj-librarian reads the Bandcamp links of {plural(len(unread), 'old backup')}. "
                  "Then it deletes them. This can take some minutes.")
        cleanup = writer.delete_old_backups()
    except Exception as error:  # noqa: BLE001 - the write is complete, and the next write tries again
        print(warn(f"dj-librarian cannot delete the old backups: {error}"))
        return
    if cleanup.deleted:
        print(f"deleted {plural(len(cleanup.deleted), 'old backup')}. dj-librarian keeps the first backup "
              f"and the last {writer.KEEP_BACKUPS}. The first backup keeps the original grids.")
    if cleanup.stopped_at:
        print(warn(f"dj-librarian cannot read {cleanup.stopped_at}. Thus it does not delete it or the newer "
                   "backups. If you do not need it, delete it yourself."))
    if cleanup.error:
        print(warn(f"{cleanup.error}\ndj-librarian tries again at the next write."))


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")
