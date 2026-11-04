import numpy as np
import pytest

from dj_librarian import tagging

from helpers import track


@pytest.mark.parametrize(("key", "scale", "camelot"), [
    ("Am", None, "8A"),
    ("C", None, "8B"),
    ("F#m", None, "11A"),
    ("Bb", None, "6B"),
    ("A", "minor", "8A"),
    ("G", "major", "9B"),
    ("8A", None, "8A"),
    (None, None, None),
    ("H", None, None),
])
def test_to_camelot(key, scale, camelot):
    assert tagging.to_camelot(key, scale) == camelot


@pytest.mark.parametrize(("rekordbox", "ours", "result"), [
    (120, 120.5, "match"),
    (120, 60, "half"),
    (85, 170, "double"),
    (120, 80, "2/3"),
    (None, 120, "missing"),
    (120, 100, "mismatch"),
])
def test_compare_bpm(rekordbox, ours, result):
    assert tagging.compare_bpm(rekordbox, ours) == result


@pytest.mark.parametrize(("rekordbox", "ours", "result"), [
    ("8A", "8A", "match"),
    ("8A", "8B", "relative"),
    ("8A", "9A", "adjacent"),
    ("12A", "1A", "adjacent"),
    ("8A", "11B", "parallel"),  # A minor and A major
    ("8A", "3B", "mismatch"),
    ("8A", None, "missing"),
])
def test_compare_key(rekordbox, ours, result):
    assert tagging.compare_key(rekordbox, ours) == result


@pytest.mark.parametrize(("existing", "comment"), [
    (None, "E7 Dark/Deep"),
    ("my note", "E7 Dark/Deep | my note"),
    ("E3 Happy | my note", "E7 Dark/Deep | my note"),  # The prefix of an earlier run goes away
    ("E3 Happy", "E7 Dark/Deep"),
    ("Visit https://artist.bandcamp.com", "E7 Dark/Deep"),
])
def test_build_comment(existing, comment):
    assert tagging.build_comment(existing, 7, ["Dark", "Deep"]) == comment


def _analysis(vector):
    return {"embedding": np.asarray(vector, dtype=np.float32), "genres": {"Electronic---Techno": 0.9}}


def test_guess_genres_votes_with_the_nearest_tagged_tracks():
    tracks = [track(str(i)) for i in range(7)]
    analysis = {"0": _analysis([1, 0.1]), "1": _analysis([1, 0.2]), "2": _analysis([1, 0]),
                "3": _analysis([0.1, 1]), "4": _analysis([0.2, 1]), "5": _analysis([0, 1]),
                "6": _analysis([1, 0.15])}
    yours = {"0": "Techno", "1": "Techno", "2": "Techno", "3": "Ambient", "4": "Ambient", "5": "Ambient"}

    guesses = tagging.guess_genres(tracks, analysis, yours)

    assert guesses["6"].genre == "Techno"
    assert guesses["6"].discogs_hint == "Techno"
    assert guesses["3"].genre == "Ambient"
    # Leave-one-out: a tagged track does not vote for itself
    assert "3" not in [i for i, _, _ in guesses["3"].neighbours]


def test_plan_cues_puts_the_start_cue_in_slot_a():
    plan = tagging.plan_cues(track(first_beat=1.0), None)

    assert plan.writes == {"A": ("Start", 1.0)}
    assert plan.skipped == {"Intro end": "not detected", "Full energy": "not detected"}


def test_plan_cues_does_not_move_your_cue_and_labels_it():
    plan = tagging.plan_cues(track(first_beat=1.0, hot_cues={"A": 0.5}, cue_ids={"A": "mine"}), None)

    assert plan.writes == {}
    assert plan.skipped["Start"] == "already on A"
    assert plan.labels == {"A": "Start"}


def test_plan_cues_replaces_a_cue_of_an_earlier_run():
    t = track(first_beat=1.0, hot_cues={"A": 3.0}, cue_names={"A": "Start"}, cue_ids={"A": "generated"})

    plan = tagging.plan_cues(t, None, {"generated"})

    assert plan.previous == {"A": ("Start", 3.0)}
    assert plan.writes == {"A": ("Start", 1.0)}


def test_plan_cues_uses_a_free_slot_if_a_is_yours():
    t = track(first_beat=30.0, hot_cues={"A": 60.0}, cue_ids={"A": "mine"})

    assert tagging.plan_cues(t, None).writes == {"B": ("Start", 30.0)}


def test_similar_pairs_in_blocks_find_the_pairs_of_the_full_matrix(monkeypatch):
    monkeypatch.setattr(tagging, "BLOCK", 3)
    embeddings = np.random.default_rng(1).normal(size=(10, 4))
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
    full = embeddings @ embeddings.T

    pairs = tagging.similar_pairs(embeddings, 0.3)

    assert [(x, y) for x, y, _ in pairs] == [tuple(map(int, p)) for p in zip(*np.where(np.triu(full, 1) > 0.3))]
    assert all(np.isclose(s, full[x, y]) for x, y, s in pairs)
