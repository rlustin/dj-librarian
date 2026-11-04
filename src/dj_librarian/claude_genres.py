"""Ask Claude, through the Claude Code CLI, how to tag tracks that the audio models cannot place.

dj-librarian keeps the answers in a cache. `review-genres` shows them only as suggestions.
"""

import json
import re
import shutil
import subprocess
import tempfile
import threading
from collections import Counter
from pathlib import Path

from dj_librarian import store, tagging
from dj_librarian.library import Track
from dj_librarian.proposals import Proposal

PROMPT_VERSION = 2  # Increase when the prompt changes. Then `ask-claude --refresh` asks about the tracks again.
REFRESH_TAG_GROWTH = 1.1  # `--refresh` also asks again if you tagged 10% more tracks since the answer
MODEL = "opus"
BATCH_SIZE = 8
TIMEOUT_SECONDS = 20 * 60
VOCABULARY_SIZE = 80
NEIGHBOURS = 5
PEER_GENRES = 4

QUESTION = "How should I tag it, to be able to match it with other songs in my DJ library?"

SYSTEM_PROMPT = f"""You help a DJ fill the Genre field of tracks in their rekordbox library.
The genre is used to find tracks that mix well together during a set, so it should group a track
with the DJ's other tracks of the same scene or style.

For each track you get its metadata, what audio analysis measured, the DJ's own note if any,
the most similar tracks (by sound) that the DJ already tagged, the genres the DJ gave to other
tracks of the same release, artist or label, and sometimes the release's page.
The genres of the same release are strong evidence. Those of the same artist or label are weaker,
because artists and labels often cover several styles.
You cannot listen to the audio. When unsure, use web search to look the release up (Bandcamp tags,
Discogs styles, label or Resident Advisor pages).

Rules:
- Prefer one of the DJ's existing genre names, spelled exactly as given.
- Only when none fits, propose a new genre (is_new_genre = true): a common, specific scene or style
  name (e.g. "Ambient", "Dubstep", "Grime"), never a broad umbrella like "Electronic" or "Dance".
- confidence: "high" if the release is clearly documented or the sound evidence is unambiguous,
  "medium" if it is a reasonable call, "low" if it is mostly a guess.
- reason: one or two short sentences a DJ would find useful (what it is, what it sits next to).
- alternatives: up to 2 other genre names that would also be defensible.
- Some tracks already have the DJ's genre ("current genre"), but do not sound like the DJ's other
  tracks of that genre. Answer the current genre if it is still the best fit for the DJ's sets,
  for example because the release or the scene supports it. Else, propose a better genre.
- Answer for every track id given. {QUESTION}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "tracks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "genre": {"type": "string"},
                    "is_new_genre": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "reason": {"type": "string"},
                    "alternatives": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "genre", "is_new_genre", "confidence", "reason", "alternatives"],
            },
        }
    },
    "required": ["tracks"],
}

URL = re.compile(r"https?://[\w.-]+\.bandcamp\.com\S*", re.IGNORECASE)


def load_answers() -> dict[str, dict]:
    return store.read_json(store.CLAUDE_ANSWERS, {})


def is_stale(answer: dict, tags: int) -> bool:
    """True if the prompt changed, or if you tagged many more tracks since Claude answered."""
    return answer.get("version") != PROMPT_VERSION or answer.get("tags", 0) * REFRESH_TAG_GROWTH < tags


def save_answers(answers: dict[str, dict]) -> None:
    store.write_json(store.CLAUDE_ANSWERS, answers)


def source_urls() -> dict[str, str]:
    """Bandcamp links that were in the comments before dj-librarian rewrote them, recovered from backups."""
    if store.SOURCE_URLS.exists():
        return store.read_json(store.SOURCE_URLS, {})
    from dj_librarian.library import MasterDatabase
    from dj_librarian.writer import DB_FILES

    urls: dict[str, str] = {}
    for backup in sorted(store.BACKUP_DIR.iterdir()) if store.BACKUP_DIR.exists() else []:
        with tempfile.TemporaryDirectory() as tmp:  # never open a backup in place
            for name in DB_FILES:
                if (backup / name).exists():
                    shutil.copy2(backup / name, Path(tmp) / name)
            for content in MasterDatabase(path=Path(tmp) / "master.db").get_content().all():
                if content.ID not in urls and (match := URL.search(content.Commnt or "")):
                    urls[content.ID] = match.group(0)
    store.write_json(store.SOURCE_URLS, urls)
    return urls


def vocabulary(tracks: list[Track], genre_of) -> list[tuple[str, int]]:
    return Counter(g for t in tracks if (g := genre_of(t))).most_common(VOCABULARY_SIZE)


class Peers:
    """Your genres for each release, artist and label."""

    FIELDS = (("album", "release"), ("artist", "artist"), ("label", "label"))

    def __init__(self, tracks: list[Track], genre_of) -> None:
        self.index: dict[tuple[str, str], dict[str, str]] = {}
        for t in tracks:
            if genre := genre_of(t):
                for field, _ in self.FIELDS:
                    if value := (getattr(t, field) or "").strip().lower():
                        self.index.setdefault((field, value), {})[t.id] = genre

    def describe(self, t: Track) -> str:
        parts = []
        for field, name in self.FIELDS:
            value = (getattr(t, field) or "").strip()
            # The track itself does not count: in `--self-check`, its genre is the answer
            genres = Counter(g for i, g in self.index.get((field, value.lower()), {}).items() if i != t.id)
            if genres:
                top = ", ".join(f"{g} ({n})" for g, n in genres.most_common(PEER_GENRES))
                parts.append(f'{name} "{value}": {top}')
        return "; ".join(parts)


def describe(p: Proposal, by_id: dict[str, Track], url: str | None, peers: Peers, current: str | None = None) -> str:
    """`current`: the genre that you gave, for a track that does not sound like its genre (`dj-librarian check`)."""
    t, a = p.track, p.analysis
    note = tagging.GENERATED_PREFIX.sub("", t.comment or "").strip()
    folder = t.path.rsplit("/", 2)[-2] if "/" in t.path else ""
    styles = ", ".join(f"{s.split('---')[-1]} {v:.2f}" for s, v in list(a["genres"].items())[:5])
    similar = "; ".join(
        f'"{by_id[i].artist} - {by_id[i].title}" ({genre}, similarity {sim:.2f})'
        for i, genre, sim in p.genre_guess.neighbours[:NEIGHBOURS]
        if i in by_id
    )
    lines = [
        f"id: {t.id}",
        (f"artist: {t.artist} | title: {t.title} | album: {t.album or '-'} | label: {t.label or '-'} "
         f"| year: {t.year or '-'} | folder: {folder}"),
        f"bpm: {t.bpm} | key: {tagging.to_camelot(t.key) or '-'} | energy: {p.energy}/10 | moods: {'/'.join(p.moods)}",
        f"audio style model (Discogs styles, 0-1): {styles}",
        f"most similar tracks the DJ tagged: {similar or '-'}",
        f"the DJ's genres for other tracks of the same release, artist or label: {peers.describe(t) or '-'}",
    ]
    if current:
        lines.append(f"current genre: {current}. None of the {tagging.KNN_K} most similar tracks that the DJ "
                     f"tagged has this genre.")
    if note:
        lines.append(f"DJ's note: {note}")
    if url:
        lines.append(f"release page: {url}")
    return "\n".join(lines)


def build_prompt(blocks: list[str], vocab: list[tuple[str, int]]) -> str:
    names = ", ".join(f"{g} ({n})" for g, n in vocab)
    tracks = "\n\n".join(blocks)
    return f"The DJ's genre names, with how many tracks use each: {names}\n\nTracks:\n\n{tracks}"


_running: set[subprocess.Popen] = set()


def stop_all() -> None:
    for process in list(_running):
        process.kill()


def ask(prompt: str, model: str = MODEL, web: bool = True, on_activity=None) -> list[dict]:
    """One Claude Code call. Return the answers for each track.

    dj-librarian calls `on_activity(text)` at each step of Claude: thoughts, web searches, pages.
    """
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("the `claude` CLI is not installed or not on PATH")
    report = on_activity or (lambda text: None)
    tools = "WebSearch,WebFetch" if web else ""
    command = [
        claude, "-p", prompt,
        "--model", model,
        "--system-prompt", SYSTEM_PROMPT,
        "--output-format", "stream-json", "--verbose",
        "--json-schema", json.dumps(SCHEMA),
        "--tools", tools,
        "--no-session-persistence",
        "--strict-mcp-config",
    ]
    if web:
        command += ["--allowedTools", tools]
    result = None
    # Empty directory: Claude Code loads no project instructions.
    # stderr goes to a file: a full stderr pipe stops the process while dj-librarian reads stdout.
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryFile("w+") as errors:
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=errors, text=True)
        _running.add(process)
        timer = threading.Timer(TIMEOUT_SECONDS, process.kill)
        timer.start()
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result":
                    result = event
                elif event.get("type") == "assistant":
                    for block in event["message"].get("content", []):
                        if text := _describe_step(block):
                            report(text)
            process.wait()
        finally:
            timer.cancel()
            _running.discard(process)
        errors.seek(0)
        if result is None:
            raise RuntimeError(f"claude stopped and gave no answer (exit {process.returncode}): {errors.read(300)}")
    if result.get("is_error") or not result.get("structured_output"):
        raise RuntimeError(f"claude failed: {str(result.get('result'))[:300]}")
    return result["structured_output"]["tracks"]


def _describe_step(block: dict) -> str | None:
    if block.get("type") == "thinking":
        return "thinks"
    if block.get("type") != "tool_use":
        return None
    tool_input = block.get("input", {})
    if block.get("name") == "WebSearch":
        return f'searches "{tool_input.get("query", "")}"'
    if block.get("name") == "WebFetch":
        url = tool_input.get("url", "")
        return f"reads {url.split('://', 1)[-1]}"
    return "writes the answers"
