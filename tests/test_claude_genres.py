import json
import os
import shlex
import shutil
from pathlib import Path

import pytest

from dj_librarian import claude_genres, store, tagging
from dj_librarian.claude_genres import PROMPT_VERSION, Peers
from dj_librarian.proposals import Proposal

from helpers import add_content, track


@pytest.mark.parametrize(("answer", "tags", "stale"), [
    ({"version": PROMPT_VERSION, "tags": 100}, 110, False),
    ({"version": PROMPT_VERSION, "tags": 100}, 111, True),  # You tagged more than 10% more tracks
    ({"version": PROMPT_VERSION - 1, "tags": 100}, 100, True),  # The prompt changed
    ({}, 0, True),
])
def test_is_stale(answer, tags, stale):
    assert claude_genres.is_stale(answer, tags) == stale


def test_vocabulary_has_the_most_used_genres_first():
    assert claude_genres.vocabulary({"1": "Grime", "2": "Jungle", "3": "Jungle"}) == [("Jungle", 2), ("Grime", 1)]


def test_peers_are_the_genres_of_the_same_release_artist_and_label():
    tracks = [
        track("1", album="EP", artist="MC", label="Rinse"),
        track("2", album="ep ", artist="MC", label="Rinse"),
        track("3", album="LP", artist="mc", label="Other"),
        track("4", album="EP", artist="Someone", label="Rinse"),
    ]
    peers = Peers(tracks, {"1": "Grime", "2": "Grime", "3": "Jungle"})

    # The genre of the track itself does not count
    assert peers.describe(tracks[0]) == ('release "EP": Grime (1); artist "MC": Grime (1), Jungle (1); '
                                         'label "Rinse": Grime (1)')
    assert peers.describe(tracks[3]) == 'release "EP": Grime (2); label "Rinse": Grime (2)'
    assert peers.describe(track("5", album="", artist="", label="")) == ""


def _proposal(t):
    guess = tagging.GenreGuess(None, 0.3, "Grime", neighbours=[("2", "Grime", 0.91), ("gone", "Jungle", 0.8)])
    return Proposal(
        track=t, analysis={"genres": {"Electronic---Grime": 0.62, "Electronic---Dubstep": 0.2}}, bpm_check="match",
        key_ours=None, key_check="match", genre_guess=guess, genre_action="suggest", genre_write=None,
        genre_pending=True, claude=None, energy=7, moods=["Dark", "Heavy"], comment="", cues=None,
    )


def test_describe_gives_claude_what_dj_librarian_knows_about_the_track():
    t = track("1", artist="MC", title="Bars", album="EP", path="/music/Grime/01.mp3", bpm=140.0, key="Am",
              comment="E7 Dark/Heavy | from the radio show")
    other = track("2", artist="Crew", title="Riddim")
    peers = Peers([t, other], {"2": "Grime"})

    lines = claude_genres.describe(_proposal(t), {"1": t, "2": other}, "https://a.bandcamp.com/album/ep", peers,
                                   current="Jungle").splitlines()

    assert lines == [
        "id: 1",
        "artist: MC | title: Bars | album: EP | label: - | year: - | folder: Grime",
        "bpm: 140.0 | key: 8A | energy: 7/10 | moods: Dark/Heavy",
        "audio style model (Discogs styles, 0-1): Grime 0.62, Dubstep 0.20",
        'most similar tracks the DJ tagged: "Crew - Riddim" (Grime, similarity 0.91)',
        "the DJ's genres for other tracks of the same release, artist or label: -",
        f"current genre: Jungle. None of the {tagging.KNN_K} most similar tracks that the DJ tagged has this genre.",
        "DJ's note: from the radio show",
        "release page: https://a.bandcamp.com/album/ep",
    ]


def test_build_prompt_lists_the_genres_then_the_tracks():
    prompt = claude_genres.build_prompt(["id: 1", "id: 2"], [("Jungle", 2), ("Grime", 1)])

    assert prompt == ("The DJ's genre names, with how many tracks use each: Jungle (2), Grime (1)\n\n"
                      "Tracks:\n\nid: 1\n\nid: 2")


def _backup(home, name: str) -> Path:
    backup = store.BACKUP_DIR / name
    backup.mkdir(parents=True)
    shutil.copy2(home / "master.db", backup / "master.db")
    return backup


def test_source_urls_prefer_the_comments_of_now_and_name_the_damaged_backups(unencrypted, home):
    content = add_content(unencrypted, "4")
    content.Commnt = "https://four.bandcamp.com/track/a"
    unencrypted.commit()
    damaged = _backup(home, "2026-11-01_120000")
    (damaged / "master.db").write_bytes(b"not a database")
    _backup(home, "2026-11-02_120000")
    store.write_json(store.SOURCE_URLS, {"backups": [], "urls": {"1": "https://old.bandcamp.com",
                                                                 "2": "https://two.bandcamp.com"}})
    tracks = [track("1", comment="Get it: https://new.bandcamp.com/album/x"), track("3", comment="no link")]

    urls, unreadable = claude_genres.source_urls(tracks)

    assert urls == {"1": "https://new.bandcamp.com/album/x", "2": "https://two.bandcamp.com",
                    "4": "https://four.bandcamp.com/track/a"}
    assert unreadable == [damaged]


def test_dj_librarian_reads_the_links_of_an_old_backup_one_time(unencrypted, home):
    content = add_content(unencrypted, "1")
    content.Commnt = "https://artist.bandcamp.com/track/a"
    unencrypted.commit()
    backup = _backup(home, "2026-11-01_120000")

    assert claude_genres.read_backup_urls(backup)

    assert store.read_json(store.SOURCE_URLS, None) == {
        "backups": [backup.name], "urls": {"1": "https://artist.bandcamp.com/track/a"}}
    assert claude_genres.unread_backups([backup]) == []


def _fake_claude(tmp_path, monkeypatch, events: list, exit_code: int = 0, stderr: str = "") -> None:
    """A `claude` command on PATH that writes these events and keeps its arguments in args.txt."""
    (tmp_path / "events.jsonl").write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events))
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir()
    args, events_file = shlex.quote(str(tmp_path / "args.txt")), shlex.quote(str(tmp_path / "events.jsonl"))
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {args}\ncat {events_file}\n"
                      f"echo {shlex.quote(stderr)} >&2\nexit {exit_code}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")


ANSWER = {"id": "1", "genre": "Grime", "is_new_genre": False, "confidence": "high", "reason": "MC over 140.",
          "alternatives": []}


def test_ask_reports_the_steps_and_returns_the_answers(tmp_path, monkeypatch):
    content = [
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "MC Bars EP"}},
        {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://a.bandcamp.com/album/ep"}},
        {"type": "tool_use", "name": "StructuredOutput", "input": {}},
    ]
    _fake_claude(tmp_path, monkeypatch, [
        "not json",
        {"type": "system"},
        {"type": "assistant", "message": {"content": content}},
        {"type": "result", "is_error": False, "structured_output": {"tracks": [ANSWER]}},
    ])
    steps = []

    assert claude_genres.ask("the prompt", on_activity=steps.append) == [ANSWER]

    assert steps == ["thinks", 'searches "MC Bars EP"', "reads a.bandcamp.com/album/ep", "writes the answers"]
    args = (tmp_path / "args.txt").read_text().splitlines()
    assert args[args.index("--allowedTools") + 1] == "WebSearch,WebFetch"


def test_ask_without_web_gives_claude_no_tools(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, [{"type": "result", "structured_output": {"tracks": [ANSWER]}}])

    claude_genres.ask("the prompt", web=False)

    args = (tmp_path / "args.txt").read_text().splitlines()
    assert args[args.index("--tools") + 1] == "" and "--allowedTools" not in args


def test_ask_stops_at_an_error_of_claude(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, [{"type": "result", "is_error": True, "result": "usage limit"}])

    with pytest.raises(RuntimeError, match="claude failed: usage limit"):
        claude_genres.ask("the prompt")


def test_ask_stops_if_claude_gives_no_answer(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, [], exit_code=3, stderr="not logged in")

    with pytest.raises(RuntimeError, match=r"claude stopped and gave no answer \(exit 3\): not logged in"):
        claude_genres.ask("the prompt")


def test_ask_needs_the_claude_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(RuntimeError, match="the `claude` CLI is not installed"):
        claude_genres.ask("the prompt")
