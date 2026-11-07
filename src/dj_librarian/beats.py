"""Compare the rekordbox beat grid with the drum hits of the audio, and find a better grid.

The fit of a grid is the mean onset strength on its beats: 1 means no relation with the beats,
a good grid on a clear beat gets 5 or more. dj-librarian compares the rekordbox grid with the best
constant grid at the same tempo, and with the best grid at any tempo.
"""

from collections import Counter
from dataclasses import dataclass, replace

import numpy as np

SR = 44100
HOP = 256  # 5.8 ms
FRAME = 1024
ENVELOPE_LAG = -0.0186  # seconds from a hit to the peak of its onset envelope (measured on synthetic hits)
OFFSETS = np.arange(-150, 51, 2) / 1000  # rekordbox and Essentia do not decode MP3 files with the same delay
DEFAULT_OFFSET = -0.058  # measured on MP3 files
LOSSLESS = (".wav", ".aif", ".aiff", ".flac")
LOSSLESS_OFFSET = -0.012  # no decoder delay
MIN_BPM, MAX_BPM = 70, 200
CHECK_VERSION = 1

NO_CLEAR_BEAT = 3.5  # below this fit, the music has no beat that a constant grid can follow
WRONG_TEMPO = 0.6  # the rekordbox grid has less than this share of the fit of a grid at another tempo
DRIFT = 0.75
SHIFT = 0.6
KICK = 1.5  # move a proposal half a beat if the kicks there are this much stronger (checked on 140 right grids)
FOLLOW = 0.4  # right grids on the beat get 0.6 or more of the fit of the off-beat
OFF_BEAT = 0.1  # a grid half a beat (+/- this share of a beat) from the best grid is on the beat, not shifted


def envelopes(audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Onset strength (full band) and low-band level (kick and bass), one value per HOP."""
    import essentia.standard as es

    window, spectrum, onset = es.Windowing(type="hann"), es.Spectrum(), es.OnsetDetection(method="flux")
    no_phase = np.zeros(FRAME // 2 + 1, dtype=np.float32)
    env, low = [], []
    for frame in es.FrameGenerator(audio, frameSize=FRAME, hopSize=HOP, startFromZero=True):
        magnitudes = spectrum(window(frame))
        env.append(onset(magnitudes, no_phase))
        low.append(magnitudes[1:5].sum())  # 43-172 Hz
    env = np.asarray(env)
    env = np.maximum(env - np.convolve(env, np.ones(43) / 43, mode="same"), 0)  # peaks above the local mean
    env /= env.mean() or 1
    env = np.maximum(env, np.maximum(np.roll(env, 1), np.roll(env, -1)))  # one frame of tolerance
    low = np.maximum(np.diff(np.asarray(low), prepend=0), 0)
    return env.astype(np.float32), (low / (low.mean() or 1)).astype(np.float32)


def fit(env: np.ndarray, times) -> float:
    idx = np.round(np.asarray(times) * SR / HOP).astype(int)
    idx = idx[(idx >= 0) & (idx < len(env))]
    return float(env[idx].mean()) if len(idx) else 0.0


def search(env: np.ndarray, bpms) -> tuple[float, float, float]:
    """(fit, BPM, time of the first beat in the envelope) of the best constant grid at these tempos."""
    best = (0.0, 0.0, 0.0)
    for bpm in bpms:
        period = 60 / bpm * SR / HOP
        beats = int((len(env) - period) / period)
        if beats < 8:
            continue
        idx = np.round(np.arange(int(period))[:, None] + np.arange(beats)[None, :] * period).astype(int)
        scores = env[idx].mean(axis=1)
        k = int(scores.argmax())
        if scores[k] > best[0]:
            best = (float(scores[k]), float(bpm), k * HOP / SR)
    return best


def downbeat(low: np.ndarray, bpm: float, phase: float) -> int:
    """Which of the first four beats starts a bar: the one with the strongest kick and bass, on average."""
    period = 60 / bpm
    times = np.arange(phase, len(low) * HOP / SR, period)
    idx = np.round(times * SR / HOP).astype(int)
    idx = idx[idx < len(low)]
    strength = [low[idx[r::4]].mean() if len(idx[r::4]) else 0 for r in range(4)]
    return int(np.argmax(strength))


def check(path: str, grid_times: list[float]) -> dict:
    """Analyse the audio file against the rekordbox grid. Store the result: the verdict comes later."""
    audio = load(path)
    env, low = envelopes(audio)
    times = np.asarray(grid_times)
    grid_bpm = 60 * (len(times) - 1) / (times[-1] - times[0])
    scores = [fit(env, times + o) for o in OFFSETS]
    k = int(np.argmax(scores))
    same = search(env, np.arange(grid_bpm * 0.99, grid_bpm * 1.01, 0.01))
    coarse = search(env, np.arange(MIN_BPM, MAX_BPM, 0.1))
    best = search(env, np.arange(coarse[1] - 0.15, coarse[1] + 0.15, 0.01))[1]
    families = [search(env, np.arange(b - 0.05, b + 0.05, 0.01)) for b in (best / 2, best, best * 2)]
    return {
        "version": CHECK_VERSION,
        "duration": len(audio) / SR,
        "grid_bpm": grid_bpm,
        "grid_first": float(times[0]),
        "grid_fit": scores[k],
        "grid_offset": float(OFFSETS[k]),
        "same": [*same, downbeat(low, same[1], same[2])],
        "families": [[*f, downbeat(low, f[1], f[2])] for f in families if f[1]],
    }


@dataclass
class Proposal:
    """In the order of the values in a check result: fit, BPM, phase, downbeat."""

    fit: float
    bpm: float
    phase: float  # first beat, in envelope time
    downbeat: int  # index of the first beat of a bar among the first four beats


def nearest_family(result: dict, target: float) -> Proposal:
    """The candidate at the tempo level (x1/2, x1, x2) nearest to `target`."""
    return Proposal(*min(result["families"], key=lambda f: abs(np.log(f[1] / target))))


def grid_shift(result: dict) -> float:
    """Where the rekordbox grid is, in beats after the best grid at the same tempo: from -0.5 to 0.5."""
    same = Proposal(*result["same"])
    beat = (result["grid_first"] + result["grid_offset"] - same.phase) * same.bpm / 60
    return (beat + 0.5) % 1 - 0.5


def verdict(result: dict) -> tuple[str, Proposal | None]:
    """("ok" | "no grid" | "no clear beat" | "wrong tempo" | "drifts" | "shifted", the better grid if any)."""
    if result.get("no_grid"):
        return "no grid", None
    grid_bpm, grid_fit = result["grid_bpm"], result["grid_fit"]
    same = Proposal(*result["same"])
    level = nearest_family(result, grid_bpm)
    if level.fit < NO_CLEAR_BEAT and same.fit < NO_CLEAR_BEAT:
        return "no clear beat", None
    if abs(level.bpm / grid_bpm - 1) > 0.01 and grid_fit < WRONG_TEMPO * level.fit:
        return "wrong tempo", level
    if abs(same.bpm - grid_bpm) >= 0.05 and grid_fit < DRIFT * same.fit:
        return "drifts", same
    # Hi-hats and claps on the off-beat are often louder than the kick: there, rekordbox is right
    if grid_fit < SHIFT * same.fit and abs(abs(grid_shift(result)) - 0.5) > OFF_BEAT:
        return "shifted", same
    return "ok", None


def calibrate(results: dict[str, dict], extensions: dict[str, str]) -> dict[str, float]:
    """The offset between rekordbox and the envelope, for each file type, from the grids that are right."""
    offsets: dict[str, list[float]] = {}
    for track_id, result in results.items():
        if verdict(result)[0] == "ok" and result["grid_fit"] >= 4:
            offsets.setdefault(extensions.get(track_id, ""), []).append(result["grid_offset"])
    every = [o for values in offsets.values() for o in values]
    default = float(np.median(every)) if len(every) >= 10 else DEFAULT_OFFSET
    calibrated = {ext: float(np.median(v)) for ext, v in offsets.items() if len(v) >= 10}
    return {ext: LOSSLESS_OFFSET for ext in LOSSLESS} | {"": default} | calibrated


def offset_for(offsets: dict[str, float], extension: str) -> float:
    return offsets.get(extension, offsets[""])


def new_grid(
    proposal: Proposal, offset: float, duration: float, first_downbeat: int | None = None
) -> list[tuple[int, float]]:
    """(beat number in the bar, rekordbox time in seconds) for the whole track."""
    period = 60 / proposal.bpm
    first = (proposal.phase - offset) % period
    start = proposal.downbeat if first_downbeat is None else first_downbeat
    return [((k - start) % 4 + 1, first + k * period) for k in range(int((duration - first) / period) + 1)]


def first_hit(env: np.ndarray) -> float:
    """The time of the first clear hit in the envelope: the first peak above a third of the loud peaks."""
    level = np.percentile(env, 99) / 3
    for i in range(1, len(env) - 1):
        if env[i] >= level and env[i] >= env[i - 1] and env[i] >= env[i + 1]:
            return i * HOP / SR
    return 0.0


def refit(env: np.ndarray, proposal: Proposal) -> Proposal:
    """The proposal with the fit of its own beats, after you move it."""
    times = np.arange(proposal.phase % (60 / proposal.bpm), len(env) * HOP / SR, 60 / proposal.bpm)
    return replace(proposal, fit=fit(env, times))


def _kicks(low: np.ndarray, bpm: float, phase: float) -> float:
    times = np.arange(phase, len(low) * HOP / SR, 60 / bpm)
    idx = np.round(times * SR / HOP).astype(int)
    window = np.stack([low[np.clip(idx + k, 0, len(low) - 1)] for k in range(-1, 5)])  # the low band peaks later
    return float(window.max(axis=0).mean())


def on_the_kick(low: np.ndarray, proposal: Proposal) -> Proposal:
    """The proposal, or the proposal half a beat later if the kicks are clearly there.

    The full band often prefers the hi-hats or claps on the off-beat.
    """
    half = 30 / proposal.bpm
    if _kicks(low, proposal.bpm, proposal.phase + half) <= KICK * _kicks(low, proposal.bpm, proposal.phase):
        return proposal
    return replace(proposal, phase=proposal.phase + half, downbeat=downbeat(low, proposal.bpm, proposal.phase + half))


def follow_old_grid(env: np.ndarray, old_times: list[float], offset: float, proposal: Proposal) -> Proposal:
    """The proposal, or the proposal half a beat later: the one on the beats of rekordbox where its grid fits best.

    For a grid that drifts, rekordbox found the beat but not the exact tempo. The onset strength often prefers
    the off-beat, so keep the choice of rekordbox, if its grid fits there.
    """
    times = np.asarray(old_times) + offset
    windows = [times[k:k + 32] for k in range(0, len(times) - 31, 16)]
    if not windows:
        return proposal
    best = max(windows, key=lambda w: fit(env, w))
    period = 60 / proposal.bpm
    beat = np.arange(np.ceil((best[0] - proposal.phase) / period), np.floor((best[-1] - proposal.phase) / period) + 1)
    if fit(env, best) < FOLLOW * fit(env, proposal.phase + beat * period):
        return proposal  # rekordbox does not find the beat anywhere
    shift = np.median(((best - proposal.phase) / period + 0.5) % 1 - 0.5)
    return replace(proposal, phase=proposal.phase + period / 2) if abs(shift) > 0.25 else proposal


def keep_bar_position(old: list[tuple[int, float]], new: list[tuple[int, float]]) -> int:
    """The new first downbeat that keeps the bars of the old grid: from its first downbeats."""
    votes = Counter()
    times = np.array([t for _, t in new])
    for number, t in old[:64]:
        if number == 1:
            votes[int(np.argmin(np.abs(times - t))) % 4] += 1
    return votes.most_common(1)[0][0] if votes else 0


def audio_time(rekordbox_time: float, offset: float) -> float:
    """The time in the audio that Essentia decodes, for a time of the rekordbox grid."""
    return rekordbox_time + offset - ENVELOPE_LAG


def load(path: str) -> np.ndarray:
    import essentia.standard as es

    return es.MonoLoader(filename=path, sampleRate=SR)()


def preview(audio: np.ndarray, start: float, seconds: float, clicks: list[tuple[int, float]], target: str) -> None:
    """Write a WAV file: the part of the track, with a click on each beat. Beat 1 of the bar has a higher click.

    `clicks`: (beat number in the bar, time in the audio).
    """
    import essentia.standard as es

    part = audio[int(start * SR): int((start + seconds) * SR)] * 0.6
    n = np.arange(int(0.03 * SR))
    for number, t in clicks:
        i = int((t - start) * SR)
        if 0 <= i < len(part):
            tone = np.sin(2 * np.pi * (1760 if number == 1 else 1100) * n / SR) * np.exp(-n / (0.006 * SR)) * 0.5
            end = min(i + len(tone), len(part))
            part[i:end] += tone[: end - i].astype(np.float32)
    es.MonoWriter(filename=target, format="wav", sampleRate=SR)(np.clip(part, -1, 1).astype(np.float32))
