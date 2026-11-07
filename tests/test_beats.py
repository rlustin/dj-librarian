from dataclasses import replace

import numpy as np
import pytest

from dj_librarian import beats

BPM, FIRST = 95.0, 0.3
PERIOD = 60 / BPM


@pytest.fixture(scope="module")
def track_95(tmp_path_factory):
    """60 s of kicks at 95 BPM. The first beat of each bar is louder."""
    import essentia.standard as es

    rng = np.random.default_rng(0)
    audio = (rng.normal(size=beats.SR * 60) * 0.003).astype(np.float32)
    n = np.arange(4000)
    kick = np.sin(2 * np.pi * 55 * n / beats.SR) * np.exp(-n / 900) + rng.normal(size=4000) * np.exp(-n / 150) * 0.3
    for k, t in enumerate(np.arange(FIRST, 59, PERIOD)):
        i = int(t * beats.SR)
        audio[i:i + 4000] += (kick * (0.9 if k % 4 == 0 else 0.4)).astype(np.float32)
    path = str(tmp_path_factory.mktemp("audio") / "track.wav")
    es.MonoWriter(filename=path, format="wav", sampleRate=beats.SR)(audio)
    return path


def test_check_finds_the_tempo_of_a_wrong_grid(track_95):
    wrong = list(np.arange(0.1, 59, 60 / 126.67))

    result = beats.check(track_95, wrong)
    verdict, better = beats.verdict(result)

    assert verdict == "wrong tempo"
    assert better.bpm == pytest.approx(BPM, abs=0.05)
    assert (better.phase - beats.ENVELOPE_LAG) % PERIOD == pytest.approx(FIRST % PERIOD, abs=0.012)
    assert better.downbeat == 0  # the loud kick


def test_check_accepts_a_right_grid(track_95):
    right = list(np.arange(FIRST, 59, PERIOD) + 0.04)  # rekordbox is later than the decoded audio

    result = beats.check(track_95, right)

    assert beats.verdict(result)[0] == "ok"
    assert result["grid_fit"] > beats.NO_CLEAR_BEAT


def _result(grid_bpm, grid_fit, same, families, grid_first=0.25):
    return {"grid_bpm": grid_bpm, "grid_fit": grid_fit, "grid_offset": -0.06, "grid_first": grid_first,
            "same": same, "families": families}


@pytest.mark.parametrize(("result", "expected"), [
    (_result(140, 6.0, [6.1, 140, 0.1, 0], [[7, 70, 0.1, 0], [6.2, 140, 0.1, 0], [3, 280, 0.1, 0]]), "ok"),
    (_result(153.6, 1.9, [3.0, 154.4, 0.1, 0], [[8.6, 95, 0.2, 1], [6.0, 190, 0.2, 1]]), "wrong tempo"),
    (_result(91.2, 1.8, [7.2, 91.5, 0.1, 0], [[7.3, 91.5, 0.1, 0]]), "drifts"),
    (_result(140, 2.0, [6.0, 140, 0.3, 0], [[6.1, 140, 0.3, 0]]), "shifted"),
    # rekordbox is half a beat (0.214 s) after the strongest hits: the kick, between the hi-hats
    (_result(140, 2.0, [6.0, 140, 0.3, 0], [[6.1, 140, 0.3, 0]], grid_first=0.3 + 0.06 + 0.214), "ok"),
    ({"version": 1, "no_grid": True}, "no grid"),
    (_result(150, 2.2, [2.3, 149, 0.1, 0], [[2.4, 180, 0.1, 0]]), "no clear beat"),
])
def test_verdict(result, expected):
    assert beats.verdict(result)[0] == expected


def test_wrong_tempo_proposes_the_level_nearest_to_the_grid():
    result = _result(153.6, 1.9, [3.0, 154.4, 0.1, 0], [[4.0, 47.5, 0.2, 1], [8.6, 95, 0.2, 1], [6.0, 190, 0.2, 1]])

    assert beats.verdict(result)[1].bpm == 190  # 153.6 is nearer to 190 than to 95
    assert beats.nearest_family(result, 125).bpm == 95  # the median BPM of the genre


def test_new_grid():
    proposal = beats.Proposal(fit=8.0, bpm=120.0, phase=0.2, downbeat=2)

    grid = beats.new_grid(proposal, offset=-0.06, duration=3.0)

    assert grid[0] == (3, pytest.approx(0.26))  # 0.2 s in the envelope is 0.26 s in rekordbox
    assert [n for n, _ in grid[:6]] == [3, 4, 1, 2, 3, 4]
    assert grid[-1][1] <= 3.0 and np.diff([t for _, t in grid]) == pytest.approx(0.5)


def test_new_grid_starts_at_the_first_beat_after_zero():
    grid = beats.new_grid(beats.Proposal(fit=8.0, bpm=120.0, phase=0.48, downbeat=0), offset=-0.06, duration=2.0)

    assert grid[0][1] == pytest.approx(0.04)


def test_keep_bar_position():
    old = [((k % 4) + 1, 0.1 + k * 0.5) for k in range(32)]  # downbeats at 0.1, 2.1, 4.1 ...
    new = [(1, 0.6 + k * 0.5) for k in range(32)]  # the second new beat is at 1.1, the fourth at 2.1

    assert beats.keep_bar_position(old, new) == 3


def test_calibrate_by_file_type():
    def ok(offset):
        return _result(140, 6.0, [6.0, 140, 0.1, 0], [[6.0, 140, 0.1, 0]]) | {"grid_offset": offset}

    results = {f"m{i}": ok(-0.06) for i in range(12)} | {f"f{i}": ok(-0.02) for i in range(12)} | {"w": ok(-0.03)}
    extensions = {i: ".mp3" if i.startswith("m") else ".flac" if i.startswith("f") else ".wav" for i in results}

    offsets = beats.calibrate(results, extensions)

    assert offsets[".mp3"] == pytest.approx(-0.06)
    assert offsets[".flac"] == pytest.approx(-0.02)
    assert offsets[".wav"] == beats.LOSSLESS_OFFSET  # too few tracks
    assert beats.offset_for(offsets, ".ogg") == offsets[""] == pytest.approx(-0.03)  # the median of all


def test_follow_old_grid_keeps_the_beat_of_rekordbox():
    env = np.ones(int(60 * beats.SR / beats.HOP), dtype=np.float32)
    old = np.arange(0.5, 59, 0.5)  # 120 BPM: right in the first half of the track, then late
    old[60:] += np.linspace(0, 0.2, len(old) - 60)
    for t in old[:60]:
        env[round(t * beats.SR / beats.HOP)] = 10
    off_beat = beats.Proposal(fit=6.0, bpm=120.0, phase=0.25, downbeat=0)

    assert beats.follow_old_grid(env, list(old), 0.0, off_beat).phase == pytest.approx(0.5)
    on_beat = beats.Proposal(fit=6.0, bpm=120.0, phase=0.0, downbeat=0)
    assert beats.follow_old_grid(env, list(old), 0.0, on_beat) == on_beat


def test_follow_old_grid_ignores_a_grid_that_fits_nowhere():
    env = np.ones(int(60 * beats.SR / beats.HOP), dtype=np.float32)
    for t in np.arange(0.25, 59, 0.5):
        env[round(t * beats.SR / beats.HOP)] = 10
    old = list(np.arange(0.5, 59, 0.5))  # always between the hits
    off_beat = beats.Proposal(fit=10.0, bpm=120.0, phase=0.25, downbeat=0)

    assert beats.follow_old_grid(env, old, 0.0, off_beat) == off_beat


def _low(on_beat, off_beat, seconds=60):
    """The low band at 120 BPM: peaks of this level on the beats (0, 0.5 ...) and on the off-beats (0.25 ...)."""
    low = np.ones(int(seconds * beats.SR / beats.HOP), dtype=np.float32)
    for t in np.arange(0.0, seconds - 1, 0.5):
        low[round((t + 0.01) * beats.SR / beats.HOP)] = on_beat  # the low band peaks a little after the hit
        low[round((t + 0.26) * beats.SR / beats.HOP)] = off_beat
    return low


OFF_BEAT_GRID = beats.Proposal(fit=8.0, bpm=120.0, phase=0.25, downbeat=0)


def test_on_the_kick_moves_a_grid_on_the_off_beat():
    assert beats.on_the_kick(_low(on_beat=10, off_beat=4), OFF_BEAT_GRID).phase == pytest.approx(0.5)


def test_on_the_kick_keeps_a_grid_if_the_kicks_are_not_clear():
    assert beats.on_the_kick(_low(on_beat=5, off_beat=4), OFF_BEAT_GRID) == OFF_BEAT_GRID
    assert beats.on_the_kick(_low(on_beat=1, off_beat=1), OFF_BEAT_GRID) == OFF_BEAT_GRID


def test_first_hit_skips_the_noise_before_the_music():
    env = np.ones(1000, dtype=np.float32)
    env[50] = 2  # noise
    env[100::40] = 10

    assert beats.first_hit(env) == pytest.approx(100 * beats.HOP / beats.SR)


def test_refit_measures_the_moved_grid():
    period = 0.5 * beats.SR / beats.HOP
    env = np.ones(5000, dtype=np.float32)
    env[np.round(np.arange(0, 4999, period)).astype(int)] = 10
    on_beat = beats.Proposal(fit=0.0, bpm=120.0, phase=0.0, downbeat=0)

    assert beats.refit(env, on_beat).fit == pytest.approx(10)
    assert beats.refit(env, replace(on_beat, phase=0.25)).fit == pytest.approx(1)
