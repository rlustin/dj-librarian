import os
import sys
import tempfile
from functools import partial
from pathlib import Path

import pytest

# Before dj-librarian is imported: the tests must never touch the files of your real library
os.environ["DJ_LIBRARIAN_HOME"] = tempfile.mkdtemp(prefix="dj-librarian-test-")
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Empty records of dj-librarian and no backups, for one test."""
    from dj_librarian import store

    for name, path in vars(store).items():
        if isinstance(path, Path) and path.suffix == ".json":
            monkeypatch.setattr(store, name, tmp_path / path.name)
    monkeypatch.setattr(store, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(store, "ANALYSIS_CACHE", tmp_path / "analysis.sqlite")
    return tmp_path


@pytest.fixture
def db(home, monkeypatch):
    """An empty master.db for one test, with empty records of dj-librarian."""
    from pyrekordbox.masterdb import database

    from helpers import empty_library

    # pyrekordbox does not write while rekordbox is open, also to another file
    monkeypatch.setattr(database, "get_rekordbox_pid", lambda *args, **kwargs: 0)
    library = empty_library(home)
    yield library
    library.close()


@pytest.fixture
def unencrypted(db, monkeypatch):
    """The library of the `db` fixture has no encryption. dj-librarian opens the real one with encryption."""
    from dj_librarian import library, writer

    for module in (library, writer):
        monkeypatch.setattr(module, "MasterDatabase", partial(module.MasterDatabase, unlock=False))
    return db


@pytest.fixture
def analysed(unencrypted, home, monkeypatch):
    """The analysis cache of the library of the `db` fixture. The commands open and write this library."""
    from dj_librarian import library, writer
    from dj_librarian.cache import Cache

    monkeypatch.setattr(library, "open_db", lambda: unencrypted)
    # Else a command makes its backup from the master.db of your real rekordbox
    monkeypatch.setattr(writer, "db_path", lambda: home / "master.db")
    monkeypatch.setattr(writer, "get_rekordbox_pid", lambda: 0)
    (home / "share").mkdir()  # pyrekordbox looks for the analysis files of the tracks in this folder
    analyses = Cache()
    yield analyses
    analyses.conn.close()
