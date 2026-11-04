"""Write proposals into a rekordbox master.db. Close rekordbox before you write."""

import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import uuid4

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


def ensure_rekordbox_closed() -> None:
    if get_rekordbox_pid():
        raise SystemExit("rekordbox is open. Close rekordbox first, because it can overwrite or corrupt the changes.")


def backup(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target, n = store.BACKUP_DIR / stamp, 1
    while target.exists():  # Two writes in the same second
        n += 1
        target = store.BACKUP_DIR / f"{stamp}_{n}"
    target.mkdir(parents=True)
    for name in DB_FILES:
        if (db_path.parent / name).exists():
            shutil.copy2(db_path.parent / name, target / name)
    return target


def backup_analysis(backup_dir: Path, paths: list[Path]) -> None:
    (backup_dir / ANALYSIS_DIR).mkdir()
    manifest = {}
    for n, path in enumerate(paths):
        name = f"{n:04d}_{path.name}"
        shutil.copy2(path, backup_dir / ANALYSIS_DIR / name)
        manifest[name] = str(path)
    store.write_json(backup_dir / ANALYSIS_MANIFEST, manifest)


def restore(backup_dir: Path, db_path: Path) -> Path:
    """Put the backup back. Return a new backup of the current state, to undo the restore."""
    if not (backup_dir / "master.db").exists():
        raise SystemExit(f"no master.db in {backup_dir}")
    originals = store.read_json(backup_dir / ANALYSIS_MANIFEST, {}).values()
    current = backup(db_path)
    backup_analysis(current, [Path(p) for p in originals if Path(p).exists()])
    for name in DB_FILES:
        if name == PLAYLISTS_XML and not (backup_dir / name).exists():
            continue  # Backups from before this file was in them
        (db_path.parent / name).unlink(missing_ok=True)
        if (backup_dir / name).exists():
            shutil.copy2(backup_dir / name, db_path.parent / name)
    if (backup_dir / ANALYSIS_MANIFEST).exists():
        for name, original in store.read_json(backup_dir / ANALYSIS_MANIFEST, {}).items():
            shutil.copy2(backup_dir / ANALYSIS_DIR / name, original)
    return current


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
