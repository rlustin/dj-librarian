from datetime import datetime
from pathlib import Path

from pyrekordbox.masterdb import MasterDatabase, models
from sqlalchemy import MetaData, create_engine

from dj_librarian.library import HOT_CUE_KINDS, Track
from dj_librarian.writer import SLOT_TO_KIND


def track(id: str = "1", genre: str | None = None, title: str = "Title", **fields) -> Track:
    defaults = {"artist": "Artist", "path": f"/music/{id}.mp3", "bpm": 120.0, "key": None, "comment": None}
    return Track(id=id, title=title, genre=genre, **(defaults | fields))


def empty_library(folder: Path) -> MasterDatabase:
    """A master.db with the tables of rekordbox and no tracks. It has no encryption, so it needs no rekordbox."""
    metadata = MetaData()
    for table in models.Base.metadata.tables.values():
        for column in table.to_metadata(metadata).columns:
            column.nullable = not column.primary_key  # rekordbox leaves empty many columns that pyrekordbox requires
    path = folder / "master.db"
    metadata.create_all(create_engine(f"sqlite:///{path}"))
    db = MasterDatabase(path=path, unlock=False)
    now = datetime.now()
    db.add(models.AgentRegistry(registry_id="localUpdateCount", int_1=0, date_1=now, date_2=now))
    db.commit()
    return db


def add_content(db: MasterDatabase, id: str, plays: int = 0, rating: int = 0, genre=None) -> models.DjmdContent:
    content = models.DjmdContent.create(ID=id, UUID=f"uuid-{id}", Title=f"Title {id}", FolderPath=f"/music/{id}.mp3",
                                        DJPlayCount=str(plays), Rating=rating, GenreID=genre.ID if genre else None)
    db.add(content)
    db.flush()
    return content


def add_cue(db: MasterDatabase, content: models.DjmdContent, slot: str, seconds: float, uuid: str,
            name: str = "") -> None:
    msec = round(seconds * 1000)
    db.add(models.DjmdCue.create(
        ID=uuid, ContentID=content.ID, ContentUUID=content.UUID, UUID=uuid, Kind=SLOT_TO_KIND[slot], Comment=name,
        Color=-1, InMsec=msec, InFrame=msec * 150 // 1000, InMpegFrame=0, InMpegAbs=0,
        OutMsec=-1, OutFrame=0, OutMpegFrame=0, OutMpegAbs=0,
    ))
    db.flush()


def cues(db: MasterDatabase, content_id: str) -> dict[str, tuple[str, str, int]]:
    """Hot cue slot -> (UUID, name, milliseconds)."""
    found = db.get_cue(ContentID=content_id).all()
    result = {HOT_CUE_KINDS[c.Kind]: (c.UUID, c.Comment, c.InMsec) for c in found}
    assert len(result) == len(found), "two cues in one slot"
    return result
