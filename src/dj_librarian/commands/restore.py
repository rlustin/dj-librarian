"""Put a backup of master.db (and of the analysis files) back."""

from pathlib import Path

from dj_librarian import store, writer
from dj_librarian.commands.common import confirm, plural


def add_parser(sub) -> None:
    parser = sub.add_parser(
        "restore", help="restore master.db (and analysis files) from a dj-librarian backup (latest by default)")
    parser.add_argument("backup", nargs="?", help="backup folder name (see --list)")
    parser.add_argument("--list", action="store_true", help="show the backups, then stop")
    parser.set_defaults(func=run)


def run(args) -> None:
    backups = writer.backups()
    if not backups:
        raise SystemExit("no backups found")
    if args.list:
        show(backups)
        return
    chosen = store.BACKUP_DIR / args.backup if args.backup else backups[-1]
    if not chosen.is_dir():
        raise SystemExit(f"no backup {args.backup}. To see the backups: dj-librarian restore --list")
    writer.ensure_rekordbox_closed()
    if not confirm(f"replace {writer.db_path()} with backup {chosen.name}?"):
        print("cancelled")
        return
    current = writer.restore(chosen, writer.db_path())
    print(f"restored. To undo: dj-librarian restore {current.name}")
    print("Your answers stay as they are. "
          f"The backup has a copy of your answers from {chosen.name}: {chosen / writer.RECORDS_DIR}"
          if (chosen / writer.RECORDS_DIR).exists() else
          "This backup has no copy of the records of dj-librarian. "
          "Thus dj-librarian can think that some of your genres are its guesses, and not learn from them.")


def show(backups: list[Path]) -> None:
    print(f"{store.BACKUP_DIR}, the oldest first:")
    for backup in backups:
        size = sum(f.stat().st_size for f in backup.rglob("*") if f.is_file()) / 1e6
        line = f"  {backup.name:20s} {writer.backup_command(backup) or '?':12s} {size:5.0f} MB  "
        print((line + _grids(backup)).rstrip())


def _grids(backup: Path) -> str:
    try:
        grids = len(store.read_json(backup / writer.ANALYSIS_MANIFEST, {}))
    except (OSError, ValueError):
        return f"damaged {writer.ANALYSIS_MANIFEST}"
    return plural(grids, "analysis file") if grids else ""
