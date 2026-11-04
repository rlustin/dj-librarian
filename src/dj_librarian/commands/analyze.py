"""Analyse the audio files in parallel processes, and keep the results in the cache."""

import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from dj_librarian import library
from dj_librarian.cache import Cache
from dj_librarian.store import MODELS_DIR

_analyzer = None
_failed = None  # Why the worker cannot load the models. Its stderr is off, so the error must go back as a result.


class ModelError(Exception):
    pass


def add_parser(sub) -> None:
    parser = sub.add_parser("analyze", help="analyse audio files (cached, resumable)")
    parser.add_argument("--playlist", help="only tracks of this playlist")
    parser.add_argument("--limit", type=int, help="analyse at most N tracks")
    parser.add_argument("--workers", type=int, default=6)
    parser.set_defaults(func=run)


def _init_worker() -> None:
    global _analyzer, _failed
    # TensorFlow writes start-up logs from C++, also with TF_CPP_MIN_LOG_LEVEL. Errors come back as results.
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stderr.fileno())
    from dj_librarian.audio import Analyzer

    try:
        _analyzer = Analyzer()
    except Exception as e:  # noqa: BLE001 - an error in an initializer only shows as BrokenProcessPool
        _failed = f"{type(e).__name__}: {e}"


def _analyze(track_id: str, path: str):
    if _failed:
        raise ModelError(_failed)
    try:
        return track_id, path, _analyzer.analyze(path), None
    except Exception as e:  # noqa: BLE001 - one bad file must not stop a multi-hour run
        return track_id, path, None, f"{type(e).__name__}: {e}"


def run(args) -> None:
    from dj_librarian.audio import download_models

    download_models()
    cache = Cache()
    tracks = library.load_tracks(library.open_db(), args.playlist, with_structure=False)
    files = {t.id: library.on_disk(t.path) for t in tracks}
    missing = [t for t in tracks if not files[t.id]]
    todo = [t for t in tracks if files[t.id] and not cache.is_fresh(t.id, files[t.id])]
    if args.limit is not None:
        todo = todo[: args.limit]
    print(f"{len(tracks)} tracks, {len(missing)} files not found, {len(todo)} to analyse")
    _fill_audio_start(cache, {t.id for t in tracks}, args.workers)
    if not todo:
        return

    started, failures = time.time(), []
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker) as pool:
        futures = [pool.submit(_analyze, t.id, files[t.id]) for t in todo]
        for done, future in enumerate(as_completed(futures), 1):
            try:
                track_id, path, result, error = future.result()
            except ModelError as e:
                pool.shutdown(cancel_futures=True)
                raise SystemExit(f"\ncannot load the analysis models: {e}\n"
                                 f"Remove the folder {MODELS_DIR}, then use analyze again.") from None
            if error:
                failures.append((path, error))
            else:
                cache.put(track_id, path, result)
            elapsed = time.time() - started
            eta = elapsed / done * (len(todo) - done)
            print(f"\r{done}/{len(todo)}  {elapsed / 60:.0f} min elapsed, ~{eta / 60:.0f} min left  ",
                  end="", flush=True)
    print()
    for path, error in failures:
        print(f"failed: {path}: {error}", file=sys.stderr)


def _fill_audio_start(cache: Cache, track_ids: set[str], workers: int) -> None:
    """For results from before the audio start detection: decode only the first seconds of each file."""
    from dj_librarian.audio import find_audio_start

    todo = [(i, p) for i, p in cache.missing("audio_start") if i in track_ids and library.on_disk(p)]
    if not todo:
        return
    print(f"find where the sound starts for {len(todo)} analysed tracks")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(find_audio_start, library.on_disk(p)): i for i, p in todo}
        for done, future in enumerate(as_completed(futures), 1):
            try:
                cache.set_field(futures[future], "audio_start", future.result())
            except Exception as e:  # noqa: BLE001 - one bad file must not stop the others
                print(f"\nfailed: {futures[future]}: {e}", file=sys.stderr)
            print(f"\r{done}/{len(todo)}", end="", flush=True)
    print()
