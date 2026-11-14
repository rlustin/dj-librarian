import os
import sys
import tempfile
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
