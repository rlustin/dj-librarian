"""Merge the copies of the same track: keep one copy, put it in the playlists of the other copies.

dj-librarian does not remove the other copies from the collection. It puts them in one playlist,
and you remove them in rekordbox (Remove from Collection). Then rekordbox does its own cleanup.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from pyrekordbox.masterdb import models

from dj_librarian.checks import DUPLICATE_SIMILARITY
from dj_librarian.library import MasterDatabase, Track, find_files
from dj_librarian.tagging import similar_pairs

DUPLICATES_PLAYLIST = "dj-librarian duplicates to remove"
MAX_LENGTH_DIFFERENCE = 1  # seconds. A larger difference is usually an edit or an extended version.
# Words that make another version of a track. An instrumental can have the same length and almost the same sound.
VERSION_WORDS = {
    "instrumental", "inst", "acapella", "acappella", "dub", "remix", "rmx", "edit", "vip", "rework", "bootleg",
    "extended", "radio", "clean", "dirty", "live", "version", "reprise", "interlude",
}


@dataclass
class Copy:
    track: Track
    plays: int
    rating: int
    own_cues: int  # hot cues that you set, not dj-librarian
    genre: str | None  # your genre, if any
    release_folder: bool  # the folder name contains the album name
    depth: int
    created: str
    playlists: list[str] = field(default_factory=list)


@dataclass
class Group:
    keeper: Copy
    extras: list[Copy]
    reason: str
    genre: str | None = None  # your genre to give to the kept copy, if it has none
    conflict: str | None = None
    cues: list[str] = field(default_factory=list)  # UUIDs of your cues to move to the kept copy (same file only)


def missing_files(tracks: list[Track], steps=None) -> set[str]:
    """The IDs of the entries whose file is not on the disk."""
    found = find_files(tracks, steps)
    return {t.id for t in tracks if t.id not in found}


def find(
    db: MasterDatabase, tracks: list[Track], analysis: dict[str, dict], yours: dict[str, str], generated: set[str],
    missing: set[str],
) -> tuple[list[Group], int]:
    """The groups to merge, and the number of pairs that dj-librarian does not merge (other length or version).

    Entries with a missing file are not in these groups: `find_moved` merges them with the moved file.
    """
    by_id = {t.id: t for t in tracks if t.id in analysis and t.id not in missing}
    ids = list(by_id)
    embeddings = np.stack([analysis[i]["embedding"] for i in ids])
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
    content = {c.ID: c for c in db.get_content().all()}
    playlists = _playlists(db)

    parent = {i: i for i in ids}

    def root(i: str) -> str:
        while parent[i] != i:
            i = parent[i]
        return i

    other_version = 0
    for x, y, _ in similar_pairs(embeddings, DUPLICATE_SIMILARITY):
        a, b = ids[x], ids[y]
        if not same_track(by_id[a], by_id[b], content[a].Length, content[b].Length):
            other_version += 1
        else:
            parent[root(a)] = root(b)
    members: dict[str, list[str]] = {}
    for i in ids:
        members.setdefault(root(i), []).append(i)

    groups = []
    for group in members.values():
        if len(group) < 2:
            continue
        copies = [_copy(by_id[i], content[i], yours, generated, playlists.get(i, [])) for i in group]
        copies.sort(key=lambda c: c.created)  # If all else is equal, keep the oldest copy (stable sort)
        in_list = [c for c in copies if DUPLICATES_PLAYLIST in c.playlists]
        # A copy in the duplicates playlist stays an extra copy, also in a later run
        candidates = [c for c in copies if c not in in_list] or copies
        candidates.sort(key=_rank, reverse=True)
        keeper = candidates[0]
        extras = [c for c in copies if c is not keeper]
        groups.append(_group(keeper, extras, _reason(keeper, extras)))
    groups.sort(key=lambda g: (g.keeper.track.artist.lower(), g.keeper.track.title.lower()))
    return groups, other_version


def find_moved(
    db: MasterDatabase, tracks: list[Track], yours: dict[str, str], generated: set[str], missing: set[str]
) -> tuple[list[Group], list[Track]]:
    """Entries with a missing file, if another entry has the same file at a new place.

    rekordbox cannot relocate these entries, because the file is already in the collection.
    Also return the missing files without such an entry. rekordbox can relocate them.
    """
    found = {t.id: t.id not in missing for t in tracks}
    content = {c.ID: c for c in db.get_content().all()}
    by_place: dict[str, list[Track]] = {}
    by_name: dict[str, list[Track]] = {}
    for t in tracks:
        if found[t.id]:
            by_place.setdefault(_place(t.path), []).append(t)
            by_name.setdefault(_place(t.path, 1), []).append(t)
    moved: dict[str, list[Track]] = {}  # kept entry ID -> entries with a missing file
    alone = []
    for t in tracks:
        if found[t.id]:
            continue
        others = by_place.get(_place(t.path)) or [  # A copy in a folder for a set: same file name, same length
            o for o in by_name.get(_place(t.path, 1), [])
            if abs((content[o.id].Length or 0) - (content[t.id].Length or 0)) <= MAX_LENGTH_DIFFERENCE
        ]
        if len(others) == 1:
            moved.setdefault(others[0].id, []).append(t)
        else:
            alone.append(t)
    by_id = {t.id: t for t in tracks}
    playlists = _playlists(db)
    copy = lambda t: _copy(t, content[t.id], yours, generated, playlists.get(t.id, []))
    groups = []
    for keeper_id, olds in moved.items():
        group = _group(copy(by_id[keeper_id]), [copy(t) for t in olds], "the file of the other entry is missing")
        for old in olds:
            group.cues += _cues_to_move(db, old, by_id[keeper_id], generated, taken=group.cues)
        groups.append(group)
    groups.sort(key=lambda g: (g.keeper.track.artist.lower(), g.keeper.track.title.lower()))
    return groups, alone


def _place(path: str, parts: int = 2) -> str:
    """Folder and file name. A moved music folder keeps them."""
    return unicodedata.normalize("NFC", "/".join(path.split("/")[-parts:])).lower()


def _cues_to_move(db: MasterDatabase, old: Track, new: Track, generated: set[str], taken: list[str]) -> list[str]:
    """Your cues of the old entry, if no cue of yours is in the same hot cue slot of the new entry.

    `taken`: the cues of other old entries that also move to the new entry.
    """
    kinds = {c.Kind for c in db.get_cue(ContentID=new.id) if c.UUID not in generated}
    kinds |= {db.get_cue(UUID=uuid).first().Kind for uuid in taken}
    return [c.UUID for c in db.get_cue(ContentID=old.id)
            if c.UUID not in generated and (c.Kind == 0 or c.Kind not in kinds)]


def _playlists(db: MasterDatabase) -> dict[str, list[str]]:
    names = {p.ID: p.Name for p in db.get_playlist().all()}
    playlists: dict[str, list[str]] = {}
    for song in db.get_playlist_songs().all():
        playlists.setdefault(song.ContentID, []).append(names.get(song.PlaylistID, "?"))
    return playlists


def _group(keeper: Copy, extras: list[Copy], reason: str) -> Group:
    group = Group(keeper, extras, reason)
    genres = {c.genre for c in [keeper, *extras] if c.genre}
    if not keeper.genre and genres:
        group.genre = Counter(c.genre for c in extras if c.genre).most_common(1)[0][0]
    if len(genres) > 1:
        group.conflict = " / ".join(sorted(genres))
    return group


def same_track(a: Track, b: Track, length_a: int | None, length_b: int | None) -> bool:
    """For two tracks with the same sound: True for two copies, False for another version (edit, instrumental)."""
    return (abs((length_a or 0) - (length_b or 0)) <= MAX_LENGTH_DIFFERENCE
            and _version(a.title) == _version(b.title))


def _version(title: str) -> set[str]:
    return set(re.findall(r"[a-z]+", (title or "").lower())) & VERSION_WORDS


def _copy(t: Track, content, yours: dict[str, str], generated: set[str], playlists: list[str]) -> Copy:
    folders = t.path.split("/")[:-1]
    album = _plain(t.album or "")
    return Copy(
        track=t,
        plays=int(content.DJPlayCount or 0),
        rating=content.Rating or 0,
        own_cues=sum(uuid not in generated for uuid in t.cue_ids.values()),
        genre=yours.get(t.id),
        release_folder=bool(album) and album in _plain(folders[-1] if folders else ""),
        depth=len(folders),
        created=str(content.created_at or ""),
        playlists=playlists,
    )


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _rank(c: Copy) -> tuple:
    """Your use of the copy first. Then the copy in the release folder, not in a folder for a set."""
    return (c.own_cues, c.plays, c.rating, bool(c.genre), c.release_folder, -c.depth)


def _reason(keeper: Copy, extras: list[Copy]) -> str:
    best = max(extras, key=_rank)
    for label, value in (("more cues of yours", "own_cues"), ("more plays", "plays"), ("higher rating", "rating")):
        if getattr(keeper, value) > getattr(best, value):
            return label
    if keeper.genre and not best.genre:
        return "your genre"
    if keeper.release_folder and not best.release_folder:
        return "release folder"
    if keeper.depth < best.depth:
        return "shorter path"
    return "oldest copy"


def apply(db: MasterDatabase, groups: list[Group]) -> Counter:
    stats = Counter()
    target = db.get_playlist(Name=DUPLICATES_PLAYLIST).first()
    if target is None:
        target = db.create_playlist(DUPLICATES_PLAYLIST)
        db.flush()
    in_target = {song.ContentID for song in db.get_playlist_songs(PlaylistID=target.ID)}
    for group in groups:
        keeper = db.get_content(ID=group.keeper.track.id)
        for extra in group.extras:
            copy = db.get_content(ID=extra.track.id)
            for song in db.get_playlist_songs(ContentID=copy.ID).all():
                if song.PlaylistID == target.ID:
                    continue
                if db.get_playlist_songs(PlaylistID=song.PlaylistID, ContentID=keeper.ID).count():
                    _remove(db, song)
                    stats["playlist entries removed (the kept copy is already there)"] += 1
                else:
                    song.ContentID = keeper.ID
                    song.updated_at = datetime.now()
                    stats["playlist entries replaced"] += 1
            for song in db.get_history_songs(ContentID=copy.ID).all():
                song.ContentID = keeper.ID
                stats["history entries moved"] += 1
            keeper_tags = {t.MyTagID for t in db.query(models.DjmdSongMyTag).filter_by(ContentID=keeper.ID)}
            for tag in db.query(models.DjmdSongMyTag).filter_by(ContentID=copy.ID).all():
                if tag.MyTagID not in keeper_tags:
                    tag.ContentID = keeper.ID
                    stats["my tags moved"] += 1
            # Move the plays, so that a second run does not count them two times
            if int(copy.DJPlayCount or 0):
                keeper.DJPlayCount = str(int(keeper.DJPlayCount or 0) + int(copy.DJPlayCount))
                copy.DJPlayCount = "0"
            keeper.Rating = max(keeper.Rating or 0, copy.Rating or 0)
            for uuid in group.cues:
                cue = db.get_cue(UUID=uuid).first()
                if cue.ContentID == copy.ID:
                    _make_room(db, keeper.ID, cue.Kind)
                    cue.ContentID, cue.ContentUUID = keeper.ID, keeper.UUID
                    stats["cues moved"] += 1
            if copy.ID not in in_target:
                db.add_to_playlist(target, copy)
                in_target.add(copy.ID)
                stats["copies to remove"] += 1
        stats["groups"] += 1
    db.commit()
    return stats


def _make_room(db: MasterDatabase, content_id: str, kind: int) -> None:
    """Delete a cue that dj-librarian generated in this hot cue slot.

    `find_moved` does not move a cue onto a cue of yours.
    """
    if kind:
        for cue in db.get_cue(ContentID=content_id, Kind=kind).all():
            db.delete(cue)
        db.flush()


def _remove(db: MasterDatabase, song) -> None:
    """Like `remove_from_playlist` of pyrekordbox, without its commit: all changes go in one commit."""
    playlist, track_no = song.PlaylistID, song.TrackNo
    db.delete(song)
    db.flush()
    following = (
        db.query(models.DjmdSongPlaylist)
        .filter(models.DjmdSongPlaylist.PlaylistID == playlist, models.DjmdSongPlaylist.TrackNo > track_no)
        .order_by(models.DjmdSongPlaylist.TrackNo)
        .all()
    )
    with db.registry.disabled():
        for other in following:
            other.TrackNo -= 1
            other.updated_at = datetime.now()
    if following:
        db.registry.on_move(following)
