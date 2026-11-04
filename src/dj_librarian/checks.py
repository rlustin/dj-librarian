"""Find what looks wrong in the library: duplicates, odd genre names, tracks that do not sound
like their genre, and BPM at half or double the BPM of their genre. Read-only."""

import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from dj_librarian import store
from dj_librarian.library import Track
from dj_librarian.tagging import KNN_K, similar_pairs, similarity_rows

DUPLICATE_SIMILARITY = 0.985  # Above this, two tracks have the same audio (copy, or edit of the same track)
MIN_GENRE_TRACKS = 10  # Smaller genres give too few neighbours to judge a track
RARE_GENRE_TRACKS = 2
BPM_MIN_GENRE_TRACKS = 15
BPM_TOLERANCE = 0.08


@dataclass
class Duplicate:
    a: Track
    b: Track
    similarity: float
    genres: tuple[str | None, str | None]


@dataclass
class Outlier:
    track: Track
    genre: str
    neighbours: Counter  # genre -> count among the KNN_K nearest tracks that you tagged


@dataclass
class GenreStats:
    name: str
    tracks: int
    consistency: float  # mean share of the nearest tracks that have the same genre
    mixed_with: list[tuple[str, int]]
    median_bpm: float | None


@dataclass
class Report:
    duplicates: list[Duplicate] = field(default_factory=list)
    variants: list[list[tuple[str, int]]] = field(default_factory=list)
    rare: list[tuple[str, int]] = field(default_factory=list)
    invalid: list[tuple[str, int]] = field(default_factory=list)
    outliers: list[Outlier] = field(default_factory=list)
    genres: dict[str, GenreStats] = field(default_factory=dict)
    mixed_pairs: list[tuple[tuple[str, str], int]] = field(default_factory=list)
    bpm: list[tuple[Track, str, float]] = field(default_factory=list)  # track, genre, median BPM of the genre
    neighbours: dict[str, Counter] = field(default_factory=dict)  # content ID -> genres of the nearest tagged tracks


def load_outliers_reviewed() -> dict[str, str]:
    """content ID -> the genre that you confirmed in `review-genres --outliers`."""
    return store.read_json(store.OUTLIERS_REVIEWED, {})


def save_outlier_reviewed(track_id: str, genre: str) -> None:
    store.write_json(store.OUTLIERS_REVIEWED, load_outliers_reviewed() | {track_id: genre})


def your_genres(tracks: list[Track], guessed: dict[str, str], decisions: dict[str, str | None]) -> dict[str, str]:
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


def run(tracks: list[Track], analysis: dict[str, dict], yours: dict[str, str], current: dict[str, str]) -> Report:
    """`yours`: the genres that you gave. `current`: the genre of each track after `apply`, guesses included."""
    report = Report()
    ids = [t.id for t in tracks if t.id in analysis]
    by_id = {t.id: t for t in tracks}
    embeddings = np.stack([analysis[i]["embedding"] for i in ids])
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
    for x, y, similarity in similar_pairs(embeddings, DUPLICATE_SIMILARITY):
        a, b = by_id[ids[x]], by_id[ids[y]]
        report.duplicates.append(Duplicate(a, b, similarity, (current.get(a.id), current.get(b.id))))
    report.duplicates.sort(key=lambda d: (d.genres[0] == d.genres[1], d.a.artist.lower(), d.a.title.lower()))

    names = Counter(current.values())
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(re.sub(r"[^a-z0-9]", "", name.lower()), []).append(name)
    report.variants = [[(n, names[n]) for n in group] for group in groups.values() if len(group) > 1]
    report.invalid = [(n, c) for n, c in names.items() if not n.isprintable() or not n.strip()]
    listed = {n for group in report.variants for n, _ in group} | {n for n, _ in report.invalid}
    report.rare = [(n, c) for n, c in names.most_common() if c <= RARE_GENRE_TRACKS and n not in listed]

    tagged = [n for n, i in enumerate(ids) if i in yours]
    labels = [yours[ids[n]] for n in tagged]
    sizes = Counter(labels)
    shares: dict[str, list[float]] = {}
    mixed: dict[str, Counter] = {}
    pairs = Counter()
    for (row, sims), label in zip(similarity_rows(embeddings[tagged], embeddings[tagged]), labels):
        row = tagged[row]
        # A copy of the same track says nothing about the genre
        sims = np.where(sims > DUPLICATE_SIMILARITY, -np.inf, sims)
        nearest = Counter(labels[n] for n in np.argsort(sims)[::-1][:KNN_K] if np.isfinite(sims[n]))
        report.neighbours[ids[row]] = nearest
        shares.setdefault(label, []).append(nearest[label] / KNN_K)
        for other, count in nearest.items():
            if other != label:
                mixed.setdefault(label, Counter())[other] += count
                pairs[tuple(sorted((label, other)))] += count
        if nearest[label] == 0 and sizes[label] >= MIN_GENRE_TRACKS:
            report.outliers.append(Outlier(by_id[ids[row]], label, nearest))
    report.outliers.sort(key=lambda o: -o.neighbours.most_common(1)[0][1])
    report.mixed_pairs = pairs.most_common()

    for name, values in shares.items():
        bpms = [by_id[i].bpm for i, g in current.items() if g == name and i in by_id and by_id[i].bpm]
        report.genres[name] = GenreStats(
            name, len(values), float(np.mean(values)), mixed.get(name, Counter()).most_common(3),
            float(np.median(bpms)) if bpms else None,
        )
        if len(bpms) < BPM_MIN_GENRE_TRACKS:
            continue
        median = report.genres[name].median_bpm
        for i, g in current.items():
            t = by_id.get(i)
            if g == name and t and t.bpm and min(abs(t.bpm / median - r) / r for r in (0.5, 2)) < BPM_TOLERANCE:
                report.bpm.append((t, name, median))
    return report


def crate(tracks: list[Track], analysis: dict[str, dict], yours: dict[str, str], genre: str) -> list[tuple[Track, float]]:
    """The tracks that you gave this genre, from the most typical sound to the least typical.

    Typical: near the average sound of the genre (cosine similarity to the mean fingerprint).
    """
    members = [t for t in tracks if t.id in analysis and yours.get(t.id, "").lower() == genre.lower()]
    if not members:
        return []
    embeddings = np.stack([analysis[t.id]["embedding"] for t in members])
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
    centre = embeddings.mean(axis=0)
    scores = embeddings @ (centre / np.linalg.norm(centre))
    return sorted(zip(members, map(float, scores)), key=lambda m: -m[1])
