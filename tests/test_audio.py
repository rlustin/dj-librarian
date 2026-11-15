import urllib.request
import wave

import numpy as np
import pytest

from dj_librarian import audio
from dj_librarian.audio import ENVELOPE_HOP_S, SR


def _sine(hz: float, seconds: float, amplitude: float = 0.5) -> np.ndarray:
    return (amplitude * np.sin(2 * np.pi * hz * np.arange(int(SR * seconds)) / SR)).astype(np.float32)


def test_audio_start_is_where_the_sound_starts():
    audio_data = np.concatenate([np.zeros(SR * 2, np.float32), _sine(440, 3, 0.01), _sine(440, 5, 1.0)])

    assert audio.audio_start(audio_data) == pytest.approx(2.0, abs=0.01)  # A quiet intro counts


def test_audio_start_ignores_noise_below_the_threshold():
    audio_data = np.concatenate([_sine(440, 2, 0.001), _sine(440, 5, 1.0)])

    assert audio.audio_start(audio_data) == pytest.approx(2.0, abs=0.01)


def test_envelopes_have_one_value_each_100_ms_and_find_the_bass():
    bass_full, bass_low = audio._envelopes(_sine(60, 10))
    treble_full, treble_low = audio._envelopes(_sine(5000, 10))

    assert len(bass_full) == len(bass_low) == pytest.approx(10 / ENVELOPE_HOP_S, abs=2)
    assert bass_full.dtype == np.float16
    assert np.median(bass_full - bass_low) < 1  # All the sound is in the sub/kick band
    assert np.median(treble_full - treble_low) > 40


def test_output_finds_the_name_from_its_purpose():
    meta = {"schema": {"outputs": [{"name": "embeddings", "output_purpose": "embeddings"},
                                   {"name": "model/Softmax", "output_purpose": "predictions"}]}}

    assert audio._output(meta, "predictions") == "model/Softmax"


def test_download_models_gets_only_the_files_that_it_does_not_have(monkeypatch, tmp_path):
    monkeypatch.setattr(audio, "MODELS_DIR", tmp_path)
    missing = "msd-musicnn-1.pb"
    for name in audio.MODEL_FILES:
        for ext in ("pb", "json"):
            if f"{name}.{ext}" != missing:
                (tmp_path / f"{name}.{ext}").write_bytes(b"model")
    downloads = []
    monkeypatch.setattr(urllib.request, "urlretrieve",
                        lambda url, target: downloads.append(url) or target.write_bytes(b"model"))

    audio.download_models()

    assert downloads == [f"{audio.MODELS_URL}/feature-extractors/musicnn/{missing}"]
    assert (tmp_path / missing).read_bytes() == b"model"


def test_analyze_refuses_a_short_file(tmp_path):
    path = tmp_path / "short.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes((_sine(440, 2) * 32767).astype(np.int16).tobytes())
    analyzer = audio.Analyzer.__new__(audio.Analyzer)  # The check comes before the models are necessary

    with pytest.raises(ValueError, match="audio shorter than 10 seconds"):
        analyzer.analyze(str(path))
