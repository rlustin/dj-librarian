"""Put a backup of master.db (and of the analysis files) back."""

from contextlib import suppress
from pathlib import Path

from dj_librarian import claude_genres, store, writer
from dj_librarian.commands.common import confirm, plural
from dj_librarian.terminal import warn


def add_parser(sub) -> None:
    parser = sub.add_parser(
        "restore", help="restore master.db (and analysis files) from a dj-librarian backup (latest by default)")
    parser.add_argument("backup", nargs="?", help="backup folder name (see --list)")
    parser.add_argument("--list", action="store_true", help="show the backups, then stop")
    parser.set_defaults(func=run)


def run(args) -> None:
    backups = writer.backups()
    if args.list:
        show(backups, writer.kept_backups())
        return
    if not backups and not args.backup:
        raise SystemExit("no backups that dj-librarian made. To see all the backups: dj-librarian restore --list")
    chosen = store.BACKUP_DIR / args.backup if args.backup else backups[-1]
    if not chosen.is_dir():
        raise SystemExit(f"no backup {args.backup}. To see the backups: dj-librarian restore --list")
    writer.ensure_rekordbox_closed()
    if missing := writer.grids_without_folder(chosen):
        print(warn(f"The folders of {plural(len(missing), 'analysis file')} do not exist now, because you removed "
                   "the tracks from rekordbox. restore cannot put these grids back. The first one:\n"
                   f"  {missing[0]}"))
    if not confirm(f"replace {writer.db_path()} with backup {chosen.name}?"):
        print("cancelled")
        return
    current = writer.restore(chosen, writer.db_path())
    print(f"restored. To undo: dj-librarian restore {current.name}")
    with suppress(OSError, ValueError):  # Else the deletion of this backup reads it later
        claude_genres.read_backup_urls(current)
    print("Your answers stay as they are. "
          f"The backup has a copy of your answers from {chosen.name}: {chosen / writer.RECORDS_DIR}"
          if (chosen / writer.RECORDS_DIR).exists() else
          "This backup has no copy of the records of dj-librarian. "
          "Thus dj-librarian can think that some of your genres are its guesses, and not learn from them.")


def show(backups: list[Path], kept: list[Path]) -> None:
    if not backups and not kept:
        print("no backups found")
        return
    first = writer.first_backup()
    for title, group in ((f"{store.BACKUP_DIR}, the oldest first:", backups),
                         ("Backups that you gave another name, the oldest first. dj-librarian keeps them:", kept)):
        if group:
            print(title)
        for backup in group:
            size = sum(f.stat().st_size for f in backup.rglob("*") if f.is_file()) / 1e6
            notes = [_grids(backup),
                     "kept: the first backup" if backup == first else ""]
            line = f"  {backup.name:20s} {writer.backup_command(backup) or '?':12s} {size:5.0f} MB  "
            print((line + ", ".join(note for note in notes if note)).rstrip())
    print("To keep a backup, give its folder another name. dj-librarian does not delete it then.")


def _grids(backup: Path) -> str:
    try:
        grids = len(store.read_json(backup / writer.ANALYSIS_MANIFEST, {}))
    except (OSError, ValueError):
        return f"damaged {writer.ANALYSIS_MANIFEST}"
    return plural(grids, "analysis file") if grids else ""
