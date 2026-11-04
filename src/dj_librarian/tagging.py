"""Turn raw analysis into proposed rekordbox metadata.

These decisions compare each track with the full library. Energy and mood use a rank in
the collection, and genres come from the tracks that you tagged. Thus they need the
analysis of all tracks, not only the tracks in the report.
"""

import re
from dataclasses import dataclass, field

import numpy as np

from dj_librarian.library import HOT_CUE_KINDS, Track

# --- BPM & key ------------------------------------------------------------------------

NOTE_TO_PC = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6,
              "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}


def to_camelot(key: str | None, scale: str | None = None) -> str | None:
    """Accepts rekordbox keys ("Am", "F#", "8A") or Essentia's ("A", "minor")."""
    if not key:
        return None
    if re.fullmatch(r"(1[0-2]|[1-9])[AB]", key):
        return key
    minor = scale == "minor" if scale else key.endswith("m")
    pc = NOTE_TO_PC.get(key.removesuffix("m") if not scale else key)
    if pc is None:
        return None
    reference = 9 if minor else 0  # 8A = A minor, 8B = C major
    return f"{((pc - reference) * 7 % 12 + 7) % 12 + 1}{'A' if minor else 'B'}"


def compare_bpm(rekordbox: float | None, ours: float) -> str:
    if not rekordbox:
        return "missing"
    ratio = ours / rekordbox
    for name, target in (("match", 1), ("half", 0.5), ("double", 2), ("2/3", 2 / 3), ("3/2", 3 / 2)):
        if abs(ratio - target) < 0.015 * target:
            return name
    return "mismatch"


def compare_key(rekordbox: str | None, ours: str | None) -> str:
    if not rekordbox or not ours:
        return "missing"
    if rekordbox == ours:
        return "match"
    (n1, l1), (n2, l2) = (int(rekordbox[:-1]), rekordbox[-1]), (int(ours[:-1]), ours[-1])
    if n1 == n2:
        return "relative"
    minor, major = (n1, n2) if l1 == "A" else (n2, n1)
    if l1 != l2 and (minor + 3 - major) % 12 == 0:
        return "parallel"  # Same root note, other mode (for example G minor and G major)
    if l1 == l2 and (n1 - n2) % 12 in (1, 11):
        return "adjacent"
    return "mismatch"


# --- Genre ----------------------------------------------------------------------------

KNN_K = 10
# On the 650 tracks that you tagged, guesses at >= 0.5 confidence matched your tag 86% of the time.
# The Discogs model matched only 13% (other taxonomy, no Amapiano/Gqom/UK Funky).
# Thus dj-librarian shows it as a hint and never writes it.
KNN_MIN_CONFIDENCE = 0.5
# Below KNN_MIN_CONFIDENCE but at least this: review-genres shows the top candidates.
# On your tags, the top 3 had the right genre for 63% of these tracks.
NEAR_MISS_CONFIDENCE = 0.4
REVIEW_CANDIDATES = 3
BLOCK = 256  # rows of the similarity matrix at a time


@dataclass
class GenreGuess:
    genre: str | None  # Learned from your own tags. None if not confident enough.
    confidence: float
    discogs_hint: str  # top style from the pretrained Discogs model, for information only
    candidates: list[tuple[str, float]] = field(default_factory=list)  # best genres with their vote share
    neighbours: list[tuple[str, str, float]] = field(default_factory=list)  # (track ID, genre, similarity)


def guess_genres(
    tracks: list[Track],
    analysis: dict[str, dict],
    guessed: dict[str, str] | None = None,
    approved: dict[str, str] | None = None,
) -> dict[str, GenreGuess]:
    """k-nearest-neighbours vote among the tracks you tagged yourself, by audio embedding similarity.

    `guessed` holds the genres that dj-librarian wrote. Do not learn from them, or errors add up.
    `approved` holds the genre changes that you approved in review. Learn from them before
    `apply` writes them. Then the guesses do not change again after `apply`.
    """
    guessed, approved = guessed or {}, approved or {}
    ids = [t.id for t in tracks if t.id in analysis]
    by_id = {t.id: t for t in tracks}
    embeddings = np.stack([analysis[i]["embedding"] for i in ids])
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
    genre = lambda t: approved.get(t.id, t.genre)  # noqa: E731
    yours = lambda t: bool(approved[t.id]) if t.id in approved else bool(t.genre and guessed.get(t.id) != t.genre)  # noqa: E731
    labeled = [n for n, i in enumerate(ids) if yours(by_id[i])]
    labels = [genre(by_id[ids[n]]) for n in labeled]
    similarity = embeddings @ embeddings[labeled].T if labeled else np.zeros((len(ids), 0))

    guesses = {}
    for row, track_id in enumerate(ids):
        sims = similarity[row].copy()
        if yours(by_id[track_id]):  # Leave-one-out: a track cannot vote for itself
            sims[labeled.index(row)] = -np.inf
        votes: dict[str, float] = {}
        nearest = [n for n in np.argsort(sims)[::-1][:KNN_K] if np.isfinite(sims[n])]
        for n in nearest:
            votes[labels[n]] = votes.get(labels[n], 0) + max(float(sims[n]), 0)
        total = sum(votes.values()) or 1
        candidates = [(g, w / total) for g, w in sorted(votes.items(), key=lambda kv: -kv[1])][:REVIEW_CANDIDATES]
        confidence = candidates[0][1] if candidates else 0.0
        genre = candidates[0][0] if confidence >= KNN_MIN_CONFIDENCE else None
        hint = next(iter(analysis[track_id]["genres"])).split("---")[-1]
        neighbours = [(ids[labeled[n]], labels[n], float(sims[n])) for n in nearest]
        guesses[track_id] = GenreGuess(genre, confidence, hint, candidates, neighbours)
    return guesses


# --- Mood & energy ----------------------------------------------------------------------

MOOD_TAGS = {  # model tag -> label written in the comment
    "dark": "Dark", "deep": "Deep", "groovy": "Groovy", "uplifting": "Uplifting",
    "melancholic": "Melancholic", "happy": "Happy", "emotional": "Emotional", "dream": "Dreamy",
    "space": "Spacey", "sexy": "Sexy", "heavy": "Heavy", "epic": "Epic", "hopeful": "Hopeful",
    "calm": "Chill",
}
MOOD_MIN_ZSCORE = 0.75
MOODS_PER_TRACK = 2


def _zscores(values: np.ndarray) -> np.ndarray:
    std = values.std(axis=0)
    return (values - values.mean(axis=0)) / np.where(std > 0, std, 1)


def guess_moods(ids: list[str], analysis: dict[str, dict]) -> dict[str, list[str]]:
    """The moods where a track stands out most compared to the rest of the library."""
    tags = list(MOOD_TAGS)
    scores = np.array(
        [[analysis[i]["moodtheme"][t] for t in tags] + [analysis[i]["moods"]["aggressive"]] for i in ids]
    )
    labels = list(MOOD_TAGS.values()) + ["Aggressive"]
    z = _zscores(scores)
    moods = {}
    for row, track_id in enumerate(ids):
        order = np.argsort(z[row])[::-1]
        chosen = [labels[n] for n in order[:MOODS_PER_TRACK] if z[row, n] >= MOOD_MIN_ZSCORE]
        moods[track_id] = chosen or [labels[order[0]]]
    return moods


ENERGY_FEATURES = {  # feature -> weight
    "arousal": 0.35,
    "onset_rate": 0.2,
    "loudness": 0.15,
    "aggressive": 0.15,
    "bpm": 0.15,
}


def energy_features(result: dict) -> dict[str, float]:
    return {
        "arousal": result["arousal"],
        "onset_rate": result["onset_rate"],
        "loudness": float(np.percentile(result["env_full"], 90)),
        "aggressive": result["moods"]["aggressive"],
        "bpm": result["bpm"],
    }


def guess_energy(ids: list[str], analysis: dict[str, dict]) -> dict[str, int]:
    """1-10, centred on 5-6 for an average track of this library."""
    features = np.array([[energy_features(analysis[i])[f] for f in ENERGY_FEATURES] for i in ids])
    combined = _zscores(features) @ np.array(list(ENERGY_FEATURES.values()))
    combined = _zscores(combined)
    levels = np.clip(np.round(5.5 + 2 * combined), 1, 10).astype(int)
    return dict(zip(ids, levels.tolist()))


# --- Comment ----------------------------------------------------------------------------

GENERATED_PREFIX = re.compile(r"^E(10|[1-9])( [A-Z][a-z]+(/[A-Z][a-z]+)*)?( \| |$)")
BOILERPLATE = re.compile(r"^\s*(visit https?://\S+|www\.mediahuman\.com)\s*$", re.IGNORECASE)


def build_comment(existing: str | None, energy: int, moods: list[str]) -> str:
    notes = GENERATED_PREFIX.sub("", existing or "").strip()
    if BOILERPLATE.match(notes):
        notes = ""
    prefix = f"E{energy} {'/'.join(moods)}"
    return f"{prefix} | {notes}" if notes else prefix


# --- Cue points -------------------------------------------------------------------------

PHRASE_BARS = 8
MIN_INTRO_BARS = 8  # rekordbox sometimes labels a 1-2 bar pickup as the intro
FULL_ENERGY_TOLERANCE_DB = 1.5
STEP_WINDOW_BARS = 2
MIN_TRANSITION_STEP_DB = 3  # A clear transition: all next bars are at least this much louder

# Labels rekordbox puts on cues by itself: such cues count as unnamed
AUTO_LABEL = re.compile(r"^\d+\.\d+Bars$")


@dataclass
class CuePlan:
    positions: dict[str, float | None]  # cue name -> seconds
    rekordbox_chorus: float | None  # first phrase rekordbox labels "Chorus", for comparison
    writes: dict[str, tuple[str, float]] = field(default_factory=dict)  # slot letter -> (cue name, seconds)
    previous: dict[str, tuple[str, float]] = field(default_factory=dict)  # cues dj-librarian created in an earlier run
    labels: dict[str, str] = field(default_factory=dict)  # your unnamed cues already at a cue point -> label
    skipped: dict[str, str] = field(default_factory=dict)  # cue name -> reason


CUE_NAMES = ["Start", "Intro end", "Full energy"]


def plan_cues(track: Track, result: dict | None, generated_ids: set[str] = frozenset()) -> CuePlan:
    # Where the sound starts: some tracks have hits before the first beat of the rekordbox grid
    start = result.get("audio_start", track.first_beat) if result else track.first_beat
    intro_end = full_energy = None
    bars = track.downbeats
    if result and bars is not None and len(bars) > 2 * PHRASE_BARS:
        levels = np.array([_level(result, bars[i], bars[i + 1]) for i in range(len(bars) - 1)])
        # Phrases start on bar 1 of rekordbox's grid, then every 8 bars (9.1, 17.1, 25.1...)
        boundaries = list(range(0, len(levels), PHRASE_BARS))
        intro_bar = _intro_end(track, levels, boundaries)
        # Full energy is a phrase of its own, after the intro end
        full_bar = _full_energy(levels, boundaries, after=intro_bar + 1 if intro_bar is not None else 0)
        intro_end = float(bars[intro_bar]) if intro_bar is not None else None
        full_energy = float(bars[full_bar]) if full_bar is not None else None
    chorus = next((p.start for p in track.phrases if p.kind == "Chorus"), None)

    plan = CuePlan({"Start": start, "Intro end": intro_end, "Full energy": full_energy}, chorus)
    _assign_slots(track, plan, generated_ids)
    return plan


def _level(result: dict, start: float, end: float) -> float:
    """Average loudness (dB) between two times, full band and kick/bass band weighted equally."""
    hop = result["duration"] / len(result["env_full"])
    a, b = int(start / hop), int(end / hop)
    if b - a < 2 or a < 0:
        return -np.inf
    to_power = lambda db: 10 ** (db / 10)  # noqa: E731
    full = 10 * np.log10(to_power(result["env_full"][a:b]).mean())
    low = 10 * np.log10(to_power(result["env_low"][a:b]).mean())
    return (full + low) / 2


def _step(levels: np.ndarray, bar: int) -> float:
    """How much louder the bars after `bar` are than the bars before (every one of them)."""
    w = STEP_WINDOW_BARS
    if bar - w < 0 or bar + w > len(levels):
        return -np.inf
    return float(levels[bar:bar + w].min() - levels[bar - w:bar].max())


def _intro_end(track: Track, levels: np.ndarray, boundaries: list[int]) -> int | None:
    """Phrase boundary near rekordbox's intro end: the one with the clearest transition, else the nearest."""
    bars = track.downbeats
    eligible = [b for b in boundaries if b >= MIN_INTRO_BARS]
    anchor = None
    if track.phrases and track.beat_length:
        min_end = track.phrases[0].start + MIN_INTRO_BARS * 4 * track.beat_length - 0.05
        rekordbox = next((p.start for p in track.phrases[1:] if p.kind != "Intro" and p.start >= min_end), None)
        if rekordbox is not None:
            anchor = int(np.argmin(np.abs(bars - rekordbox)))
    candidates = [b for b in eligible if anchor is None or abs(b - anchor) <= PHRASE_BARS]
    if anchor is None:
        candidates = candidates[:2]
    if not candidates:
        return None
    step, best = max((_step(levels, b), b) for b in candidates)
    if step >= MIN_TRANSITION_STEP_DB:
        return best
    return min(candidates, key=lambda b: abs(b - anchor)) if anchor is not None else None


def _full_energy(levels: np.ndarray, boundaries: list[int], after: int) -> int | None:
    """Start of the first phrase whose loudness (full band + kick/bass band) is near the track's peak."""
    to_power = lambda db: 10 ** (db / 10)  # noqa: E731
    phrases = {
        b: 10 * np.log10(to_power(levels[b:b + PHRASE_BARS]).mean())
        for b in boundaries
        if len(levels[b:b + PHRASE_BARS]) >= PHRASE_BARS // 2
    }
    if not phrases:
        return None
    peak = max(phrases.values())
    return next((b for b, level in phrases.items() if b >= after and level >= peak - FULL_ENERGY_TOLERANCE_DB), None)


def _assign_slots(track: Track, plan: CuePlan, generated_ids: set[str]) -> None:
    """Same slot as a previous run, else A/B/C, else the next free slot. Never moves your own cues."""
    plan.previous = {
        slot: (track.cue_names.get(slot, ""), seconds)
        for slot, seconds in track.hot_cues.items()
        if track.cue_ids.get(slot) in generated_ids
    }
    own_cues = {slot: seconds for slot, seconds in track.hot_cues.items() if slot not in plan.previous}
    used = set(own_cues)
    tolerance = max(track.beat_length or 0.5, 0.5)
    for name, preferred in zip(CUE_NAMES, ["A", "B", "C"]):
        position = plan.positions[name]
        if position is None:
            plan.skipped[name] = "not detected"
            continue
        if name == "Start":
            # Your cue before the sound starts or in the first bar is the start cue
            near = max(4 * (track.beat_length or 0.5), 2.0)
            existing = next((s for s, t in sorted(own_cues.items(), key=lambda c: c[1]) if t <= position + near), None)
        else:
            existing = next((s for s, t in own_cues.items() if abs(t - position) <= tolerance), None)
        if existing:
            plan.skipped[name] = f"already on {existing}"
            label = track.cue_names.get(existing)
            if (not label or AUTO_LABEL.match(label)) and existing not in plan.labels:
                plan.labels[existing] = name
            continue
        if any(abs(t - position) <= tolerance for _, t in plan.writes.values()):
            plan.skipped[name] = "same position as another generated cue"
            continue
        previous_slot = next((s for s, (n, _) in plan.previous.items() if n == name), None)
        slot = next((s for s in (previous_slot, preferred, *HOT_CUE_KINDS.values()) if s and s not in used), None)
        if slot is None:
            plan.skipped[name] = "no free hot cue slot"
            continue
        used.add(slot)
        plan.writes[slot] = (name, position)
