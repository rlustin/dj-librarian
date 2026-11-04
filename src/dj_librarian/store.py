"""The files that dj-librarian keeps between runs: the analysis cache, your answers, the records of its writes,
and the backups. Set DJ_LIBRARIAN_HOME to keep them in another folder.
"""

import json
import os
from pathlib import Path

HOME = Path(os.environ.get("DJ_LIBRARIAN_HOME") or Path.home() / ".cache" / "dj-librarian")
MODELS_DIR = HOME / "models"
BACKUP_DIR = HOME / "backups"
ANALYSIS_CACHE = HOME / "analysis.sqlite"
GENRE_DECISIONS = HOME / "genre_decisions.json"
GUESSED_GENRES = HOME / "generated_genres.json"
GENERATED_CUES = HOME / "generated_cues.json"
OUTLIERS_REVIEWED = HOME / "outliers_reviewed.json"
CLAUDE_ANSWERS = HOME / "claude_genres.json"
SOURCE_URLS = HOME / "source_urls.json"


def read_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path: Path, data) -> None:
    """If dj-librarian stops during the write, the old file stays complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    temporary.replace(path)
