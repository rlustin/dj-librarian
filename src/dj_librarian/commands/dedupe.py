"""Merge the copies of the same track, and the entries of moved files."""

from dj_librarian import dedupe, library, proposals
from dj_librarian.cache import Cache
from dj_librarian.commands.common import confirm, track_name, write_csv, write_target
from dj_librarian.progress import Steps


def add_parser(sub) -> None:
    parser = sub.add_parser("dedupe", help="merge the copies of the same track into one (playlists included)")
    parser.add_argument("--apply", action="store_true", help="write the changes (rekordbox must be closed)")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--limit", type=int, default=10, help="groups to show (default: 10, all in the CSV)")
    parser.add_argument("--out", default="dedupe.csv")
    parser.add_argument("--db", help="write to this master.db copy instead of the real library (for tests)")
    parser.set_defaults(func=run)


def run(args) -> None:
    steps = Steps()
    steps.step("Load library and analysis")
    db = library.open_db()
    analysis = Cache().all()
    tracks = library.load_tracks(db, with_structure=False)
    decisions = proposals.load_genre_decisions()
    yours = proposals.your_genres(tracks, proposals.load_guessed_genres(), decisions)
    generated = proposals.load_generated_cues()
    steps.step("Find the missing files")
    missing = dedupe.missing_files(tracks)
    moved, alone = dedupe.find_moved(db, tracks, yours, generated, missing)
    steps.step("Find the copies")
    groups, other_length = dedupe.find(db, tracks, analysis, yours, generated, missing)
    steps.done()
    # A copy that gets the playlists of a missing entry must not also go to the duplicates playlist
    kept = {g.keeper.track.id for g in moved}
    waits = [any(c.track.id in kept for c in [g.keeper, *g.extras]) for g in groups]
    later = sum(waits)
    groups = [g for g, wait in zip(groups, waits) if not wait]
    if alone:
        print(f"\n{len(alone)} files are missing, and no other entry has the same file. "
              "If you moved them, relocate them in rekordbox:")
        for t in alone[: args.limit]:
            print(f"  {track_name(t)}  ({t.path})")
        if len(alone) > args.limit:
            print(f"  ... and {len(alone) - args.limit} more")
    if later:
        print(f"\n{later} tracks have a missing entry and more than one copy on the disk. "
              "Remove the duplicates below in rekordbox, then use dedupe again for these tracks.")
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
            print(f"  {track_name(g.keeper.track)}  (keep: {g.reason})")
        for role, c in [("keep", g.keeper)] + [("remove", c) for c in g.extras]:
            if shown:
                print(f"    {role:6s} {_short_path(c.track.path)}  ·  {c.plays} plays, {c.own_cues} cues of yours  ·  "
                      f"{', '.join(c.playlists) or 'no playlist'}")
            rows.append({"track": track_name(g.keeper.track), "role": role,
                         "reason": g.reason if role == "keep" else "", "plays": c.plays, "your_cues": c.own_cues, "genre": c.genre or "",
                         "playlists": "; ".join(c.playlists), "id": c.track.id, "path": c.track.path})
        if shown and g.cues:
            print(f"    cues: {len(g.cues)} cues of yours move to the kept copy")
        if shown and g.genre:
            print(f"    genre: the kept copy gets your genre {g.genre}")
        elif shown and g.conflict:
            print(f"    genre: {g.conflict}. The kept copy keeps {g.keeper.genre}.")
    if len(groups) > args.limit:
        print(f"  ... and {len(groups) - args.limit} more")
    write_csv(args.out, rows)
    conflicts = sum(bool(g.conflict) for g in groups)
    print(f"\n{extras} copies to remove, {conflicts} tracks with two different genres. All copies -> {args.out}")
    if not args.apply:
        print("To make these changes: dj-librarian dedupe --apply (rekordbox must be closed)")
        return
    if not args.yes and not confirm("\nwrite these changes?"):
        print("cancelled. dj-librarian wrote nothing.")
        return

    stats = dedupe.apply(write_target(db, args.db), groups)
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


def _short_path(path: str) -> str:
    return "/".join(path.split("/")[-3:])
