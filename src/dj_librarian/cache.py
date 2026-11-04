"""SQLite cache of raw audio analysis, keyed by rekordbox content ID."""

import json
import os
import sqlite3
import unicodedata
from pathlib import Path

import numpy as np

from dj_librarian.audio import ANALYSIS_VERSION

CACHE_PATH = Path.home() / ".cache" / "dj-librarian" / "analysis.sqlite"
ARRAYS = {"embedding": np.float32, "env_full": np.float16, "env_low": np.float16}


class Cache:
    def __init__(self, path: Path = CACHE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS analysis (
                content_id TEXT PRIMARY KEY, path TEXT, size INTEGER, mtime REAL, version INTEGER,
                data TEXT, embedding BLOB, env_full BLOB, env_low BLOB)"""
        )
        # rekordbox's beat grid and phrases, keyed by the analysis files' modification times
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS structure (content_id TEXT PRIMARY KEY, files_key TEXT, data TEXT)"
        )

    @staticmethod
    def fingerprint(path: str) -> tuple[int, float]:
        stat = os.stat(path)
        return stat.st_size, stat.st_mtime

    def is_fresh(self, content_id: str, path: str) -> bool:
        row = self.conn.execute(
            "SELECT path, size, mtime, version FROM analysis WHERE content_id = ?", (content_id,)
        ).fetchone()
        if row is None:
            return False
        # The same file can come back with its accents in another Unicode form (see library.on_disk)
        same_path = unicodedata.normalize("NFC", row[0]) == unicodedata.normalize("NFC", path)
        return same_path and row[1:] == (*self.fingerprint(path), ANALYSIS_VERSION)

    def put(self, content_id: str, path: str, result: dict) -> None:
        data = {k: v for k, v in result.items() if k not in ARRAYS}
        blobs = [np.asarray(result[k], dtype=t).tobytes() for k, t in ARRAYS.items()]
        self.conn.execute(
            "INSERT OR REPLACE INTO analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (content_id, path, *self.fingerprint(path), ANALYSIS_VERSION, json.dumps(data), *blobs),
        )
        self.conn.commit()

    def missing(self, key: str) -> list[tuple[str, str]]:
        """(content ID, path) of cached results without this field, for analysis added later."""
        rows = self.conn.execute("SELECT content_id, path, data FROM analysis WHERE version = ?", (ANALYSIS_VERSION,))
        return [(content_id, path) for content_id, path, data in rows if key not in json.loads(data)]

    def set_field(self, content_id: str, key: str, value) -> None:
        (data,) = self.conn.execute("SELECT data FROM analysis WHERE content_id = ?", (content_id,)).fetchone()
        data = json.loads(data) | {key: value}
        self.conn.execute("UPDATE analysis SET data = ? WHERE content_id = ?", (json.dumps(data), content_id))
        self.conn.commit()

    def structures(self) -> dict[str, tuple[str, dict | None]]:
        rows = self.conn.execute("SELECT content_id, files_key, data FROM structure")
        return {content_id: (key, json.loads(data)) for content_id, key, data in rows}

    def put_structures(self, entries: dict[str, tuple[str, dict | None]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO structure VALUES (?, ?, ?)",
            [(content_id, key, json.dumps(data)) for content_id, (key, data) in entries.items()],
        )
        self.conn.commit()

    def all(self) -> dict[str, dict]:
        results = {}
        rows = self.conn.execute(
            "SELECT content_id, data, embedding, env_full, env_low FROM analysis WHERE version = ?",
            (ANALYSIS_VERSION,),
        )
        for content_id, data, *blobs in rows:
            result = json.loads(data)
            for (name, dtype), blob in zip(ARRAYS.items(), blobs):
                result[name] = np.frombuffer(blob, dtype=dtype).astype(np.float32)
            results[content_id] = result
        return results
