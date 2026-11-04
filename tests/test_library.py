import os
import unicodedata

from dj_librarian import library


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
