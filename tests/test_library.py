import os
import time
import unicodedata

import pytest

from dj_librarian import library

from helpers import track


def test_on_disk_finds_the_file(tmp_path):
    path = tmp_path / "track.mp3"
    path.write_bytes(b"")

    assert library.on_disk(str(path)) == str(path)
    assert library.on_disk(str(tmp_path / "missing.mp3")) is None


def test_on_disk_finds_the_file_with_accents_in_the_other_form(monkeypatch):
    nfc = unicodedata.normalize("NFC", "/music/Décembre/track.mp3")
    nfd = unicodedata.normalize("NFD", nfc)
    monkeypatch.setattr(os.path, "exists", lambda path: path == nfd)

    assert library.on_disk(nfc) == nfd


def test_find_files_gives_the_size_and_date(tmp_path):
    there = tmp_path / "there.mp3"
    there.write_bytes(b"audio")

    files = library.find_files([track("1", path=str(there)), track("2", path=str(tmp_path / "missing.mp3"))])

    assert files == {"1": library.AudioFile(str(there), 5, there.stat().st_mtime)}


def test_find_files_counts_a_file_that_goes_away_as_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(library, "on_disk", lambda path: path)  # The file was there, then it moved

    assert library.find_files([track("1", path=str(tmp_path / "moved.mp3"))]) == {}


def test_find_files_stops_at_once_after_ctrl_c(monkeypatch):
    started = []

    def slow(path):
        started.append(path)
        time.sleep(0.01)

    class Interrupt:
        def count(self, done, total):
            raise KeyboardInterrupt

    monkeypatch.setattr(library, "on_disk", slow)
    tracks = [track(str(n)) for n in range(500)]

    with pytest.raises(KeyboardInterrupt):
        library.find_files(tracks, Interrupt())

    assert len(started) < 100  # pool.map cancels the queue when the loop stops
