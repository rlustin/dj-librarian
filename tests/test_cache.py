import unicodedata

import numpy as np
import pytest

from dj_librarian import cache
from dj_librarian.cache import Cache
from dj_librarian.library import AudioFile

FILE = AudioFile("/music/Café.mp3", 1000, 1700000000.0)
RESULT = {"bpm": 124.0, "embedding": np.ones(4), "env_full": np.zeros(3), "env_low": np.zeros(3)}


@pytest.fixture
def stored(tmp_path):
    analyses = Cache(tmp_path / "analysis.sqlite")
    analyses.put("1", FILE, RESULT)
    yield analyses
    analyses.conn.close()


def test_a_stored_analysis_comes_back(stored):
    result = stored.all()["1"]

    assert result["bpm"] == 124.0
    assert result["embedding"].tolist() == [1.0] * 4 and result["embedding"].dtype == np.float32


def test_an_analysis_is_fresh_for_the_same_file(stored):
    assert stored.is_fresh("1", FILE)
    # A network share can give the accents of the path in the other Unicode form
    assert stored.is_fresh("1", FILE._replace(path=unicodedata.normalize("NFD", FILE.path)))


@pytest.mark.parametrize("change", [{"size": 1001}, {"mtime": 1700000001.0}, {"path": "/music/Other.mp3"}])
def test_an_analysis_is_old_after_a_change_of_the_file(stored, change):
    assert not stored.is_fresh("1", FILE._replace(**change))


def test_an_unknown_track_has_no_fresh_analysis(stored):
    assert not stored.is_fresh("2", FILE)


def test_an_analysis_is_old_after_a_new_analysis_version(stored, monkeypatch):
    monkeypatch.setattr(cache, "ANALYSIS_VERSION", cache.ANALYSIS_VERSION + 1)

    assert not stored.is_fresh("1", FILE)
    assert stored.all() == {}
