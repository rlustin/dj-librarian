"""Double the BPM and the beat grid of tracks that rekordbox analysed at half tempo."""

from dj_librarian import grid, library, writer
from dj_librarian.commands.common import confirm, load_checks, matches, track_name
from dj_librarian.progress import Steps


def add_parser(sub) -> None:
    parser = sub.add_parser("double-bpm", help="double the BPM and beat grid of tracks at half the BPM of their genre")
    parser.add_argument("--apply", action="store_true", help="write the changes (rekordbox must be closed)")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--track", help="only the track whose 'artist - title' contains this text, or with this ID")
    parser.add_argument("--exclude", action="append", default=[],
                        help="do not change the tracks whose 'artist - title' contains this text (repeat for more)")
    parser.set_defaults(func=run)


def run(args) -> None:
    steps = Steps()
    db = library.open_db()
    _, _, _, report = load_checks(db, steps)
    found = [(t, genre, median) for t, genre, median in report.bpm if t.bpm < median]
    if args.track:
        found = [f for f in found if matches(f[0], args.track)]
    excluded = [f for f in found if any(x.lower() in track_name(f[0]).lower() for x in args.exclude)]
    found = [f for f in found if f not in excluded]
    changes, problems = [], []
    for t, genre, median in found:
        try:
            changes.append(grid.prepare(db, t, genre, median))
        except grid.GridError as error:
            problems.append((t, str(error)))

    print(f"\n{len(changes)} tracks have half the BPM of their genre. dj-librarian doubles the BPM and the beat grid:")
    for c in changes:
        print(f"  {c.track.bpm:6.2f} -> {2 * c.track.bpm:6.2f} BPM  {track_name(c.track)}  "
              f"({c.genre}, median {c.median:.0f})")
    if excluded:
        print(f"\nexcluded with --exclude ({len(excluded)}):")
        for t, _, _ in excluded:
            print(f"  {track_name(t)}")
    if problems:
        print(f"\ndj-librarian does not change these tracks ({len(problems)}):")
        for t, problem in problems:
            print(f"  {track_name(t)}: {problem}")
    if not changes:
        return
    if not args.apply:
        print("\nTo make these changes: dj-librarian double-bpm --apply (rekordbox must be closed). "
              "To try one track first, add --track.")
        return
    if not args.yes and not confirm(f"\ndouble the BPM of {len(changes)} tracks?"):
        print("cancelled. dj-librarian wrote nothing.")
        return

    writer.ensure_rekordbox_closed()
    backup = writer.backup(writer.db_path())
    writer.backup_analysis(backup, [p for c in changes for p in (c.dat, c.ext)])
    print(f"backup (master.db and analysis files): {backup}")
    print(f"written: {grid.apply(db, changes)} tracks")
    print("\nNext, open rekordbox. Make sure that the BPM, the beat grid, the phrases and the cues are correct.\n"
          "to undo: dj-librarian restore (restores the backup above)")
