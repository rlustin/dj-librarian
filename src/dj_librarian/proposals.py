"""What dj-librarian would change for each track. Shared by `report`, `review-genres` and `apply`."""

from dataclasses import dataclass

from dj_librarian import library, store, tagging
from dj_librarian.cache import Cache
from dj_librarian.progress import Steps


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
    return store.read_json(store.GENRE_DECISIONS, {})


def save_genre_decisions(decisions: dict[str, str | None]) -> None:
    store.write_json(store.GENRE_DECISIONS, decisions)


def save_genre_decision(track_id: str, genre: str | None) -> None:
    """Read the file again before the write. Then two sessions do not erase the answers of each other."""
    save_genre_decisions(load_genre_decisions() | {track_id: genre})


def load_generated_cues() -> set[str]:
    """UUIDs of the hot cues that dj-librarian created. A later run can move or replace only these cues."""
    return set(store.read_json(store.GENERATED_CUES, []))


def save_generated_cues(ids: set[str]) -> None:
    store.write_json(store.GENERATED_CUES, sorted(ids))


def load_guessed_genres() -> dict[str, str]:
    """content ID -> genre that dj-librarian wrote from its own guess, without your review."""
    return store.read_json(store.GUESSED_GENRES, {})


def save_guessed_genres(genres: dict[str, str]) -> None:
    store.write_json(store.GUESSED_GENRES, genres)


def your_genres(
    tracks: list[library.Track], guessed: dict[str, str], decisions: dict[str, str | None]
) -> dict[str, str]:
    """content ID -> the genre that you gave: in rekordbox, or in review-genres.

    The guesses of dj-librarian do not count.
    """
    genres = {}
    for t in tracks:
        if decisions.get(t.id) is not None:
            if decisions[t.id]:
                genres[t.id] = decisions[t.id]
        elif t.genre and guessed.get(t.id) != t.genre:
            genres[t.id] = t.genre
    return genres


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
    genres = tagging.guess_genres(everything, analysis, your_genres(everything, guessed, decisions))
    moods = tagging.guess_moods(ids, analysis)
    energy = tagging.guess_energy(ids, analysis)
    generated = load_generated_cues()

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
