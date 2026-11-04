from types import SimpleNamespace

import pytest

from dj_librarian import dedupe

from helpers import track


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
