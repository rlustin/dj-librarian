from types import SimpleNamespace

import pytest
from pyrekordbox.masterdb import models

from dj_librarian import dedupe

from helpers import add_content, add_cue, cues, track


@pytest.mark.parametrize(("title_a", "title_b", "length_b", "same"), [
    ("Pressure", "Pressure", 300, True),
    ("Pressure", "Pressure", 301, True),
    ("Pressure", "Pressure", 305, False),  # an edit
    ("Pressure", "Pressure (Instrumental)", 300, False),
    ("Pressure (Extended Mix)", "Pressure", 300, False),
    ("Pressure (Original Mix)", "Pressure", 300, True),
])
def test_same_track(title_a, title_b, length_b, same):
    assert dedupe.same_track(track("a", title=title_a), track("b", title=title_b), 300, length_b) is same


def _copy(id, **fields):
    defaults = {"plays": 0, "rating": 0, "own_cues": 0, "genre": None, "release_folder": False, "depth": 5,
                "created": "2024"}
    return dedupe.Copy(track=track(id), **(defaults | fields))


def test_group_gives_your_genre_to_the_kept_copy():
    group = dedupe._group(_copy("keep"), [_copy("extra", genre="Jungle")], "reason")

    assert group.genre == "Jungle"
    assert group.conflict is None


def test_group_shows_two_different_genres():
    group = dedupe._group(_copy("keep", genre="Jungle"), [_copy("extra", genre="Drum & Bass")], "reason")

    assert group.genre is None
    assert group.conflict == "Drum & Bass / Jungle"


@pytest.mark.parametrize(("keeper", "reason"), [
    ({"own_cues": 2}, "more cues of yours"),
    ({"plays": 3}, "more plays"),
    ({"genre": "Jungle"}, "your genre"),
    ({"release_folder": True}, "release folder"),
    ({"depth": 3}, "shorter path"),
    ({}, "oldest copy"),
])
def test_reason(keeper, reason):
    assert dedupe._reason(_copy("keep", **keeper), [_copy("extra")]) == reason


class FakeDb:
    def __init__(self, contents):
        self.contents = contents

    def get_content(self):
        return SimpleNamespace(all=lambda: self.contents)

    def get_playlist(self):
        return SimpleNamespace(all=list)

    def get_playlist_songs(self):
        return SimpleNamespace(all=list)

    def get_cue(self, **_):
        return []


def test_a_moved_file_is_merged_one_time():
    # The old entry has your plays, the new entry has the file. The cache keeps the analysis of both.
    old, new = track("old", path="/old/Artist/a.mp3"), track("new", path="/new/Artist/a.mp3")
    db = FakeDb([SimpleNamespace(ID=t.id, Length=300, DJPlayCount=plays, Rating=0, created_at="2024")
                 for t, plays in ((old, 9), (new, 0))])
    analysis = {"old": {"embedding": [1.0, 0.0]}, "new": {"embedding": [1.0, 0.0]}}

    copies, _ = dedupe.find(db, [old, new], analysis, {}, set(), missing={"old"})
    moved, _ = dedupe.find_moved(db, [old, new], {}, set(), missing={"old"})

    assert copies == []
    assert [(g.keeper.track.id, [c.track.id for c in g.extras]) for g in moved] == [("new", ["old"])]


def _playlist(db, name, *contents):
    playlist = db.create_playlist(name)
    for content in contents:
        db.add_to_playlist(playlist, content)
    return playlist


def _tracks(db, name):
    playlist = db.get_playlist(Name=name).one()
    return [s.ContentID for s in sorted(db.get_playlist_songs(PlaylistID=playlist.ID), key=lambda s: s.TrackNo)]


def _tag(db, tag_id, content):
    db.add(models.DjmdSongMyTag.create(ID=f"{tag_id}-{content.ID}", MyTagID=tag_id, ContentID=content.ID))
    db.flush()


def _tags(db, content_id):
    return sorted(t.MyTagID for t in db.query(models.DjmdSongMyTag).filter_by(ContentID=content_id))


def test_apply_moves_the_playlists_cues_and_plays_to_the_kept_copy(db):
    kept, extra = add_content(db, "kept", plays=2), add_content(db, "extra", plays=3, rating=5)
    add_cue(db, kept, "A", 0.5, "generated")
    add_cue(db, extra, "A", 1.0, "yours")
    _playlist(db, "only the extra", extra)
    both = _playlist(db, "both", extra, kept)
    db.add(models.DjmdHistory.create(ID="h", Name="2026-11-07"))
    db.add(models.DjmdSongHistory.create(ID="hs", HistoryID="h", ContentID="extra", TrackNo=1))
    for tag in ("dark", "vocal"):
        db.add(models.DjmdMyTag.create(ID=tag, Name=tag))
    _tag(db, "dark", kept)
    _tag(db, "dark", extra)
    _tag(db, "vocal", extra)
    group = dedupe._group(_copy("kept"), [_copy("extra")], "reason")
    group.cues = ["yours"]

    dedupe.apply(db, [group])
    dedupe.apply(db, [group])  # A second run changes nothing

    assert _tracks(db, "only the extra") == ["kept"]
    assert _tracks(db, "both") == ["kept"]
    assert [s.TrackNo for s in db.get_playlist_songs(PlaylistID=both.ID)] == [1]  # No gap in the order
    assert _tracks(db, dedupe.DUPLICATES_PLAYLIST) == ["extra"]
    assert cues(db, "kept") == {"A": ("yours", "", 1000)} and cues(db, "extra") == {}
    assert (db.get_content(ID="kept").DJPlayCount, db.get_content(ID="extra").DJPlayCount) == ("5", "0")
    assert db.get_content(ID="kept").Rating == 5
    assert [s.ContentID for s in db.get_history_songs(HistoryID="h")] == ["kept"]
    assert _tags(db, "kept") == ["dark", "vocal"]  # A tag that the kept copy has already does not move
