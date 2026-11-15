import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
from pyrekordbox.masterdb import MasterDatabase, models
from sqlalchemy import MetaData, create_engine

from dj_librarian.cache import Cache
from dj_librarian.library import HOT_CUE_KINDS, AudioFile, Track
from dj_librarian.tagging import MOOD_TAGS
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


def analysis(embedding, **fields) -> dict:
    """An analysis result with the fields that proposals.build reads. Only the embedding is different for each track."""
    result = {
        "duration": 300.0, "audio_start": 0.5, "bpm": 120.0, "bpm_confidence": 3.0, "key": "A", "scale": "minor",
        "key_strength": 0.8, "genres": {"Electronic---Jungle": 0.5}, "moodtheme": dict.fromkeys(MOOD_TAGS, 0.1),
        "moods": {"aggressive": 0.5}, "danceability": 0.5, "valence": 5.0, "arousal": 5.0, "onset_rate": 4.0,
        "embedding": np.asarray(embedding, np.float32), "env_full": np.zeros(10), "env_low": np.zeros(10),
    }
    return result | fields


def add_analysed(db: MasterDatabase, analyses: Cache, id: str, embedding, genre: str | None = None,
                 title: str | None = None, **columns) -> models.DjmdContent:
    """`columns`: other values of the djmdContent row, for example BPM (x 100) or Length (seconds)."""
    found = genre and (db.get_genre(Name=genre).first() or db.add_genre(genre))
    content = add_content(db, id, genre=found or None)
    content.Title = title or f"Title {id}"
    content.ArtistID = (db.get_artist(Name="Artist").first() or db.add_artist("Artist")).ID
    for name, value in columns.items():
        setattr(content, name, value)
    db.commit()
    analyses.put(id, AudioFile(content.FolderPath, 1, 0.0), analysis(embedding))
    return content


SOUNDS = 3  # The axes of the sounds. Each track also has an axis of its own after these.


def sound(axis: int, n: int) -> np.ndarray:
    """The embedding of track `n`, with sound `axis`.

    The tracks with the same sound have a similarity of 0.86: below checks.DUPLICATE_SIMILARITY.
    Tracks with other sounds have a similarity of 0.
    """
    embedding = np.zeros(SOUNDS + 64)
    embedding[axis] = 1
    embedding[SOUNDS + n] = 0.4
    return embedding


def two_genres(db: MasterDatabase, analyses: Cache, size: int) -> None:
    """`size` tracks of Jungle (sound 0, tracks 0 to size - 1), then `size` tracks of Grime (sound 1)."""
    for n in range(size):
        add_analysed(db, analyses, f"j{n}", sound(0, n), "Jungle", f"Jungle {n}")
        add_analysed(db, analyses, f"g{n}", sound(1, size + n), "Grime", f"Grime {n}")


def add_grid(db: MasterDatabase, content: models.DjmdContent, beats: list[tuple[int, int, int]]) -> tuple[Path, Path]:
    """The DAT and EXT analysis files of the track, where pyrekordbox looks for them. `beats`: as in anlz.dat."""
    import anlz

    content.AnalysisDataPath = f"/PIONEER/USBANLZ/{content.ID}/ANLZ0000.DAT"
    db.commit()
    folder = db.get_anlz_dir(content)
    folder.mkdir(parents=True)
    dat, ext = folder / "ANLZ0000.DAT", folder / "ANLZ0000.EXT"
    dat.write_bytes(anlz.dat(beats))
    ext.write_bytes(anlz.ext(beats, [{"index": 1, "beat": 1, "kind": 1}], end_beat=len(beats) + 1))
    return dat, ext


def fill_and_review(db: MasterDatabase, analyses: Cache) -> None:
    """Grime and Jungle. "fill" has no genre and sounds like Jungle. "review" is Grime and sounds like Jungle."""
    two_genres(db, analyses, 6)
    add_analysed(db, analyses, "fill", sound(0, 12), title="No genre")
    add_analysed(db, analyses, "review", sound(0, 13), "Grime", "Odd one")


def library_copy(home: Path, folder: Path) -> Path:
    """A copy of the master.db of the `db` fixture, for the --db option of the commands."""
    copy = folder / "copy" / "master.db"
    copy.parent.mkdir()
    shutil.copy(home / "master.db", copy)
    return copy


@contextmanager
def opened(path: Path):
    db = MasterDatabase(path=path, unlock=False)
    try:
        yield db
    finally:
        db.close()
