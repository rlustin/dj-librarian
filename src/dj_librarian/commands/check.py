"""Show what looks wrong in the library. Read-only."""

from collections import Counter

from dj_librarian import checks, dedupe, library, tagging
from dj_librarian.commands.common import load_checks, neighbours, track_name, write_csv
from dj_librarian.progress import Steps

CSV_FIELDS = ["kind", "genre", "track", "detail", "id", "path"]


def add_parser(sub) -> None:
    parser = sub.add_parser("check", help="find duplicates, odd genre names, tracks unlike their genre (read-only)")
    parser.add_argument("--genre", help="list the tracks of this genre, most typical sound first")
    parser.add_argument("--limit", type=int, default=10, help="lines for each list (default: 10, all in the CSV)")
    parser.add_argument("--out", default="check.csv")
    parser.set_defaults(func=run)


def run(args) -> None:
    db = library.open_db()
    tracks, analysis, yours, report = load_checks(db, Steps())
    rows = []
    if args.genre:
        members = checks.crate(tracks, analysis, yours, args.genre)
        if not members:
            raise SystemExit(f'no track has the genre "{args.genre}". Genres: {", ".join(sorted(report.genres))}')
        stats = report.genres[yours[members[0][0].id]]
        bpm = f", median {stats.median_bpm:.0f} BPM" if stats.median_bpm else ""
        print(f"\n{stats.name}: {stats.tracks} tracks{bpm}. On average, {stats.consistency:.0%} of the "
              f"{tagging.KNN_K} nearest tracks also have this genre.")
        print(f"It mixes most with: {neighbours(Counter(dict(stats.mixed_with)))}")
        print("Most typical sound first. ! = none of the nearest tracks has this genre.\n")
        for n, (t, score) in enumerate(members, 1):
            nearest = report.neighbours.get(t.id, Counter())
            mark = "!" if nearest and not nearest[stats.name] else " "
            print(f"  {n:4d} {mark} {score:.2f}  {track_name(t)}  ({t.bpm or '?'} BPM)  nearest: {neighbours(nearest)}")
            rows.append({"kind": "crate", "genre": stats.name, "track": track_name(t), "detail":
                         f"typical {score:.2f}; nearest: {neighbours(nearest)}", "id": t.id, "path": t.path})
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
    pair_line = lambda d: f"{track_name(d.a)} [{d.genres[0] or '-'}]  =  {track_name(d.b)} [{d.genres[1] or '-'}]"

    def pair_row(kind):
        return lambda d: {"kind": kind, "genre": f"{d.genres[0] or ''} / {d.genres[1] or ''}",
                          "track": f"{track_name(d.a)} = {track_name(d.b)}", "detail": f"similarity {d.similarity:.3f}",
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
            lambda o: f"{track_name(o.track)}: {o.genre}. Nearest: {neighbours(o.neighbours)}",
            lambda o: {"kind": "outlier", "genre": o.genre, "track": track_name(o.track),
                       "detail": f"nearest: {neighbours(o.neighbours)}", "id": o.track.id, "path": o.track.path})

    genres = sorted((g for g in report.genres.values() if g.tracks >= checks.MIN_GENRE_TRACKS),
                    key=lambda g: g.consistency)
    print(f"\nGenre consistency: the share of the {tagging.KNN_K} nearest tracks with the same genre "
          "(low: the genre does not have one sound)")
    for g in genres:
        bpm = f"{g.median_bpm:5.0f} BPM" if g.median_bpm else "    ? BPM"
        print(f"  {g.name[:24]:24s} {g.tracks:4d} tracks  {g.consistency:4.0%}  {bpm}  "
              f"mixes with: {neighbours(Counter(dict(g.mixed_with)))}")
    print("Genres that mix most: " + ", ".join(f"{a} + {b} ({n})" for (a, b), n in report.mixed_pairs[:6]))
    rows.extend({"kind": "genre", "genre": g.name, "track": "", "detail":
                 f"{g.tracks} tracks; consistency {g.consistency:.2f}; median BPM {g.median_bpm or ''}",
                 "id": "", "path": ""} for g in genres)

    section(f"BPM at half or double the median BPM of the genre: {len(report.bpm)}", report.bpm,
            lambda b: f"{track_name(b[0])}: {b[1]}, {b[0].bpm} BPM (median of the genre: {b[2]:.0f})",
            lambda b: {"kind": "bpm", "genre": b[1], "track": track_name(b[0]),
                       "detail": f"{b[0].bpm} BPM, median of the genre {b[2]:.0f}", "id": b[0].id, "path": b[0].path})
    _write_rows(args.out, rows)


def _write_rows(path: str, rows: list[dict]) -> None:
    write_csv(path, rows, CSV_FIELDS)
    print(f"\n{len(rows)} rows -> {path}")
