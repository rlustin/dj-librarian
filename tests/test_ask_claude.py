import re
from argparse import Namespace

import pytest

from dj_librarian import claude_genres
from dj_librarian.commands import ask_claude

from helpers import add_analysed, sound, two_genres


@pytest.fixture
def library(db, analysed):
    """Grime and Jungle, a Grime track that sounds like Jungle ("odd"), and a track without a genre ("mystery").

    The nearest tracks of "mystery" have four different genres: dj-librarian has no guess.
    """
    two_genres(db, analysed, 10)
    add_analysed(db, analysed, "odd", sound(0, 20), "Grime", "Odd one")
    for n, genre in enumerate(["Dub", "Dubstep", "Ambient", "Bass"]):
        add_analysed(db, analysed, f"s{n}", sound(2, 21 + n), genre, genre)
    add_analysed(db, analysed, "mystery", sound(2, 30), title="Mystery")
    return db


class FakeClaude:
    """Answer "Gqom" for "mystery" and "Jungle" for the other tracks. Fail for the tracks in `fail`."""

    def __init__(self, fail=()):
        self.prompts, self.web, self.fail = [], [], set(fail)

    def __call__(self, prompt, model, web, on_activity):
        self.prompts.append(prompt)
        self.web.append(web)
        ids = re.findall(r"^id: (\S+)$", prompt, re.MULTILINE)
        if self.fail & set(ids):
            raise RuntimeError("claude failed: usage limit")
        on_activity("thinks")
        return [{"id": i, "genre": "Gqom" if i == "mystery" else "Jungle", "is_new_genre": i == "mystery",
                 "confidence": "medium", "reason": "Reason.", "alternatives": []} for i in ids]


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(claude_genres, "ask", fake)
    return fake


def _args(**fields) -> Namespace:
    defaults = {"playlist": None, "limit": None, "model": "opus", "no_web": False, "refresh": False, "workers": 2,
                "self_check": None}
    return Namespace(**(defaults | fields))


def test_ask_claude_asks_about_the_tracks_without_a_guess_and_the_outliers(library, claude, capsys):
    ask_claude.run(_args())

    answers = claude_genres.load_answers()
    assert set(answers) == {"mystery", "odd"}
    assert answers["mystery"].items() >= {"genre": "Gqom", "model": "opus", "outlier": False,
                                          "version": claude_genres.PROMPT_VERSION, "tags": 25}.items()
    assert answers["odd"]["outlier"]
    assert "current genre: Grime." in claude.prompts[0]
    assert claude.web == [True]
    output = capsys.readouterr().out
    assert "1 track without a genre, 1 track that does not sound like your genre" in output
    assert "2 tracks in 1 call to Claude (opus, web search on)" in output
    assert "dj-librarian saved 2 answers." in output
    assert "new genres proposed: Gqom (1)" in output


def test_ask_claude_does_not_ask_again_until_the_answer_is_old(library, claude, monkeypatch, capsys):
    ask_claude.run(_args())
    capsys.readouterr()

    ask_claude.run(_args(refresh=True))
    assert "0 tracks in 0 calls" in capsys.readouterr().out

    monkeypatch.setattr(claude_genres, "PROMPT_VERSION", claude_genres.PROMPT_VERSION + 1)
    ask_claude.run(_args())
    assert "0 tracks in 0 calls" in capsys.readouterr().out  # Without --refresh, an answer stays
    ask_claude.run(_args(refresh=True))
    assert "2 tracks in 1 call" in capsys.readouterr().out


def test_a_failed_call_does_not_stop_the_other_calls(library, monkeypatch, capsys):
    monkeypatch.setattr(claude_genres, "ask", FakeClaude(fail={"odd"}))
    monkeypatch.setattr(claude_genres, "BATCH_SIZE", 1)

    ask_claude.run(_args())

    assert set(claude_genres.load_answers()) == {"mystery"}
    output = capsys.readouterr().out
    assert "claude failed: usage limit" in output and "dj-librarian saved 1 answer." in output


def test_ask_claude_with_a_limit_and_without_web_search(library, claude, capsys):
    ask_claude.run(_args(limit=1, no_web=True))

    assert len(claude_genres.load_answers()) == 1
    assert claude.web == [False]
    assert "1 track in 1 call to Claude (opus, web search off)" in capsys.readouterr().out


def test_self_check_compares_the_answers_with_your_genres_and_saves_nothing(library, claude, capsys):
    ask_claude.run(_args(self_check=100))

    assert claude_genres.load_answers() == {}
    output = capsys.readouterr().out
    assert "Claude matched your genre on 10/25 tracks (40%)" in output
    assert "  medium confidence: 10/25 right" in output
    assert "Artist - Odd one: Grime -> Jungle (medium): Reason." in output
