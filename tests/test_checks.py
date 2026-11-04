import numpy as np

from dj_librarian import checks

from helpers import track


def test_run_finds_a_bpm_at_half_the_bpm_of_the_genre():
    rng = np.random.default_rng(0)
    tracks = [track(str(i), "Jungle", bpm=170.0) for i in range(checks.BPM_MIN_GENRE_TRACKS)]
    tracks.append(track("half", "Jungle", bpm=86.0))
    analysis = {t.id: {"embedding": rng.normal(size=8).astype(np.float32)} for t in tracks}
    genres = {t.id: t.genre for t in tracks}

    report = checks.run(tracks, analysis, genres, genres)

    assert [(t.id, genre, median) for t, genre, median in report.bpm] == [("half", "Jungle", 170.0)]
    assert report.genres["Jungle"].tracks == len(tracks)


def test_run_finds_copies_and_rare_genre_names():
    tracks = [track("a", "Jungle"), track("b", "jungle"), track("c", "Grime")]
    analysis = {"a": {"embedding": np.array([1, 0], np.float32)}, "b": {"embedding": np.array([1, 0], np.float32)},
                "c": {"embedding": np.array([0, 1], np.float32)}}
    genres = {t.id: t.genre for t in tracks}

    report = checks.run(tracks, analysis, genres, genres)

    assert [(d.a.id, d.b.id) for d in report.duplicates] == [("a", "b")]
    assert report.variants == [[("Jungle", 1), ("jungle", 1)]]
    assert report.rare == [("Grime", 1)]
