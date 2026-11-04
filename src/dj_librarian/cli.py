import argparse
import csv
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed

from dj_librarian import library, proposals, store, tagging
from dj_librarian.cache import Cache
from dj_librarian.player import SEEK_SECONDS, Player
from dj_librarian.progress import LiveStatus, Steps

_analyzer = None


def _init_worker() -> None:
    global _analyzer
    # TensorFlow writes start-up logs from C++, also with TF_CPP_MIN_LOG_LEVEL. Errors come back as results.
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stderr.fileno())
    from dj_librarian.audio import Analyzer

    _analyzer = Analyzer()


def _analyze(track_id: str, path: str):
    try:
        return track_id, path, _analyzer.analyze(path), None
    except Exception as e:  # noqa: BLE001 - one bad file must not stop a multi-hour run
        return track_id, path, None, f"{type(e).__name__}: {e}"


def cmd_analyze(args) -> None:
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
            track_id, path, result, error = future.result()
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


def _fmt_time(seconds: float | None) -> str:
    return "" if seconds is None else f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _fmt_cues(cues: dict) -> str:
    return " ".join(f"{slot}={_fmt_time(seconds)}" for slot, (_, seconds) in sorted(cues.items()))


def _select(db, args) -> tuple[list[proposals.Proposal], proposals.Context]:
    track_ids = None
    if getattr(args, "track", None):
        needle = args.track.lower()
        matches = [t for t in library.load_tracks(db, with_structure=False)
                   if t.id == args.track or needle in f"{t.artist} - {t.title}".lower()]
        if len(matches) != 1:
            listing = "\n".join(f"  {t.id}  {t.artist} - {t.title}  ({t.path})" for t in matches[:20])
            raise SystemExit(f"--track must match exactly one track, matched {len(matches)}\n{listing}")
        track_ids = {matches[0].id}
    selected, context = proposals.build(db, args.playlist, track_ids, steps=Steps())
    if not selected:
        raise SystemExit("none of the selected tracks has an analysis. Run `dj-librarian analyze` first.")
    return selected, context


def cmd_report(args) -> None:
    selected, context = _select(library.open_db(), args)
    stats = Counter()
    rows = []
    for p in selected:
        t, a = p.track, p.analysis
        stats[f"bpm {p.bpm_check}"] += 1
        stats[f"key {p.key_check}"] += 1
        genre_status = f"{p.genre_action} (pending)" if p.genre_pending else p.genre_action
        stats[f"genre {genre_status}"] += 1
        stats[f"energy {p.energy}"] += 1
        stats["cues to write"] += len(p.cues.writes)
        stats["cues to label"] += len(p.cues.labels)
        for reason in p.cues.skipped.values():
            stats[f"cue skipped: {reason.split(' on ')[0]}"] += 1
        rows.append({
            "artist": t.artist, "title": t.title,
            "bpm_rekordbox": t.bpm, "bpm_ours": round(a["bpm"], 2), "bpm_check": p.bpm_check,
            "key_rekordbox": tagging.to_camelot(t.key), "key_ours": p.key_ours, "key_check": p.key_check,
            "genre_current": t.genre or "", "genre_proposed": p.genre_guess.genre or "",
            "genre_confidence": round(p.genre_guess.confidence, 2), "genre_discogs_hint": p.genre_guess.discogs_hint,
            "genre_action": p.genre_action, "genre_to_write": p.genre_write or "",
            "energy": p.energy, "moods": "/".join(p.moods),
            "comment_current": t.comment or "", "comment_proposed": p.comment,
            "hot_cues_existing": " ".join(f"{s}={_fmt_time(v)}" for s, v in sorted(t.hot_cues.items())),
            **{f"cue_{n.lower().replace(' ', '_')}": _fmt_time(p.cues.positions[n]) for n in tagging.CUE_NAMES},
            "rekordbox_first_chorus": _fmt_time(p.cues.rekordbox_chorus),
            "cues_to_write": _fmt_cues(p.cues.writes),
            "cues_skipped": "; ".join(f"{n}: {r}" for n, r in p.cues.skipped.items()),
            "cues_to_label": " ".join(f"{s}={n}" for s, n in sorted(p.cues.labels.items())),
            "path": t.path,
        })

    with open(args.out, "w", newline="") as f:
        writer_ = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer_.writeheader()
        writer_.writerows(rows)

    print(f"{len(rows)} tracks in report ({context.analysed}/{context.total} analysed in total) -> {args.out}\n")
    for group in ("bpm", "key", "genre", "energy", "cue"):
        items = sorted((k, v) for k, v in stats.items() if k.startswith(group))
        if group == "energy":
            items.sort(key=lambda kv: int(kv[0].split()[1]))
        print("  " + ",  ".join(f"{k}: {v}" for k, v in items))
    _print_genre_accuracy(context.tracks, context.genres)


def _load_checks(db, steps: Steps):
    from dj_librarian import checks

    steps.step("Load library and analysis")
    analysis = Cache().all()
    tracks = [t for t in library.load_tracks(db, with_structure=False) if t.id in analysis]
    decisions = proposals.load_genre_decisions()
    yours = checks.your_genres(tracks, proposals.load_guessed_genres(), decisions)
    current = {t.id: g for t in tracks if (g := t.genre if decisions.get(t.id) is None else decisions[t.id])}
    steps.step("Compare the tracks")
    report = checks.run(tracks, analysis, yours, current)
    steps.done()
    return tracks, analysis, yours, report


def _name(t) -> str:
    return f"{t.artist} - {t.title}"


def _neighbours(counter: Counter) -> str:
    return ", ".join(f"{g} {n}" for g, n in counter.most_common(3))


def cmd_check(args) -> None:
    from dj_librarian import checks, dedupe

    db = library.open_db()
    tracks, analysis, yours, report = _load_checks(db, Steps())
    rows = []
    if args.genre:
        members = checks.crate(tracks, analysis, yours, args.genre)
        if not members:
            raise SystemExit(f'no track has the genre "{args.genre}". Genres: {", ".join(sorted(report.genres))}')
        stats = report.genres[yours[members[0][0].id]]
        bpm = f", median {stats.median_bpm:.0f} BPM" if stats.median_bpm else ""
        print(f"\n{stats.name}: {stats.tracks} tracks{bpm}. On average, {stats.consistency:.0%} of the "
              f"{tagging.KNN_K} nearest tracks also have this genre.")
        print(f"It mixes most with: {_neighbours(Counter(dict(stats.mixed_with)))}")
        print("Most typical sound first. ! = none of the nearest tracks has this genre.\n")
        for n, (t, score) in enumerate(members, 1):
            nearest = report.neighbours.get(t.id, Counter())
            mark = "!" if nearest and not nearest[stats.name] else " "
            print(f"  {n:4d} {mark} {score:.2f}  {_name(t)}  ({t.bpm or '?'} BPM)  nearest: {_neighbours(nearest)}")
            rows.append({"kind": "crate", "genre": stats.name, "track": _name(t), "detail":
                         f"typical {score:.2f}; nearest: {_neighbours(nearest)}", "id": t.id, "path": t.path})
        _write_rows(args.out, rows)
        return

    def section(title: str, items: list, line, row) -> None:
        print(f"\n{title}")
        for item in items[: args.limit]:
            print(f"  {line(item)}")
        if len(items) > args.limit:
            print(f"  ... and {len(items) - args.limit} more (see {args.out})")
        rows.extend(row(item) for item in items)

    lengths = {c.ID: c.Length for c in db.get_content().all()}
    target = db.get_playlist(Name=dedupe.DUPLICATES_PLAYLIST).first()
    to_remove = {s.ContentID for s in db.get_playlist_songs(PlaylistID=target.ID)} if target else set()
    copies = [d for d in report.duplicates if dedupe.same_track(d.a, d.b, lengths[d.a.id], lengths[d.b.id])]
    versions = [d for d in report.duplicates if d not in copies]
    waiting = sum(d.a.id in to_remove or d.b.id in to_remove for d in copies)
    different = sum(d.genres[0] != d.genres[1] for d in copies)
    pair_line = lambda d: f"{_name(d.a)} [{d.genres[0] or '-'}]  =  {_name(d.b)} [{d.genres[1] or '-'}]"

    def pair_row(kind):
        return lambda d: {"kind": kind, "genre": f"{d.genres[0] or ''} / {d.genres[1] or ''}",
                          "track": f"{_name(d.a)} = {_name(d.b)}", "detail": f"similarity {d.similarity:.3f}",
                          "id": f"{d.a.id} {d.b.id}", "path": f"{d.a.path} | {d.b.path}"}

    todo = [f"{len(copies) - waiting} to merge with `dj-librarian dedupe`"] if len(copies) > waiting else []
    if waiting:
        todo.append(f'{waiting} merged, in the playlist "{dedupe.DUPLICATES_PLAYLIST}": '
                    "remove them from the collection in rekordbox")
    section(f"Duplicates: {len(copies)} pairs of copies of the same track, {different} of them with different genres. "
            + ". ".join(todo) + ".", copies, pair_line, pair_row("duplicate"))
    section(f"Other versions with the same sound: {len(versions)} pairs (edit, extended, instrumental, "
            "other length). dj-librarian does not merge them.", versions, pair_line, pair_row("other version"))

    names = ([("same name, other spelling", ", ".join(f"{n} ({c})" for n, c in v)) for v in report.variants]
             + [("not a valid name", f"{n!r} ({c})") for n, c in report.invalid]
             + [("on 1 or 2 tracks only", f"{n} ({c})") for n, c in report.rare])
    section(f"Genre names to check: {len(names)}", names, lambda n: f"{n[0]}: {n[1]}",
            lambda n: {"kind": "genre name", "genre": n[1], "track": "", "detail": n[0], "id": "", "path": ""})

    section(f"Tracks that do not sound like their genre: {len(report.outliers)}. None of their "
            f"{tagging.KNN_K} nearest tagged tracks has the same genre. Listen to them with "
            "`dj-librarian review-genres --outliers`.",
            report.outliers,
            lambda o: f"{_name(o.track)}: {o.genre}. Nearest: {_neighbours(o.neighbours)}",
            lambda o: {"kind": "outlier", "genre": o.genre, "track": _name(o.track),
                       "detail": f"nearest: {_neighbours(o.neighbours)}", "id": o.track.id, "path": o.track.path})

    genres = sorted((g for g in report.genres.values() if g.tracks >= checks.MIN_GENRE_TRACKS),
                    key=lambda g: g.consistency)
    print(f"\nGenre consistency: the share of the {tagging.KNN_K} nearest tracks with the same genre "
          "(low: the genre does not have one sound)")
    for g in genres:
        bpm = f"{g.median_bpm:5.0f} BPM" if g.median_bpm else "    ? BPM"
        print(f"  {g.name[:24]:24s} {g.tracks:4d} tracks  {g.consistency:4.0%}  {bpm}  "
              f"mixes with: {_neighbours(Counter(dict(g.mixed_with)))}")
    print("Genres that mix most: " + ", ".join(f"{a} + {b} ({n})" for (a, b), n in report.mixed_pairs[:6]))
    rows.extend({"kind": "genre", "genre": g.name, "track": "", "detail":
                 f"{g.tracks} tracks; consistency {g.consistency:.2f}; median BPM {g.median_bpm or ''}",
                 "id": "", "path": ""} for g in genres)

    section(f"BPM at half or double the median BPM of the genre: {len(report.bpm)}", report.bpm,
            lambda b: f"{_name(b[0])}: {b[1]}, {b[0].bpm} BPM (median of the genre: {b[2]:.0f})",
            lambda b: {"kind": "bpm", "genre": b[1], "track": _name(b[0]),
                       "detail": f"{b[0].bpm} BPM, median of the genre {b[2]:.0f}", "id": b[0].id, "path": b[0].path})
    _write_rows(args.out, rows)


def _write_rows(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        writer_ = csv.DictWriter(f, fieldnames=["kind", "genre", "track", "detail", "id", "path"])
        writer_.writeheader()
        writer_.writerows(rows)
    print(f"\n{len(rows)} rows -> {path}")


def cmd_dedupe(args) -> None:
    from dj_librarian import checks, dedupe, writer

    steps = Steps()
    steps.step("Load library and analysis")
    db = library.open_db()
    analysis = Cache().all()
    tracks = library.load_tracks(db, with_structure=False)
    decisions = proposals.load_genre_decisions()
    yours = checks.your_genres(tracks, proposals.load_guessed_genres(), decisions)
    generated = proposals.load_generated_cues()
    steps.step("Find the copies")
    groups, other_length = dedupe.find(db, tracks, analysis, yours, generated)
    steps.step("Find the missing files")
    moved, alone = dedupe.find_moved(db, tracks, yours, generated)
    steps.done()
    if alone:
        print(f"\n{len(alone)} files are missing, and no other entry has the same file. "
              "If you moved them, relocate them in rekordbox:")
        for t in alone[: args.limit]:
            print(f"  {_name(t)}  ({t.path})")
        if len(alone) > args.limit:
            print(f"  ... and {len(alone) - args.limit} more")
    groups = moved + groups
    if not groups:
        print("no duplicates")
        return

    extras = sum(len(g.extras) for g in groups)
    entries = sum(len([n for n in c.playlists if n != dedupe.DUPLICATES_PLAYLIST]) for g in groups for c in g.extras)
    print(f"\n{len(groups)} tracks have more than one copy. dj-librarian keeps one copy of each and puts it in the "
          f"playlists of the other copies ({entries} playlist entries).\n"
          f"{len(moved)} of them have an entry with a missing file, and an entry with the same file at a new place. "
          "rekordbox cannot relocate these.\n"
          f"{other_length} pairs have the same sound but another length or version (for example, instrumental). "
          "dj-librarian does not merge them.\n")
    rows = []
    for g in groups:
        shown = len(rows) < args.limit
        if shown:
            print(f"  {_name(g.keeper.track)}  (keep: {g.reason})")
        for role, c in [("keep", g.keeper)] + [("remove", c) for c in g.extras]:
            if shown:
                print(f"    {role:6s} {_short_path(c.track.path)}  ·  {c.plays} plays, {c.own_cues} cues of yours  ·  "
                      f"{', '.join(c.playlists) or 'no playlist'}")
            rows.append({"track": _name(g.keeper.track), "role": role, "reason": g.reason if role == "keep" else "",
                         "plays": c.plays, "your_cues": c.own_cues, "genre": c.genre or "",
                         "playlists": "; ".join(c.playlists), "id": c.track.id, "path": c.track.path})
        if shown and g.cues:
            print(f"    cues: {len(g.cues)} cues of yours move to the kept copy")
        if shown and g.genre:
            print(f"    genre: the kept copy gets your genre {g.genre}")
        elif shown and g.conflict:
            print(f"    genre: {g.conflict}. The kept copy keeps {g.keeper.genre}.")
    if len(groups) > args.limit:
        print(f"  ... and {len(groups) - args.limit} more")
    with open(args.out, "w", newline="") as f:
        writer_ = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer_.writeheader()
        writer_.writerows(rows)
    conflicts = sum(bool(g.conflict) for g in groups)
    print(f"\n{extras} copies to remove, {conflicts} tracks with two different genres. All copies -> {args.out}")
    if not args.apply:
        print("To make these changes: dj-librarian dedupe --apply (rekordbox must be closed)")
        return
    if not args.yes and input("\nwrite these changes? [y/N] ").strip().lower() != "y":
        print("cancelled. dj-librarian wrote nothing.")
        return

    if args.db:
        import pyrekordbox.masterdb.database as masterdb

        masterdb.get_rekordbox_pid = lambda *a, **k: 0
        target = library.MasterDatabase(path=args.db)
    else:
        writer.ensure_rekordbox_closed()
        target = db
        print(f"backup: {writer.backup(_db_path())}")
    stats = dedupe.apply(target, groups)
    print("written: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    genres = {g.keeper.track.id: g.genre for g in groups if g.genre}
    if genres and not args.db:
        # The next `apply` writes them, as if you gave them in review-genres
        proposals.save_genre_decisions(proposals.load_genre_decisions() | genres)
        print(f"{len(genres)} kept copies get the genre of another copy at the next `dj-librarian apply`.")
    print(f'\nNext, in rekordbox: open the playlist "{dedupe.DUPLICATES_PLAYLIST}", select all tracks, '
          "and use Remove from Collection. The files stay on the disk.")
    if not args.db:
        print("to undo: dj-librarian restore (restores the backup above)")


def cmd_double_bpm(args) -> None:
    from dj_librarian import grid, writer

    steps = Steps()
    db = library.open_db()
    _, _, _, report = _load_checks(db, steps)
    found = [(t, genre, median) for t, genre, median in report.bpm if t.bpm < median]
    if args.track:
        needle = args.track.lower()
        found = [f for f in found if f[0].id == args.track or needle in _name(f[0]).lower()]
    excluded = [f for f in found if any(x.lower() in _name(f[0]).lower() for x in args.exclude)]
    found = [f for f in found if f not in excluded]
    changes, problems = [], []
    for t, genre, median in found:
        try:
            changes.append(grid.prepare(db, t, genre, median))
        except grid.GridError as error:
            problems.append((t, str(error)))

    print(f"\n{len(changes)} tracks have half the BPM of their genre. dj-librarian doubles the BPM and the beat grid:")
    for c in changes:
        print(f"  {c.track.bpm:6.2f} -> {2 * c.track.bpm:6.2f} BPM  {_name(c.track)}  ({c.genre}, median {c.median:.0f})")
    if excluded:
        print(f"\nexcluded with --exclude ({len(excluded)}):")
        for t, _, _ in excluded:
            print(f"  {_name(t)}")
    if problems:
        print(f"\ndj-librarian does not change these tracks ({len(problems)}):")
        for t, problem in problems:
            print(f"  {_name(t)}: {problem}")
    if not changes:
        return
    if not args.apply:
        print("\nTo make these changes: dj-librarian double-bpm --apply (rekordbox must be closed). "
              "To try one track first, add --track.")
        return
    if not args.yes and input(f"\ndouble the BPM of {len(changes)} tracks? [y/N] ").strip().lower() != "y":
        print("cancelled. dj-librarian wrote nothing.")
        return

    writer.ensure_rekordbox_closed()
    backup = writer.backup(_db_path())
    writer.backup_analysis(backup, [p for c in changes for p in (c.dat, c.ext)])
    print(f"backup (master.db and analysis files): {backup}")
    print(f"written: {grid.apply(db, changes)} tracks")
    print("\nNext, open rekordbox. Make sure that the BPM, the beat grid, the phrases and the cues are correct.\n"
          "to undo: dj-librarian restore (restores the backup above)")


def _short_path(path: str) -> str:
    return "/".join(path.split("/")[-3:])


def cmd_review_genres(args) -> None:
    db = library.open_db()
    selected, context = proposals.build(db, args.playlist, with_cues=False, steps=Steps())
    decisions = proposals.load_genre_decisions()
    guessed = proposals.load_guessed_genres()
    used = Counter(t.genre for t in context.tracks if t.genre and guessed.get(t.id) != t.genre)
    used.update(g for g in decisions.values() if g)
    # If a name has two spellings, the most used one comes last and wins
    names = {g.lower(): g for g, _ in reversed(used.most_common())}
    reviews = [p for p in selected if p.genre_pending and p.genre_action == "review"]
    outliers = {}
    if args.outliers:
        from dj_librarian import checks

        _, _, _, report = _load_checks(db, Steps())
        outliers = {o.track.id: o for o in report.outliers
                    if not args.genre or o.genre.lower() == args.genre.lower()}
        # Earlier answers do not count: most come from a suggestion, before this check existed
        reviewed = checks.load_outliers_reviewed()
        reviews = [p for p in selected if p.track.id in outliers
                   and reviewed.get(p.track.id) != outliers[p.track.id].genre]
        order = {i: n for n, i in enumerate(outliers)}
        reviews.sort(key=lambda p: order[p.track.id])
    # Claude's answers first, most confident first: high-confidence answers matched your tags 9 times in 10
    rank = {"high": 0, "medium": 1, "low": 2}
    suggestions = sorted(
        (p for p in selected if p.genre_pending and p.genre_action == "suggest" and not args.outliers),
        key=lambda p: (0, rank[p.claude["confidence"]]) if p.claude else (1, -p.genre_guess.confidence),
    )
    pending = reviews + suggestions
    if not pending:
        print("no genre change to review")
        return
    player = Player(enabled=not args.no_play)
    answered = approved_now = 0
    from_claude = sum(bool(p.claude) for p in suggestions)
    if args.outliers:
        print(f"{len(reviews)} tracks that do not sound like their genre, the most different first.")
    else:
        print(f"{len(reviews)} tracks where your genre is different from similar tracks, then "
              f"{len(suggestions)} tracks without a genre or with an old guess of dj-librarian "
              f"({from_claude} with a suggestion from Claude, first).")
    print("dj-librarian saves each answer. You can stop at any time and continue later.")
    if player.available:
        print(f"Each track plays from its full-energy part: > / < to move {SEEK_SECONDS}s, r to restart.")
    print()
    try:
        for n, p in enumerate(pending, 1):
            candidates = p.genre_guess.candidates
            print(f"[{n}/{len(pending)}] {p.track.artist} - {p.track.title}")
            outlier = outliers.get(p.track.id)
            # An approved answer is the current genre, also before `apply` writes it
            current_genre = outlier.genre if outlier else p.track.genre
            old_guess = not outlier and bool(current_genre) and guessed.get(p.track.id) == current_genre
            current = f"{current_genre} (old guess of dj-librarian)" if old_guess else current_genre or "(empty)"
            print(f"    current: {current}    Discogs model: {p.genre_guess.discogs_hint}")
            if outlier:
                print(f"    nearest tagged tracks: {_neighbours(outlier.neighbours)}")
            if p.claude:
                new = ", new genre" if p.claude["is_new_genre"] else ""
                agrees = ", same as yours" if outlier and p.claude["genre"] == current_genre else ""
                print(f"    c. {p.claude['genre']}  (Claude, {p.claude['confidence']}{new}{agrees}): {p.claude['reason']}")
                if p.claude["alternatives"]:
                    print(f"       also possible: {', '.join(p.claude['alternatives'])}")
            for k, (genre, share) in enumerate(candidates, 1):
                print(f"    {k}. {genre}  ({share:.0%} of the most similar tracks)")
            start = _preview_start(db, p)
            if player.available and not player.play(p.track.path, start):
                print(f"    (cannot play: file not found. Make sure that its drive is mounted: {p.track.path})")
            keep = "[k]eep current" if current_genre else "[k]eep empty"
            pick = f"{'c, ' if p.claude else ''}1-{len(candidates)}"
            prompt = f"    {pick} to pick, {keep}, [s]kip, [q]uit, or type a genre: "
            while True:
                answer = input(prompt).strip()
                if answer in (">", "<", "r"):
                    if answer == "r":
                        player.play(p.track.path, start)
                    else:
                        player.seek(SEEK_SECONDS if answer == ">" else -SEEK_SECONDS)
                    if player.available:
                        print(f"    play from {_fmt_time(player.offset)}")
                    continue
                choices = {"q", "s", "k", ""} | ({"c"} if p.claude else set())
                if answer.lower() in choices or answer.isdigit() and 1 <= int(answer) <= len(candidates):
                    break
                if typed := _typed_genre(answer, names):
                    answer = typed
                    break
            player.stop()
            if answer.lower() == "q":
                break
            if answer.lower() == "s" or not answer:
                continue
            answered += 1
            if answer.lower() == "c" and p.claude:
                decisions[p.track.id] = p.claude["genre"]
            elif answer.isdigit() and 1 <= int(answer) <= len(candidates):
                decisions[p.track.id] = candidates[int(answer) - 1][0]
            elif answer.lower() == "k" and outlier:
                decisions.setdefault(p.track.id, None)
            elif answer.lower() == "k":
                # Keep an old guess: it becomes your genre, so dj-librarian learns from it
                decisions[p.track.id] = current_genre if old_guess else None
            else:
                decisions[p.track.id] = answer
            approved_now += decisions[p.track.id] not in (None, current_genre)
            proposals.save_genre_decision(p.track.id, decisions[p.track.id])
            if outlier:
                checks.save_outlier_reviewed(p.track.id, decisions[p.track.id] or current_genre)
    finally:
        player.stop()
    decisions = proposals.load_genre_decisions()
    to_write = sum(decisions.get(p.track.id) not in (None, p.track.genre or "") for p in selected)
    print(f"\nThis session: {answered} answers, {approved_now} genres approved. "
          f"The next `dj-librarian apply` writes {to_write} genre changes from your answers.")


def cmd_ask_claude(args) -> None:
    import random
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    from dj_librarian import claude_genres

    db = library.open_db()
    steps = Steps()
    selected, context = proposals.build(db, args.playlist, with_cues=False, steps=steps)
    by_id = {t.id: t for t in context.tracks}
    guessed = proposals.load_guessed_genres()
    decisions = proposals.load_genre_decisions()
    approved = {i: g for i, g in decisions.items() if g is not None}
    yours = lambda t: approved[t.id] if t.id in approved else (t.genre if guessed.get(t.id) != t.genre else None)
    vocab = claude_genres.vocabulary(context.tracks, yours)
    peers = claude_genres.Peers(context.tracks, yours)
    tags = sum(bool(yours(t)) for t in context.tracks)
    if not store.SOURCE_URLS.exists():
        steps.step("Get the Bandcamp links from the backups (one time)")
    urls = claude_genres.source_urls()
    steps.done()
    web = not args.no_web

    outliers: dict[str, str] = {}
    if args.self_check:
        # Tracks you tagged yourself, genre hidden: how often does Claude agree with you?
        tagged = [p for p in selected if yours(p.track)]
        todo = random.Random(0).sample(tagged, min(args.self_check, len(tagged)))
    else:
        from dj_librarian import checks

        answers = claude_genres.load_answers()

        def ask(p: proposals.Proposal, outlier: bool = False) -> bool:
            answer = answers.get(p.track.id)
            if answer is None or outlier and not answer.get("outlier"):
                return True
            return args.refresh and claude_genres.is_stale(answer, tags)

        # Tracks without a genre or with an old guess, and without a confident guess now
        todo = [p for p in selected if p.genre_action in ("unknown", "suggest") and p.track.id not in decisions
                and ask(p)]
        _, _, _, report = _load_checks(db, Steps(quiet=True))
        reviewed = checks.load_outliers_reviewed()
        # Tracks that do not sound like your genre, and that you did not check in `review-genres --outliers`
        outliers = {o.track.id: o.genre for o in report.outliers if reviewed.get(o.track.id) != o.genre}
        more = [p for p in selected if p.track.id in outliers and ask(p, outlier=True)]
        print(f"{len(todo)} tracks without a genre, {len(more)} tracks that do not sound like your genre")
        todo += more
        if args.limit is not None:
            todo = todo[: args.limit]
    todo.sort(key=lambda p: p.track.path)  # tracks of the same release go together
    batches = [todo[i:i + claude_genres.BATCH_SIZE] for i in range(0, len(todo), claude_genres.BATCH_SIZE)]
    print(f"{len(todo)} tracks in {len(batches)} calls to Claude ({args.model}, web search {'on' if web else 'off'})")
    if not todo:
        return

    status = LiveStatus(f"ask-claude ({args.model}, web search {'on' if web else 'off'})",
                        total_jobs=len(batches), total_items=len(todo))

    def run(job: int, batch: list[proposals.Proposal]):
        status.start(job, _batch_label(batch), len(batch))
        blocks = [claude_genres.describe(p, by_id, urls.get(p.track.id), peers, outliers.get(p.track.id))
                  for p in batch]
        prompt = claude_genres.build_prompt(blocks, vocab)
        return claude_genres.ask(prompt, args.model, web, on_activity=lambda text: status.activity(job, text))

    results: dict[str, dict] = {}
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {pool.submit(run, job, batch): (job, batch) for job, batch in enumerate(batches)}
    pending = set(futures)
    try:
        while pending:
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            for future in done:
                job, batch = futures[future]
                try:
                    answers_ = future.result()
                except Exception as e:  # noqa: BLE001 - one failed call must not stop the others
                    status.finish(job, [], error=str(e)[:120])
                    continue
                titles = {p.track.id: f"{p.track.artist} - {p.track.title}" for p in batch}
                lines = []
                for answer in answers_:
                    if answer["id"] in titles:
                        results[answer["id"]] = answer | {
                            "model": args.model, "version": claude_genres.PROMPT_VERSION, "tags": tags,
                            "outlier": answer["id"] in outliers}
                        lines.append(f"{titles[answer['id']]} → {answer['genre']} ({answer['confidence']})")
                if not args.self_check:
                    claude_genres.save_answers(claude_genres.load_answers() | results)
                status.finish(job, lines)
            status.render()
    except KeyboardInterrupt:
        print("\nstop: cancel the queued calls and stop the calls in progress...")
        pool.shutdown(wait=False, cancel_futures=True)
        claude_genres.stop_all()
        if not args.self_check:
            print(f"dj-librarian saved {len(results)} answers. Run the command again to continue.")
        return
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    if args.self_check:
        _print_claude_self_check([p for p in todo if p.track.id in results], results, yours)
    else:
        new = Counter(a["genre"] for a in results.values() if a["is_new_genre"])
        print(f"dj-librarian saved {len(results)} answers. "
              "Review them with `dj-librarian review-genres` (candidate c).")
        if new:
            print("new genres proposed: " + ", ".join(f"{g} ({n})" for g, n in new.most_common()))


def _typed_genre(text: str, names: dict[str, str]) -> str | None:
    """Your spelling of a genre that you use, or a new genre after you confirm it. None: ask again."""
    if not text.isprintable():
        print("    this is not a genre name: it contains control characters (for example, an arrow key)")
        return None
    if text.lower() in names:
        return names[text.lower()]
    if input(f'    "{text}" is a new genre. Type y to add it: ').strip().lower() == "y":
        names[text.lower()] = text
        return text
    return None


def _batch_label(batch: list[proposals.Proposal]) -> str:
    folders = {p.track.path.rsplit("/", 1)[0] for p in batch}
    first = batch[0].track
    name = folders.pop().rsplit("/", 1)[-1] if len(folders) == 1 else f"{first.artist} - {first.title} and others"
    return f"{name} ({len(batch)} track{'s' if len(batch) > 1 else ''})"


def _print_claude_self_check(checked, results, yours) -> None:
    if not checked:
        return
    right = [p for p in checked if results[p.track.id]["genre"].lower() == yours(p.track).lower()]
    print(f"Claude matched your genre on {len(right)}/{len(checked)} tracks ({100 * len(right) / len(checked):.0f}%)")
    by_confidence = Counter((results[p.track.id]["confidence"], p in right) for p in checked)
    for level in ("high", "medium", "low"):
        total = by_confidence[(level, True)] + by_confidence[(level, False)]
        if total:
            print(f"  {level} confidence: {by_confidence[(level, True)]}/{total} right")
    print("disagreements (yours -> Claude):")
    for p in checked:
        if p not in right:
            a = results[p.track.id]
            print(f"  {p.track.artist} - {p.track.title}: {yours(p.track)} -> {a['genre']} ({a['confidence']}): {a['reason']}")


def _preview_start(db, p: proposals.Proposal) -> float:
    """The most representative part to listen to: full energy, else intro end, else a third in."""
    track = library.load_tracks(db, only={p.track.id})[0]  # beat grid of this track only (cached)
    positions = tagging.plan_cues(track, p.analysis).positions
    return positions.get("Full energy") or positions.get("Intro end") or p.analysis["duration"] / 3


def cmd_apply(args) -> None:
    from dj_librarian import writer

    parts = set(args.only.split(",")) if args.only else set(writer.PARTS)
    if unknown := parts - set(writer.PARTS):
        raise SystemExit(f"unknown parts: {', '.join(unknown)} (choose from {', '.join(writer.PARTS)})")
    if not args.db:
        writer.ensure_rekordbox_closed()

    db = library.open_db()
    selected, _ = _select(db, args)
    todo = [(p, c) for p in selected if (c := writer.changes(p, parts))]
    if not todo:
        print("nothing to change")
        return
    counts = Counter(part for _, c in todo for part in c)
    cue_count = sum(len(c["cues"]) for _, c in todo if "cues" in c)
    print(f"{len(todo)} tracks will change: genre {counts['genre']}, comment {counts['comment']}, "
          f"cues {cue_count} (on {counts['cues']} tracks), labels on your cues {counts['labels']}\n")
    for p, c in todo[:8]:
        print(f"  {p.track.artist} - {p.track.title}")
        if "genre" in c:
            print(f"      genre:   {p.track.genre or '(empty)'}  ->  {c['genre']}")
        if "comment" in c:
            print(f"      comment: {p.track.comment or '(empty)'}  ->  {c['comment']}")
        if "cues" in c:
            if p.cues.previous:
                print("      replaces: " + ", ".join(f"{s} {n} @ {_fmt_time(t)}" for s, (n, t) in sorted(p.cues.previous.items())))
            print("      cues:    " + ", ".join(f"{s} {n} @ {_fmt_time(t)}" for s, (n, t) in sorted(c["cues"].items())))
        if "labels" in c:
            print("      labels:  " + ", ".join(f"your {s} -> \"{n}\"" for s, n in sorted(c["labels"].items())))
    if len(todo) > 8:
        print(f"  ... and {len(todo) - 8} more (see `dj-librarian report`)")
    pending = sum(p.genre_pending for p in selected)
    if pending and "genre" in parts:
        print(f"\n{pending} genre changes wait for review. `apply` does not write them (`dj-librarian review-genres`).")

    if not args.yes and input("\nwrite these changes? [y/N] ").strip().lower() != "y":
        print("cancelled. dj-librarian wrote nothing.")
        return

    if args.db:
        # A test copy: rekordbox can be open, because it does not use that file
        import pyrekordbox.masterdb.database as masterdb

        masterdb.get_rekordbox_pid = lambda *a, **k: 0
        target = library.MasterDatabase(path=args.db)
    else:
        writer.ensure_rekordbox_closed()
        target = db
        print(f"backup: {writer.backup(_db_path())}")
    stats = writer.apply(target, [p for p, _ in todo], parts, record=not args.db)
    print("written: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    if not args.db:
        print("to undo: dj-librarian restore (restores the backup above)")


def _db_path():
    from pathlib import Path

    from pyrekordbox.config import get_config

    return Path(get_config("rekordbox7").get("db_path") or get_config("rekordbox6")["db_path"])


def cmd_restore(args) -> None:
    from dj_librarian import writer

    backups = sorted(p for p in store.BACKUP_DIR.iterdir() if p.is_dir()) if store.BACKUP_DIR.exists() else []
    if not backups:
        raise SystemExit("no backups found")
    chosen = store.BACKUP_DIR / args.backup if args.backup else backups[-1]
    writer.ensure_rekordbox_closed()
    if input(f"replace {_db_path()} with backup {chosen.name}? [y/N] ").strip().lower() != "y":
        print("cancelled")
        return
    writer.restore(chosen, _db_path())
    print("restored")


def _print_genre_accuracy(tracks, genres) -> None:
    """Leave-one-out: how often the learned guess matches a genre you set yourself."""
    tagged = [t for t in tracks if t.genre and t.id in genres]
    guessed = proposals.load_guessed_genres()
    tagged = [t for t in tagged if guessed.get(t.id) != t.genre]
    learned = [t for t in tagged if genres[t.id].genre]
    if not learned:
        return
    correct = sum(genres[t.id].genre.lower() == t.genre.lower() for t in learned)
    print(f"\n  genre self-check on your {len(tagged)} tagged tracks: confident guess for {len(learned)}, "
          f"of which {correct} ({100 * correct / len(learned):.0f}%) match your tag")


def main() -> None:
    parser = argparse.ArgumentParser(prog="dj-librarian", description="Metadata helper for rekordbox libraries")
    sub = parser.add_subparsers(required=True)

    analyze = sub.add_parser("analyze", help="analyse audio files (cached, resumable)")
    analyze.add_argument("--playlist", help="only tracks of this playlist")
    analyze.add_argument("--limit", type=int, help="analyse at most N tracks")
    analyze.add_argument("--workers", type=int, default=6)
    analyze.set_defaults(func=cmd_analyze)

    report = sub.add_parser("report", help="propose metadata (read-only) and write it to a CSV file")
    report.add_argument("--playlist", help="only tracks of this playlist")
    report.add_argument("--out", default="report.csv")
    report.add_argument("--track", help="only the track whose 'artist - title' contains this text, or with this ID")
    report.set_defaults(func=cmd_report)

    check = sub.add_parser("check", help="find duplicates, odd genre names, tracks unlike their genre (read-only)")
    check.add_argument("--genre", help="list the tracks of this genre, most typical sound first")
    check.add_argument("--limit", type=int, default=10, help="lines for each list (default: 10, all in the CSV)")
    check.add_argument("--out", default="check.csv")
    check.set_defaults(func=cmd_check)

    dedupe = sub.add_parser("dedupe", help="merge the copies of the same track into one (playlists included)")
    dedupe.add_argument("--apply", action="store_true", help="write the changes (rekordbox must be closed)")
    dedupe.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    dedupe.add_argument("--limit", type=int, default=10, help="groups to show (default: 10, all in the CSV)")
    dedupe.add_argument("--out", default="dedupe.csv")
    dedupe.add_argument("--db", help="write to this master.db copy instead of the real library (for tests)")
    dedupe.set_defaults(func=cmd_dedupe)

    double = sub.add_parser("double-bpm", help="double the BPM and beat grid of tracks at half the BPM of their genre")
    double.add_argument("--apply", action="store_true", help="write the changes (rekordbox must be closed)")
    double.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    double.add_argument("--track", help="only the track whose 'artist - title' contains this text, or with this ID")
    double.add_argument("--exclude", action="append", default=[],
                        help="do not change the tracks whose 'artist - title' contains this text (repeat for more)")
    double.set_defaults(func=cmd_double_bpm)

    review = sub.add_parser("review-genres", help="approve or reject proposed genre changes, interactively")
    review.add_argument("--playlist", help="only tracks of this playlist")
    review.add_argument("--no-play", action="store_true", help="do not play the tracks during the review")
    review.add_argument("--outliers", action="store_true",
                        help="instead, review the tracks that do not sound like their genre (see `dj-librarian check`)")
    review.add_argument("--genre", help="with --outliers: only the tracks of this genre")
    review.set_defaults(func=cmd_review_genres)

    apply = sub.add_parser("apply", help="write genre, comment and cues into rekordbox (rekordbox must be closed)")
    apply.add_argument("--playlist", help="only tracks of this playlist")
    apply.add_argument("--track", help="only the track whose 'artist - title' contains this text, or with this ID")
    apply.add_argument("--only", help="comma-separated subset of: genre,comment,cues")
    apply.add_argument("--db", help="write to this master.db copy instead of the real library (for tests)")
    apply.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    apply.set_defaults(func=cmd_apply)

    ask = sub.add_parser("ask-claude", help="ask Claude how to tag tracks without a confident genre guess, or unlike their genre")
    ask.add_argument("--playlist", help="only tracks of this playlist")
    ask.add_argument("--limit", type=int, help="ask about at most N tracks")
    ask.add_argument("--model", default="opus", help="Claude model alias (default: opus)")
    ask.add_argument("--no-web", action="store_true", help="do not let Claude search the web")
    ask.add_argument("--refresh", action="store_true",
                     help="also ask again if the prompt changed or you tagged 10%% more tracks since the answer")
    ask.add_argument("--workers", type=int, default=3, help="parallel Claude calls")
    ask.add_argument("--self-check", type=int, metavar="N",
                     help="instead, ask about N tracks you tagged (genre hidden) and report agreement")
    ask.set_defaults(func=cmd_ask_claude)

    restore = sub.add_parser("restore", help="restore master.db (and analysis files) from a dj-librarian backup (latest by default)")
    restore.add_argument("backup", nargs="?", help="backup folder name")
    restore.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    args.func(args)
