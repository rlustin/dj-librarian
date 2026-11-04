"""What dj-librarian would change for each track. Shared by `report`, `review-genres` and `apply`."""

import json
from dataclasses import dataclass
from pathlib import Path

from dj_librarian import library, tagging
from dj_librarian.cache import Cache
from dj_librarian.progress import Steps

GENRE_DECISIONS_PATH = Path.home() / ".cache" / "dj-librarian" / "genre_decisions.json"
GENERATED_CUES_PATH = Path.home() / ".cache" / "dj-librarian" / "generated_cues.json"
GUESSED_GENRES_PATH = Path.home() / ".cache" / "dj-librarian" / "generated_genres.json"


@dataclass
class Proposal:
    track: library.Track
    analysis: dict
    bpm_check: str
    key_ours: str | None
    key_check: str
    genre_guess: tagging.GenreGuess
    genre_action: str  # fill | review | suggest | approved | keep | unknown
    genre_write: str | None  # genre that `apply` will write, if any
    genre_pending: bool  # Review or suggestion without your answer
    claude: dict | None  # Claude's answer from `ask-claude`, if any
    energy: int
    moods: list[str]
    comment: str
    cues: tagging.CuePlan


@dataclass
class Context:
    analysed: int
    total: int
    tracks: list[library.Track]
    genres: dict[str, tagging.GenreGuess]


def load_genre_decisions() -> dict[str, str | None]:
    """content ID -> your genre. None: keep the genre in rekordbox. "": remove the genre."""
    """content ID -> approved genre, or None if you keep the current genre."""
    if not GENRE_DECISIONS_PATH.exists():
        return {}
    return json.loads(GENRE_DECISIONS_PATH.read_text())


def save_genre_decisions(decisions: dict[str, str | None]) -> None:
    GENRE_DECISIONS_PATH.write_text(json.dumps(decisions, indent=1, ensure_ascii=False))


def save_genre_decision(track_id: str, genre: str | None) -> None:
    """Read the file again before the write. Then two sessions do not erase the answers of each other."""
    save_genre_decisions(load_genre_decisions() | {track_id: genre})


def load_generated_cues(db) -> set[str]:
    """UUIDs of the hot cues that dj-librarian created. A later run can move or replace only these cues."""
    if not GENERATED_CUES_PATH.exists():
        # Earlier versions recognised generated cues by their label
        ids = {c.UUID for c in db.get_cue().all() if c.Comment in tagging.CUE_NAMES}
        save_generated_cues(ids)
        return ids
    return set(json.loads(GENERATED_CUES_PATH.read_text()))


def save_generated_cues(ids: set[str]) -> None:
    GENERATED_CUES_PATH.write_text(json.dumps(sorted(ids), indent=1))


def load_guessed_genres() -> dict[str, str]:
    """content ID -> genre that dj-librarian wrote from its own guess, without your review."""
    return json.loads(GUESSED_GENRES_PATH.read_text()) if GUESSED_GENRES_PATH.exists() else {}


def save_guessed_genres(genres: dict[str, str]) -> None:
    GUESSED_GENRES_PATH.write_text(json.dumps(genres, indent=1, ensure_ascii=False))


def build(
    db, playlist: str | None = None, track_ids: set[str] | None = None, with_cues: bool = True, steps=None
) -> tuple[list[Proposal], Context]:
    """If `with_cues=False`, do not read the beat grids. Then only the Start cue has a position.
    Do not apply cues from these proposals. `steps` (progress.Steps) shows the startup progress."""
    steps = steps or Steps(quiet=True)
    steps.step("Load library and analysis")
    analysis = Cache().all()
    everything = library.load_tracks(db, with_structure=False)
    analysed = [t for t in everything if t.id in analysis]
    if not analysed:
        raise SystemExit("nothing analysed yet, run `dj-librarian analyze` first")
    if len(analysed) < 200:
        print(f"warning: only {len(analysed)} tracks have an analysis. Energy, mood and genre compare "
              "each track with these tracks. They become more reliable with more analysed tracks.\n")

    ids = [t.id for t in analysed]
    from dj_librarian.claude_genres import load_answers

    steps.step("Learn genres, energy and moods")
    guessed = load_guessed_genres()
    decisions = load_genre_decisions()
    claude = load_answers()
    approved = {i: g for i, g in decisions.items() if g is not None}
    genres = tagging.guess_genres(everything, analysis, guessed, approved)
    moods = tagging.guess_moods(ids, analysis)
    energy = tagging.guess_energy(ids, analysis)
    generated = load_generated_cues(db)

    only = set(analysis) if track_ids is None else set(analysis) & track_ids
    proposals = []
    selected = library.load_tracks(db, playlist, with_structure=with_cues, only=only, steps=steps)
    steps.step("Plan the changes")
    for t in selected:
        a = analysis[t.id]
        rb_key, our_key = tagging.to_camelot(t.key), tagging.to_camelot(a["key"], a["scale"])
        guess = genres[t.id]
        if approved.get(t.id) == "":  # You removed the genre. Do not fill it again.
            action, write = ("approved", "") if t.genre else ("keep", None)
        elif approved.get(t.id):  # Your answer in review-genres wins over any later guess
            action, write = ("approved", approved[t.id]) if approved[t.id] != t.genre else ("keep", None)
        elif not t.genre or guessed.get(t.id) == t.genre:  # Empty, or an earlier guess of dj-librarian
            if guess.genre:
                action, write = "fill", guess.genre
            elif guess.confidence >= tagging.NEAR_MISS_CONFIDENCE or t.id in claude:
                action, write = "suggest", decisions.get(t.id)
            else:
                action, write = "unknown", None
        elif guess.genre and guess.genre.lower() != t.genre.lower():
            action, write = "review", decisions.get(t.id)
        else:
            action, write = "keep", None
        proposals.append(Proposal(
            track=t,
            analysis=a,
            bpm_check=tagging.compare_bpm(t.bpm, a["bpm"]),
            key_ours=our_key,
            key_check=tagging.compare_key(rb_key, our_key),
            genre_guess=guess,
            genre_action=action,
            genre_write=write,
            genre_pending=action in ("review", "suggest") and t.id not in decisions,
            claude=claude.get(t.id),
            energy=energy[t.id],
            moods=moods[t.id],
            comment=tagging.build_comment(t.comment, energy[t.id], moods[t.id]),
            cues=tagging.plan_cues(t, a, generated),
        ))
    steps.done()
    return proposals, Context(len(analysed), len(everything), everything, genres)
