"""Audio analysis with Essentia: tempo, key, model predictions and loudness over time.

The results depend only on the audio file. `tagging.py` makes the decisions that compare
a track with the library (genre, mood, energy, cue points). You can tune these decisions
without a new audio analysis.
"""

import json
import os
from pathlib import Path

import numpy as np

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

import essentia  # noqa: E402

essentia.log.warningActive = False
essentia.log.infoActive = False

import essentia.standard as es  # noqa: E402

MODELS_DIR = Path.home() / ".cache" / "dj-librarian" / "models"
MODELS_URL = "https://essentia.upf.edu/models"
MODEL_FILES = {
    "discogs-effnet-bs64-1": "feature-extractors/discogs-effnet",
    "genre_discogs400-discogs-effnet-1": "classification-heads/genre_discogs400",
    "mtg_jamendo_moodtheme-discogs-effnet-1": "classification-heads/mtg_jamendo_moodtheme",
    "mood_aggressive-discogs-effnet-1": "classification-heads/mood_aggressive",
    "mood_happy-discogs-effnet-1": "classification-heads/mood_happy",
    "mood_party-discogs-effnet-1": "classification-heads/mood_party",
    "mood_relaxed-discogs-effnet-1": "classification-heads/mood_relaxed",
    "mood_sad-discogs-effnet-1": "classification-heads/mood_sad",
    "danceability-discogs-effnet-1": "classification-heads/danceability",
    "msd-musicnn-1": "feature-extractors/musicnn",
    "deam-msd-musicnn-2": "classification-heads/deam",
}
BINARY_HEADS = ["aggressive", "happy", "party", "relaxed", "sad"]

# Increase this number when the analysis output changes. Then `analyze` does the analysis again.
ANALYSIS_VERSION = 2

SR = 44100
AUDIO_START_WINDOW_S = 30
AUDIO_START_THRESHOLD = 0.005  # Of the peak amplitude in the first 30 s (-46 dB). Quiet intros count.
MODEL_SR = 16000
ENVELOPE_HOP_S = 0.1


def download_models() -> None:
    import urllib.request

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for name, folder in MODEL_FILES.items():
        for ext in ("pb", "json"):
            target = MODELS_DIR / f"{name}.{ext}"
            if not target.exists():
                print(f"download model: {target.name}")
                # If the download stops, a part of the file must not look like a model at the next run
                temporary = target.with_name(target.name + ".tmp")
                urllib.request.urlretrieve(f"{MODELS_URL}/{folder}/{name}.{ext}", temporary)
                temporary.replace(target)


def _meta(name: str) -> dict:
    return json.loads((MODELS_DIR / f"{name}.json").read_text())


def _output(meta: dict, purpose: str) -> str:
    return next(o["name"] for o in meta["schema"]["outputs"] if o.get("output_purpose") == purpose)


class Analyzer:
    """Load the models one time. Use the same instance for all files."""

    def __init__(self) -> None:
        download_models()
        graph = lambda name: str(MODELS_DIR / f"{name}.pb")  # noqa: E731

        effnet_meta = _meta("discogs-effnet-bs64-1")
        self.effnet = es.TensorflowPredictEffnetDiscogs(
            graphFilename=graph("discogs-effnet-bs64-1"), output=_output(effnet_meta, "embeddings")
        )

        def head(name: str) -> es.TensorflowPredict2D:
            meta = _meta(name)
            return es.TensorflowPredict2D(
                graphFilename=graph(name),
                input=meta["schema"]["inputs"][0]["name"],
                output=_output(meta, "predictions"),
            )

        self.genre_head = head("genre_discogs400-discogs-effnet-1")
        self.genre_labels = _meta("genre_discogs400-discogs-effnet-1")["classes"]
        self.moodtheme_head = head("mtg_jamendo_moodtheme-discogs-effnet-1")
        self.moodtheme_labels = _meta("mtg_jamendo_moodtheme-discogs-effnet-1")["classes"]
        self.binary_heads = {}
        for mood in BINARY_HEADS:
            name = f"mood_{mood}-discogs-effnet-1"
            self.binary_heads[mood] = (head(name), _meta(name)["classes"].index(mood))
        self.danceability_head = head("danceability-discogs-effnet-1")
        self.danceable_index = _meta("danceability-discogs-effnet-1")["classes"].index("danceable")

        musicnn_meta = _meta("msd-musicnn-1")
        self.musicnn = es.TensorflowPredictMusiCNN(
            graphFilename=graph("msd-musicnn-1"), output=_output(musicnn_meta, "embeddings")
        )
        self.deam_head = head("deam-msd-musicnn-2")
        self.deam_labels = _meta("deam-msd-musicnn-2")["classes"]

        self.resample = es.Resample(inputSampleRate=SR, outputSampleRate=MODEL_SR, quality=1)
        self.rhythm = es.RhythmExtractor2013(method="degara")
        self.key = es.KeyExtractor(profileType="edmm")  # Best agreement with rekordbox on a sample of 24 tracks

    def analyze(self, path: str) -> dict:
        audio = es.MonoLoader(filename=path, sampleRate=SR)()
        if len(audio) < SR * 10:
            raise ValueError("audio shorter than 10 seconds")
        audio16 = self.resample(audio)

        bpm, _, bpm_confidence, _, _ = self.rhythm(audio)
        key, scale, key_strength = self.key(audio)

        embeddings = self.effnet(audio16)
        genre = self.genre_head(embeddings).mean(axis=0)
        top_genres = np.argsort(genre)[::-1][:10]
        moodtheme = self.moodtheme_head(embeddings).mean(axis=0)
        moods = {
            mood: float(model(embeddings)[:, index].mean())
            for mood, (model, index) in self.binary_heads.items()
        }
        danceability = float(self.danceability_head(embeddings)[:, self.danceable_index].mean())
        valence_arousal = self.deam_head(self.musicnn(audio16)).mean(axis=0)
        env_full, env_low = _envelopes(audio)

        return {
            "duration": len(audio) / SR,
            "audio_start": audio_start(audio),
            "bpm": float(bpm),
            "bpm_confidence": float(bpm_confidence),
            "key": key,
            "scale": scale,
            "key_strength": float(key_strength),
            "genres": {self.genre_labels[i]: round(float(genre[i]), 4) for i in top_genres},
            "moodtheme": {label: round(float(p), 4) for label, p in zip(self.moodtheme_labels, moodtheme)},
            "moods": moods,
            "danceability": danceability,
            "valence": float(valence_arousal[self.deam_labels.index("valence")]),
            "arousal": float(valence_arousal[self.deam_labels.index("arousal")]),
            "onset_rate": float(es.OnsetRate()(audio)[1]),
            # The cache keeps these arrays as binary data, not as JSON
            "embedding": embeddings.mean(axis=0).astype(np.float32),
            "env_full": env_full,
            "env_low": env_low,
        }


def audio_start(audio: np.ndarray) -> float:
    """Time (seconds) where the sound starts. This time can be before the first beat of the rekordbox grid."""
    opening = np.abs(audio[: SR * AUDIO_START_WINDOW_S])
    return float(np.argmax(opening >= opening.max() * AUDIO_START_THRESHOLD) / SR)


def find_audio_start(path: str) -> float:
    """Same as `audio_start`, but decode only the first seconds of the file."""
    return audio_start(es.EasyLoader(filename=path, sampleRate=SR, endTime=AUDIO_START_WINDOW_S)())


def _envelopes(audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Loudness over time (dB), full band and sub/kick band (20-150 Hz), one value per 100 ms."""
    frame_size = 4096
    hop = int(SR * ENVELOPE_HOP_S)
    window = es.Windowing(type="hann")
    spectrum = es.Spectrum()
    bin_hz = SR / frame_size
    low_bins = slice(int(20 / bin_hz), int(150 / bin_hz) + 1)
    full, low = [], []
    for frame in es.FrameGenerator(audio, frameSize=frame_size, hopSize=hop, startFromZero=True):
        power = spectrum(window(frame)) ** 2
        full.append(power.sum())
        low.append(power[low_bins].sum())
    to_db = lambda x: (10 * np.log10(np.asarray(x) + 1e-10)).astype(np.float16)  # noqa: E731
    return to_db(full), to_db(low)
