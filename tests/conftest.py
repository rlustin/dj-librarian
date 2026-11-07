import os
import sys
import tempfile
from pathlib import Path

import pytest

# Before dj-librarian is imported: the tests must never touch the files of your real library
os.environ["DJ_LIBRARIAN_HOME"] = tempfile.mkdtemp(prefix="dj-librarian-test-")
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An empty master.db for one test, with empty records of dj-librarian."""
    from pyrekordbox.masterdb import database

    from dj_librarian import store

    from helpers import empty_library

    # pyrekordbox does not write while rekordbox is open, also to another file
    monkeypatch.setattr(database, "get_rekordbox_pid", lambda *args, **kwargs: 0)
    for name, path in vars(store).items():  # The records of what dj-librarian wrote start empty
        if isinstance(path, Path) and path.suffix == ".json":
            monkeypatch.setattr(store, name, tmp_path / path.name)
    library = empty_library(tmp_path)
    yield library
    library.close()
