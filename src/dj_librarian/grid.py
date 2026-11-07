"""Change the beat grid of tracks: double it, or replace it with a grid that fits the audio.

dj-librarian changes the BPM in master.db and the beat grid in the analysis files (DAT and EXT).
The cues stay in place, because rekordbox stores them in milliseconds.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from construct import Container, ListContainer
from pyrekordbox import AnlzFile

from dj_librarian.library import MasterDatabase, Track

PHRASE_BEAT_FIELDS = ("beat", "beat_2", "beat_3", "beat_4", "beat_fill")


class GridError(Exception):
    pass


@dataclass
class Change:
    track: Track
    dat: Path
    ext: Path
    dat_data: bytes  # the new file contents
    ext_data: bytes
    bpm: int  # x 100, as in master.db


def _open(db: MasterDatabase, track: Track):
    content = db.get_content(ID=track.id)
    paths = db.get_anlz_paths(content)
    if not paths.get("DAT") or not paths.get("EXT"):
        raise GridError("no analysis files")
    dat, ext = Path(paths["DAT"]), Path(paths["EXT"])
    return content, dat, ext, _parse_exact(dat), _parse_exact(ext)


def prepare_double(db: MasterDatabase, track: Track) -> Change:
    """Make the new analysis files in memory. Raise GridError if the track is not safe to change."""
    content, dat, ext, dat_file, ext_file = _open(db, track)
    entries = dat_file.get_tag("PQTZ").content.entries
    if len(entries) < 2:
        raise GridError("no beat grid")
    if len({e.tempo for e in entries}) > 1:
        raise GridError("the beat grid has more than one tempo")
    if entries[0].tempo != int(content.BPM or 0):
        raise GridError(f"the BPM in master.db ({(content.BPM or 0) / 100}) is not the BPM of the beat grid")
    _double_grid(dat_file, (content.Length or 0) * 1000)
    if "PQT2" in ext_file.tag_types:
        _clear_extended_grid(ext_file)
    if "PSSI" in ext_file.tag_types:
        _double_phrases(ext_file)
    return Change(track, dat, ext, dat_file.build(), ext_file.build(), 2 * int(content.BPM))


def prepare_retime(db: MasterDatabase, track: Track, bpm: float, beats: list[tuple[int, float]]) -> Change:
    """Replace the grid with `beats`: (beat number in the bar, time in seconds). The phrases keep their times."""
    _, dat, ext, dat_file, ext_file = _open(db, track)
    tag = dat_file.get_tag("PQTZ")
    old_times = [e.time / 1000 for e in tag.content.entries]
    if len(old_times) < 2 or len(beats) < 2:
        raise GridError("no beat grid")
    tempo = round(bpm * 100)
    tag.content.entries = ListContainer(Container(beat=n, tempo=tempo, time=round(t * 1000)) for n, t in beats)
    tag.content.entry_count = len(beats)
    if "PQT2" in ext_file.tag_types:
        _clear_extended_grid(ext_file)
    if "PSSI" in ext_file.tag_types:
        _move_phrases(ext_file, old_times, [t for _, t in beats])
    return Change(track, dat, ext, dat_file.build(), ext_file.build(), tempo)


def _parse_exact(path: Path) -> AnlzFile:
    """Only change files that pyrekordbox writes back byte for byte. Then only the grid changes."""
    data = path.read_bytes()
    parsed = AnlzFile.parse(data)
    if parsed.build() != data:
        raise GridError(f"dj-librarian cannot write {path.name} back without other changes")
    return parsed


def _double_grid(dat_file: AnlzFile, end_ms: int) -> None:
    """Add a beat between each two beats. Old beat 1 and 3 become new downbeats."""
    tag = dat_file.get_tag("PQTZ")
    old = tag.content.entries
    new = []
    for i, entry in enumerate(old):
        # Old beats 1, 2, 3, 4 are new beats 1, 3, 1, 3. The added beats are 2 and 4.
        number = (2 * (entry.beat - 1)) % 4 + 1
        new.append(Container(beat=number, tempo=2 * entry.tempo, time=entry.time))
        following = old[i + 1].time if i + 1 < len(old) else 2 * entry.time - old[i - 1].time
        middle = round((entry.time + following) / 2)
        if i + 1 < len(old) or not end_ms or middle < end_ms:
            new.append(Container(beat=number + 1, tempo=2 * entry.tempo, time=middle))
    tag.content.entries = ListContainer(new)
    tag.content.entry_count = len(new)


def _clear_extended_grid(ext_file: AnlzFile) -> None:
    """The format of the per-beat data is not known. An empty extended grid is a state that rekordbox also writes."""
    tag = ext_file.get_tag("PQT2")
    tag.content.bpm = ListContainer([Container(beat=0, tempo=0, time=0) for _ in range(2)])
    tag.content.entry_count = 0
    tag.content.entries = ListContainer()
    tag.content.u3 = 0
    tag.struct.len_tag = tag.struct.len_header


def _double_phrases(ext_file: AnlzFile) -> None:
    """Old beat n is at the same time as new beat 2n - 1."""
    content = ext_file.get_tag("PSSI").content
    for entry in content.entries:
        for name in PHRASE_BEAT_FIELDS:
            if entry[name]:
                entry[name] = 2 * entry[name] - 1
    content.end_beat = 2 * content.end_beat - 1
    if content.end_beat > 0xFFFF:
        raise GridError("the track is too long for the phrase format")


def _move_phrases(ext_file: AnlzFile, old_times: list[float], new_times: list[float]) -> None:
    """Each phrase starts at the new beat nearest to the time of its old beat."""
    new = np.asarray(new_times)

    def moved(beat: int) -> int:
        time = old_times[min(max(beat, 1), len(old_times)) - 1]
        return int(np.argmin(np.abs(new - time))) + 1

    content = ext_file.get_tag("PSSI").content
    for entry in content.entries:
        for name in PHRASE_BEAT_FIELDS:
            if entry[name]:
                entry[name] = moved(entry[name])
    content.end_beat = moved(content.end_beat)


def apply(db: MasterDatabase, changes: list[Change]) -> int:
    """Write the analysis files first, then commit master.db one time. Back up all files before."""
    for change in changes:
        for path, data in ((change.dat, change.dat_data), (change.ext, change.ext_data)):
            temporary = path.with_name(path.name + ".dj-librarian")
            temporary.write_bytes(data)
            temporary.replace(path)
        db.get_content(ID=change.track.id).BPM = change.bpm
    db.commit()
    return len(changes)
