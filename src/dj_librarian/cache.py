"""SQLite cache of raw audio analysis, keyed by rekordbox content ID."""

import json
import os
import sqlite3
import unicodedata
from pathlib import Path

import numpy as np

from dj_librarian.audio import ANALYSIS_VERSION
from dj_librarian.store import ANALYSIS_CACHE

ARRAYS = {"embedding": np.float32, "env_full": np.float16, "env_low": np.float16}


class Cache:
    def __init__(self, path: Path = ANALYSIS_CACHE) -> None:
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
        # The beat grid check (beats.check), keyed by the grid file and the audio file
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS grid_check (content_id TEXT PRIMARY KEY, files_key TEXT, data TEXT)"
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

    def structures(self) -> dict[str, tuple[str, dict | None]]:
        rows = self.conn.execute("SELECT content_id, files_key, data FROM structure")
        return {content_id: (key, json.loads(data)) for content_id, key, data in rows}

    def put_structures(self, entries: dict[str, tuple[str, dict | None]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO structure VALUES (?, ?, ?)",
            [(content_id, key, json.dumps(data)) for content_id, (key, data) in entries.items()],
        )
        self.conn.commit()

    def grid_checks(self) -> dict[str, tuple[str, dict]]:
        rows = self.conn.execute("SELECT content_id, files_key, data FROM grid_check")
        return {content_id: (key, json.loads(data)) for content_id, key, data in rows}

    def put_grid_check(self, content_id: str, key: str, result: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO grid_check VALUES (?, ?, ?)", (content_id, key, json.dumps(result)))
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
