from collections import Counter

import pytest

from dj_librarian import checks, proposals, tagging
from dj_librarian.commands import review_genres
from dj_librarian.commands.review_genres import Question, Review, keep_answer, release, resolve_genre

from helpers import track


def _proposal(t):
    return proposals.Proposal(
        track=t, analysis={"duration": 300.0}, bpm_check="match", key_ours=None, key_check="match",
        genre_guess=tagging.GenreGuess(None, 0.0, "", [("Jungle", 0.6), ("Breaks", 0.4)]),
        genre_action="review", genre_write=None, genre_pending=True, claude=None, energy=5, moods=["Dark"],
        comment="", cues=tagging.CuePlan(positions={}, rekordbox_chorus=None),
    )


def _question(id, current=None, old_guess=False, outlier=False, **fields):
    t = track(id, current, **fields)
    found = checks.Outlier(t, current, Counter()) if outlier else None
    return Question(_proposal(t), "test", current, old_guess, found)


def test_release_needs_the_same_album_and_folder():
    a = track("a", album="EP", path="/music/EP/01.mp3")
    b = track("b", album="ep ", path="/music/EP/02.mp3")
    other_folder = track("c", album="EP", path="/music/set/03.mp3")

    assert release(a) == release(b)
    assert release(a) != release(other_folder)
    assert release(track("d", album="")) is None


@pytest.mark.parametrize(("question", "before", "answer"), [
    (_question("1", "Jungle"), {}, None),  # keep the genre in rekordbox
    (_question("1", "Jungle", old_guess=True), {}, "Jungle"),  # an old guess becomes your genre
    (_question("1", "Jungle", outlier=True), {}, None),
    (_question("1", "Jungle", outlier=True), {"1": "Jungle"}, "Jungle"),  # your earlier answer stays
])
def test_keep_answer(question, before, answer):
    assert keep_answer(question, before) == answer


@pytest.mark.parametrize(("text", "result"), [
    ("jungle", ("known", "Jungle")),
    ("jungel", ("close", "Jungle")),
    ("Drum and Bass", ("close", "Drum & Bass")),
    ("Gqom", ("new", "Gqom")),
])
def test_resolve_genre(text, result):
    names = {"jungle": "Jungle", "drum & bass": "Drum & Bass", "uk bass": "UK Bass"}

    assert resolve_genre(text, names) == result


class FakeKeys:
    def __init__(self, keys):
        self.keys = list(keys)

    def read(self, timeout=None):
        return self.keys.pop(0)


class FakePlayer:
    available, path = False, ""


def _review(questions, keys):
    proposals.save_genre_decisions({})
    review = Review(None, questions, {"jungle": "Jungle"}, {}, FakePlayer())
    review.keys = FakeKeys(keys)
    return review


@pytest.fixture(autouse=True)
def no_screen(monkeypatch):
    monkeypatch.setattr(review_genres, "_preview_start", lambda db, p: 0.0)
    monkeypatch.setattr(Review, "show", lambda self, *args: None)


def test_pick_back_and_change():
    questions = [_question("1", "Breaks"), _question("2", "Breaks")]
    review = _review(questions, ["1", "b", "k", "q"])

    assert review.ask(0) == "next"
    assert review.ask(1) == "back"
    assert review.ask(0) == "next"

    assert proposals.load_genre_decisions() == {"1": None}


def test_whole_release_gets_the_same_genre():
    ep = {"album": "EP", "path": "/music/EP/x.mp3"}
    questions = [_question("1", **ep), _question("2", **ep), _question("3", album="Other")]
    review = _review(questions, ["a", "2"])

    assert review.ask(0) == "next"

    assert proposals.load_genre_decisions() == {"1": "Breaks", "2": "Breaks"}
    assert set(review.answers) == {"1", "2"}


def test_an_outlier_answer_is_recorded_as_reviewed():
    review = _review([_question("1", "Jungle", outlier=True)], ["k"])

    review.ask(0)

    assert checks.load_outliers_reviewed()["1"] == "Jungle"
