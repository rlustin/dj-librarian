"""Write proposals into a rekordbox master.db. Close rekordbox before you write."""

import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pyrekordbox.config import get_config
from pyrekordbox.masterdb import models
from pyrekordbox.utils import get_rekordbox_pid

from dj_librarian import store
from dj_librarian.library import HOT_CUE_KINDS, MasterDatabase
from dj_librarian.proposals import (
    Proposal,
    load_generated_cues,
    load_guessed_genres,
    save_generated_cues,
    save_guessed_genres,
)

SLOT_TO_KIND = {slot: kind for kind, slot in HOT_CUE_KINDS.items()}
PLAYLISTS_XML = "masterPlaylists6.xml"  # rekordbox keeps the playlist tree in this file too
DB_FILES = ("master.db", "master.db-wal", "master.db-shm", PLAYLISTS_XML)
PARTS = ("genre", "comment", "cues")
ANALYSIS_DIR = "analysis"  # copies of rekordbox analysis files (beat grids), if the change touched them
ANALYSIS_MANIFEST = "analysis.json"  # copy name -> original path
RECORDS_DIR = "dj-librarian"  # copies of the files of dj-librarian at the time of the backup
BACKUP_INFO = "backup.json"
KEEP_BACKUPS = 10  # and the first backup, which has the library from before the first write of dj-librarian
BACKUP_NAME = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}(_\d+)?")  # Give a backup another name to keep it


def ensure_rekordbox_closed() -> None:
    if get_rekordbox_pid():
        raise SystemExit("rekordbox is open. Close rekordbox first, because it can overwrite or corrupt the changes.")


def db_path() -> Path:
    config = get_config("rekordbox7").get("db_path") or get_config("rekordbox6")["db_path"]
    return Path(config)


def open_test_copy(path: str) -> MasterDatabase:
    """A copy of master.db, for tests. rekordbox can be open, because it does not use that file."""
    import pyrekordbox.masterdb.database as masterdb

    masterdb.get_rekordbox_pid = lambda *a, **k: 0
    return MasterDatabase(path=path)


def _records() -> list[Path]:
    """Not the Bandcamp links: they come from the backups, and dj-librarian reads them again."""
    return [store.GENRE_DECISIONS, store.GUESSED_GENRES, store.GENERATED_CUES, store.OUTLIERS_REVIEWED,
            store.CLAUDE_ANSWERS, store.GRID_DECISIONS]


def _library_records() -> list[Path]:
    """The records of what dj-librarian wrote. They must agree with master.db.

    grid_decisions.json has records of the grids that dj-librarian wrote, but also your answers. Thus it stays.
    After a restore of grids, its records do not agree with the files, and check-grids asks about them again.
    """
    return [store.GUESSED_GENRES, store.GENERATED_CUES]


def backup(db_path: Path, command: str) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target, n = store.BACKUP_DIR / stamp, 1
    while target.exists():  # Two writes in the same second
        n += 1
        target = store.BACKUP_DIR / f"{stamp}_{n}"
    target.mkdir(parents=True)
    for name in DB_FILES:
        if (db_path.parent / name).exists():
            shutil.copy2(db_path.parent / name, target / name)
    (target / RECORDS_DIR).mkdir()
    for path in _records():
        if path.exists():
            shutil.copy2(path, target / RECORDS_DIR / path.name)
    store.write_json(target / BACKUP_INFO, {"command": command, "made": target.name})
    _complete_info()
    return target


def _folders() -> list[Path]:
    if not store.BACKUP_DIR.exists():
        return []
    return sorted(p for p in store.BACKUP_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))


def backups() -> list[Path]:
    """The backups that dj-librarian made, the oldest first."""
    return [p for p in _folders() if BACKUP_NAME.fullmatch(p.name)]


def kept_backups() -> list[Path]:
    """The backups that you gave another name, the oldest first. dj-librarian does not delete them."""
    return sorted((p for p in _folders() if not BACKUP_NAME.fullmatch(p.name)), key=lambda p: made(p) or "~")


def _info(backup_dir: Path) -> dict:
    try:
        return store.read_json(backup_dir / BACKUP_INFO, {})
    except (OSError, ValueError):  # A damaged backup.json must not stop each later write
        return {}


def backup_command(backup_dir: Path) -> str | None:
    return _info(backup_dir).get("command")


def made(backup_dir: Path) -> str | None:
    """The time of the backup, as in the folder names. The name of a backup can change, this time does not."""
    return _info(backup_dir).get("made") or (backup_dir.name if BACKUP_NAME.fullmatch(backup_dir.name) else None)


def first_backup() -> Path | None:
    """The backup with the library from before the first write. It stays the first one if you give it another name."""
    marked = [p for p in _folders() if _info(p).get("first")]
    found = marked or backups()
    return found[0] if found else None


def _complete_info() -> None:
    """Older backups have no time and no mark of the first backup. Add them while the names show the time."""
    for path in backups():
        if not _info(path).get("made"):
            store.write_json(path / BACKUP_INFO, _info(path) | {"made": path.name})
    first = first_backup()
    if first and not _info(first).get("first"):
        store.write_json(first / BACKUP_INFO, _info(first) | {"first": True})


def old_backups() -> list[Path]:
    return _old(backups(), first_backup())


def _old(found: list[Path], first: Path | None) -> list[Path]:
    return [p for p in found[:-KEEP_BACKUPS] if p != first]


@dataclass
class Cleanup:
    deleted: list[Path] = field(default_factory=list)
    stopped_at: Path | None = None  # A backup that dj-librarian cannot read. It and the newer backups stay.
    error: str | None = None  # A problem that stopped the deletion


def delete_old_backups() -> Cleanup:
    """Keep the first backup and the last KEEP_BACKUPS backups.

    Some data is only in the backup. Before dj-librarian deletes a backup, the original grids go into the first
    backup, and the Bandcamp links go into source_urls.json. The oldest copy of a grid is the original. Thus
    dj-librarian stops at the first backup that it cannot read.
    """
    from dj_librarian.claude_genres import read_backup_urls, unread_backups

    found, first = backups(), first_backup()
    old = _old(found, first)
    if not old:
        return Cleanup()
    cleanup, ready = Cleanup(), []
    unread = unread_backups(old)
    try:
        grids = store.read_json(first / ANALYSIS_MANIFEST, {})
    except (OSError, ValueError) as error:
        return Cleanup(error=f"dj-librarian cannot read the first backup {first}: {error}")
    try:
        for path in _grid_sources(old, first, found):
            try:
                copies = store.read_json(path / ANALYSIS_MANIFEST, {})
            except (OSError, ValueError):
                cleanup.stopped_at = path
                break
            _keep_analysis(path, copies, first, grids)  # Also if the links stop the deletion here
            if path in unread and not read_backup_urls(path):
                cleanup.stopped_at = path
                break
            if path in old:
                ready.append(path)
        if grids:
            store.write_json(first / ANALYSIS_MANIFEST, grids)
    except OSError as error:  # For example, a full disk
        return Cleanup(error=f"dj-librarian cannot copy the grids into the first backup {first}: {error}")
    trash = store.BACKUP_DIR / ".deleting"  # If the deletion stops, no half backup stays with the backups
    try:
        if trash.exists():
            shutil.rmtree(trash)
        trash.mkdir()
        for path in ready:
            path.rename(trash / path.name)
            cleanup.deleted.append(path)
        shutil.rmtree(trash)
    except OSError as error:
        cleanup.error = f"dj-librarian cannot delete the old backups: {error}"
    return cleanup


def _grid_sources(old: list[Path], first: Path, found: list[Path]) -> list[Path]:
    """The backups that give their grids to the first backup, the oldest first.

    A backup that you gave another name gives its grids when all older backups gave theirs. A backup from
    before the first backup does not give its grids: they are older than the master.db of the first backup.
    """
    recent = [p for p in found if p not in old and p != first]
    limit = made(recent[0]) if recent else "~"
    start = made(first) or ""
    renamed = [p for p in kept_backups() if p != first and start < (made(p) or "~") < limit]
    return sorted(old + renamed, key=made)


def _keep_analysis(old: Path, copies: dict[str, str], first: Path, grids: dict[str, str]) -> None:
    """Copy the analysis files of the old backup into the first backup, if the first backup has no copy of them.

    The analysis files of the first write are the only copy of the original grids. A restore of the first
    backup then puts back all the original grids.
    """
    known = set(grids.values())
    for name, original in copies.items():
        if original not in known and (old / ANALYSIS_DIR / name).exists():
            (first / ANALYSIS_DIR).mkdir(exist_ok=True)
            new = f"{len(grids):04d}_{Path(original).name}"
            shutil.copy2(old / ANALYSIS_DIR / name, first / ANALYSIS_DIR / new)
            grids[new] = original
            known.add(original)


def backup_analysis(backup_dir: Path, paths: list[Path]) -> None:
    (backup_dir / ANALYSIS_DIR).mkdir()
    manifest = {}
    for n, path in enumerate(paths):
        name = f"{n:04d}_{path.name}"
        shutil.copy2(path, backup_dir / ANALYSIS_DIR / name)
        manifest[name] = str(path)
    store.write_json(backup_dir / ANALYSIS_MANIFEST, manifest)


def restore(backup_dir: Path, db_path: Path) -> Path:
    """Put the backup back. Return a new backup of the current state, to undo the restore.

    Your answers stay: they are still correct after the restore. The backup keeps a copy of them.
    """
    if not (backup_dir / "master.db").exists():
        raise SystemExit(f"no master.db in {backup_dir}")
    originals = store.read_json(backup_dir / ANALYSIS_MANIFEST, {}).values()
    current = backup(db_path, "restore")
    backup_analysis(current, [Path(p) for p in originals if Path(p).exists()])
    for name in DB_FILES:
        if name == PLAYLISTS_XML and not (backup_dir / name).exists():
            continue  # Keep the current file if the backup has none
        (db_path.parent / name).unlink(missing_ok=True)
        if (backup_dir / name).exists():
            shutil.copy2(backup_dir / name, db_path.parent / name)
    if (backup_dir / RECORDS_DIR).exists():  # Older backups have no records
        for path in _library_records():
            path.unlink(missing_ok=True)
            if (backup_dir / RECORDS_DIR / path.name).exists():
                shutil.copy2(backup_dir / RECORDS_DIR / path.name, path)
    for name, original in store.read_json(backup_dir / ANALYSIS_MANIFEST, {}).items():
        if Path(original).parent.exists():  # See grids_without_folder
            shutil.copy2(backup_dir / ANALYSIS_DIR / name, original)
    return current


def grids_without_folder(backup_dir: Path) -> list[str]:
    """The analysis files of the backup that restore cannot put back: rekordbox deleted the folder of the track."""
    return [p for p in store.read_json(backup_dir / ANALYSIS_MANIFEST, {}).values() if not Path(p).parent.exists()]


def changes(proposal: Proposal, parts: set[str]) -> dict:
    """Only the values that are different from the library."""
    track, result = proposal.track, {}
    if "genre" in parts and proposal.genre_write is not None and proposal.genre_write != (track.genre or ""):
        result["genre"] = proposal.genre_write
    if "comment" in parts and proposal.comment != (track.comment or ""):
        result["comment"] = proposal.comment
    if "cues" in parts and not _same_cues(proposal.cues.writes, proposal.cues.previous):
        result["cues"] = proposal.cues.writes  # Replaces the cues that an earlier run generated
    if "cues" in parts and proposal.cues.labels:
        result["labels"] = proposal.cues.labels
    return result


def _same_cues(a: dict, b: dict) -> bool:
    return a.keys() == b.keys() and all(a[s][0] == b[s][0] and abs(a[s][1] - b[s][1]) < 0.002 for s in a)


def apply(db: MasterDatabase, proposals: list[Proposal], parts: set[str], record: bool = True) -> Counter:
    """Use `record=False` for a test copy. The records of what dj-librarian wrote apply to the real library only."""
    stats = Counter()
    genres = {g.Name: g for g in db.get_genre().all()}
    generated = load_generated_cues()
    guessed = load_guessed_genres()
    for proposal in proposals:
        todo = changes(proposal, parts)
        if not todo:
            continue
        content = db.get_content(ID=proposal.track.id)
        if "genre" in todo:
            name = todo["genre"]
            if not name:
                content.GenreID = None
                guessed.pop(content.ID, None)
                stats["genre removed"] += 1
            else:
                if name not in genres:
                    genres[name] = db.add_genre(name)
                    stats["genres created"] += 1
                content.GenreID = genres[name].ID
                if proposal.genre_action == "fill":
                    guessed[content.ID] = name
                else:  # You approved this genre in review-genres
                    guessed.pop(content.ID, None)
                stats["genre"] += 1
        if "comment" in todo:
            content.Commnt = todo["comment"]
            stats["comment"] += 1
        if "cues" in todo:
            for cue in db.get_cue(ContentID=content.ID).all():
                if cue.UUID in generated:
                    db.delete(cue)
                    generated.discard(cue.UUID)
                    stats["generated cues replaced"] += 1
            db.flush()
        for slot, name in todo.get("labels", {}).items():
            for cue in db.get_cue(ContentID=content.ID, Kind=SLOT_TO_KIND[slot]).all():
                cue.Comment = name
                stats["cues labelled"] += 1
        for slot, (name, seconds) in todo.get("cues", {}).items():
            msec = round(seconds * 1000)
            uuid = str(uuid4())
            generated.add(uuid)
            db.add(models.DjmdCue.create(
                ID=str(db.generate_unused_id(models.DjmdCue, is_28_bit=False)),
                ContentID=content.ID, ContentUUID=content.UUID, UUID=uuid,
                Kind=SLOT_TO_KIND[slot], Comment=name, Color=-1,
                InMsec=msec, InFrame=msec * 150 // 1000, InMpegFrame=0, InMpegAbs=0,
                OutMsec=-1, OutFrame=0, OutMpegFrame=0, OutMpegAbs=0,
            ))
            stats["cues"] += 1
        stats["tracks"] += 1
    db.commit()
    if record:
        save_generated_cues(generated)
        save_guessed_genres(guessed)
    return stats
