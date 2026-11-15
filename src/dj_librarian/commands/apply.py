"""Write the genres, comments and cues into master.db."""

from collections import Counter

from dj_librarian import library, writer
from dj_librarian.commands.common import clean_backups, confirm, fmt_time, select, write_target
from dj_librarian.terminal import plural


def add_parser(sub) -> None:
    parser = sub.add_parser("apply", help="write genre, comment and cues into rekordbox (rekordbox must be closed)")
    parser.add_argument("--playlist", help="only tracks of this playlist")
    parser.add_argument("--track", help="only the track whose 'artist - title' contains this text, or with this ID")
    parser.add_argument("--only", help="comma-separated subset of: genre,comment,cues")
    parser.add_argument("--db", help="write to this master.db copy instead of the real library (for tests)")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.set_defaults(func=run)


def run(args) -> None:
    parts = set(args.only.split(",")) if args.only else set(writer.PARTS)
    if unknown := parts - set(writer.PARTS):
        raise SystemExit(f"unknown parts: {', '.join(unknown)} (choose from {', '.join(writer.PARTS)})")
    if not args.db:
        writer.ensure_rekordbox_closed()

    db = library.open_db()
    selected, _ = select(db, args)
    todo = [(p, c) for p in selected if (c := writer.changes(p, parts))]
    if not todo:
        print("nothing to change")
        return
    counts = Counter(part for _, c in todo for part in c)
    cue_count = sum(len(c["cues"]) for _, c in todo if "cues" in c)
    print(f"{plural(len(todo), 'track')} will change: genre {counts['genre']}, comment {counts['comment']}, "
          f"cues {cue_count} (on {plural(counts['cues'], 'track')}), labels on your cues {counts['labels']}\n")
    for p, c in todo[:8]:
        print(f"  {p.track.artist} - {p.track.title}")
        if "genre" in c:
            print(f"      genre:   {p.track.genre or '(empty)'}  ->  {c['genre']}")
        if "comment" in c:
            print(f"      comment: {p.track.comment or '(empty)'}  ->  {c['comment']}")
        if "cues" in c:
            if p.cues.previous:
                print(f"      replaces: {_fmt_cues(p.cues.previous)}")
            print(f"      cues:    {_fmt_cues(c['cues'])}")
        if "labels" in c:
            print("      labels:  " + ", ".join(f"your {s} -> \"{n}\"" for s, n in sorted(c["labels"].items())))
    if len(todo) > 8:
        print(f"  ... and {len(todo) - 8} more (see `dj-librarian report`)")
    pending = sum(p.genre_pending for p in selected)
    if pending and "genre" in parts:
        waits, them = ("waits", "it") if pending == 1 else ("wait", "them")
        print(f"\n{plural(pending, 'genre change')} {waits} for review. "
              f"`apply` does not write {them} (`dj-librarian review-genres`).")

    if not args.yes and not confirm("\nwrite these changes?"):
        print("cancelled. dj-librarian wrote nothing.")
        return

    target = write_target(db, args.db, "apply")
    stats = writer.apply(target, [p for p, _ in todo], parts, record=not args.db)
    print("written: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    if not args.db:
        print("to undo: dj-librarian restore (restores the backup above)")
        clean_backups()


def _fmt_cues(cues: dict[str, tuple[str, float]]) -> str:
    return ", ".join(f"{slot} {name} @ {fmt_time(seconds)}" for slot, (name, seconds) in sorted(cues.items()))
