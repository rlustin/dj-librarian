"""Read-only access to the rekordbox library (master.db + ANLZ analysis files)."""

import logging
import os
import unicodedata
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np

warnings.filterwarnings("ignore", category=DeprecationWarning, module="pyrekordbox")
logging.getLogger("pyrekordbox").setLevel(logging.ERROR)

from pyrekordbox import AnlzFile, MasterDatabase
from pyrekordbox.masterdb import models

# djmdCue.Kind values of hot cues. 0 is a memory cue. Rekordbox does not use 4.
FEW_TRACKS = 500  # Up to this number of IDs, ask the database for these tracks only, not the whole library
HOT_CUE_KINDS = {1: "A", 2: "B", 3: "C", 5: "D", 6: "E", 7: "F", 8: "G", 9: "H"}

# Phrase kinds from the PSSI tag, per phrase mood (1 = high, 2 = mid, 3 = low).
PHRASE_KINDS = {
    1: {1: "Intro", 2: "Up", 3: "Down", 5: "Chorus", 6: "Outro"},
    2: {1: "Intro", 2: "Verse", 3: "Verse", 4: "Verse", 5: "Verse", 6: "Verse", 7: "Verse",
        8: "Bridge", 9: "Chorus", 10: "Outro"},
    3: {1: "Intro", 2: "Verse", 3: "Verse", 4: "Verse", 5: "Verse", 6: "Verse", 7: "Verse",
        8: "Bridge", 9: "Chorus", 10: "Outro"},
}


@dataclass
class Phrase:
    kind: str
    start: float  # seconds
    end: float


@dataclass
class Track:
    id: str
    title: str
    artist: str
    path: str
    bpm: float | None
    key: str | None  # As rekordbox stores it, for example "Am" or "8A"
    genre: str | None
    comment: str | None
    album: str = ""
    label: str = ""
    year: int | None = None
    hot_cues: dict[str, float] = field(default_factory=dict)  # slot letter -> seconds
    cue_names: dict[str, str] = field(default_factory=dict)  # slot letter -> hot cue label, when set
    cue_ids: dict[str, str] = field(default_factory=dict)  # slot letter -> cue UUID
    first_beat: float | None = None  # seconds
    downbeats: np.ndarray | None = None  # seconds, first beat of each bar
    beat_length: float | None = None  # seconds
    phrases: list[Phrase] = field(default_factory=list)


def open_db() -> MasterDatabase:
    return MasterDatabase()


def load_tracks(
    db: MasterDatabase,
    playlist: str | None = None,
    with_structure: bool = True,
    only: set[str] | None = None,
    steps=None,
) -> list[Track]:
    """`only` keeps only these content IDs. `steps` (progress.Steps) shows the progress of slow work."""
    few = only is not None and len(only) <= FEW_TRACKS
    if playlist:
        matches = [p for p in db.get_playlist().all() if p.Name == playlist and not p.is_folder]
        if not matches:
            raise SystemExit(f"playlist not found: {playlist}")
        by_id = {}
        for p in matches:
            songs = db.get_playlist_contents(p).all() if p.is_smart_playlist else [s.Content for s in p.Songs]
            for c in songs:
                by_id.setdefault(c.ID, c)  # A playlist can have the same track two times
        contents = list(by_id.values())
    elif few:
        contents = db.query(models.DjmdContent).filter(models.DjmdContent.ID.in_(only)).all()
    else:
        contents = db.get_content().all()

    cues_by_track: dict[str, dict[str, float]] = {}
    names_by_track: dict[str, dict[str, str]] = {}
    ids_by_track: dict[str, dict[str, str]] = {}
    cues = db.query(models.DjmdCue).filter(models.DjmdCue.ContentID.in_(only)) if few else db.get_cue()
    for cue in cues.all():
        if cue.Kind in HOT_CUE_KINDS:
            cues_by_track.setdefault(cue.ContentID, {})[HOT_CUE_KINDS[cue.Kind]] = cue.InMsec / 1000
            ids_by_track.setdefault(cue.ContentID, {})[HOT_CUE_KINDS[cue.Kind]] = cue.UUID
            if cue.Comment:
                names_by_track.setdefault(cue.ContentID, {})[HOT_CUE_KINDS[cue.Kind]] = cue.Comment

    tracks, selected = [], []
    for c in contents:
        if only is not None and c.ID not in only:
            continue
        selected.append(c)
        track = Track(
            id=c.ID,
            title=c.Title or "",
            artist=c.ArtistName or "",
            path=c.FolderPath,
            bpm=c.BPM / 100 if c.BPM else None,
            key=c.Key.ScaleName if c.Key else None,
            genre=c.Genre.Name if c.Genre else None,
            comment=c.Commnt or None,
            album=c.AlbumName or "",
            label=c.LabelName or "",
            year=c.ReleaseYear or None,
            hot_cues=cues_by_track.get(c.ID, {}),
            cue_names=names_by_track.get(c.ID, {}),
            cue_ids=ids_by_track.get(c.ID, {}),
        )
        tracks.append(track)
    if with_structure:
        _load_structures(db, [(c, t) for c, t in zip(selected, tracks)], steps)
    return tracks


def _load_structures(db: MasterDatabase, pairs: list, steps=None) -> None:
    """Beat grids and phrases. Use the cache if rekordbox did not change the analysis files."""
    from dj_librarian.cache import Cache

    cache = Cache()
    files = {}
    for content, track in pairs:
        paths = db.get_anlz_paths(content)
        dat, ext = (str(paths[k]) if paths.get(k) else None for k in ("DAT", "EXT"))
        files[track.id] = (dat, ext, f"{_mtime(dat)}:{_mtime(ext)}")
    cached = cache.structures()
    structures = {i: cached[i][1] for i, (_, _, key) in files.items() if i in cached and cached[i][0] == key}
    todo = [i for i in files if i not in structures]
    if todo:
        if steps:
            steps.step("Read rekordbox beat grids and phrases (one time, then from the cache)")
        fresh = {}
        if len(todo) < 50:
            fresh = {i: read_structure(*files[i][:2]) for i in todo}
        else:
            with ProcessPoolExecutor() as pool:
                futures = {pool.submit(read_structure, *files[i][:2]): i for i in todo}
                for n, future in enumerate(as_completed(futures), 1):
                    fresh[futures[future]] = future.result()
                    if steps:
                        steps.count(n, len(todo))
        cache.put_structures({i: (files[i][2], fresh[i]) for i in todo})
        structures |= fresh
    for _, track in pairs:
        if data := structures.get(track.id):
            track.first_beat = data["first_beat"]
            track.beat_length = data["beat_length"]
            track.downbeats = np.asarray(data["downbeats"])
            track.phrases = [Phrase(*phrase) for phrase in data["phrases"]]


def on_disk(path: str) -> str | None:
    """The path of the file as the disk spells it, or None if the file is missing.

    master.db keeps accents in NFC form. Some network shares keep them in NFD form and do not convert.
    """
    if os.path.exists(path):
        return path
    for form in ("NFD", "NFC"):
        other = unicodedata.normalize(form, path)
        if other != path and os.path.exists(other):
            return other
    return None


def _mtime(path: str | None) -> float | None:
    return os.path.getmtime(path) if path and os.path.exists(path) else None


def read_structure(dat: str | None, ext: str | None) -> dict | None:
    """Beat grid (DAT file) and phrases (EXT file) from rekordbox's own analysis."""
    beat_numbers = times = pssi = None
    try:
        if dat:
            beat_numbers, _, times = AnlzFile.parse_file(dat).get_tag("PQTZ").get()
        if ext:
            ext_file = AnlzFile.parse_file(ext)
            if "PSSI" in ext_file.tag_types:
                pssi = ext_file.get_tag("PSSI").content
    except Exception:  # noqa: BLE001 - an analysis file that pyrekordbox cannot read is like no file
        return None
    if times is None or len(times) < 2:
        return None
    times = np.asarray(times, dtype=float)
    phrases = []
    if pssi is not None:
        names = PHRASE_KINDS.get(int(pssi.mood), {})
        beat_time = lambda beat: float(times[min(max(beat - 1, 0), len(times) - 1)])
        entries = list(pssi.entries)
        for i, entry in enumerate(entries):
            end_beat = entries[i + 1].beat if i + 1 < len(entries) else pssi.end_beat
            phrases.append((names.get(int(entry.kind), "?"), beat_time(entry.beat), beat_time(end_beat)))
    return {
        "first_beat": float(times[0]),
        "beat_length": float(np.median(np.diff(times))),
        "downbeats": times[np.asarray(beat_numbers) == 1].tolist(),
        "phrases": phrases,
    }
