import os
import urllib.request

import pytest

from dj_librarian import audio
from dj_librarian.commands import analyze


def test_a_stopped_download_leaves_no_model(monkeypatch, tmp_path):
    def stop(url, target):
        target.write_bytes(b"part of a model")
        raise KeyboardInterrupt

    monkeypatch.setattr(audio, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(urllib.request, "urlretrieve", stop)

    with pytest.raises(KeyboardInterrupt):
        audio.download_models()

    assert not any(p.suffix in (".pb", ".json") for p in tmp_path.iterdir())


def test_a_model_that_does_not_load_stops_the_analysis(monkeypatch):
    def broken():
        raise ValueError("bad graph")

    monkeypatch.setattr(os, "dup2", lambda *_: None)  # Keep the stderr of pytest
    monkeypatch.setattr(audio, "Analyzer", broken)
    monkeypatch.setattr(analyze, "_failed", None)

    analyze._init_worker()

    with pytest.raises(analyze.ModelError, match="bad graph"):
        analyze._analyze("1", "/music/1.mp3")
