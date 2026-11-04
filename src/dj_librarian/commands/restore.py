"""Put a backup of master.db (and of the analysis files) back."""

from dj_librarian import store, writer
from dj_librarian.commands.common import confirm


def add_parser(sub) -> None:
    parser = sub.add_parser(
        "restore", help="restore master.db (and analysis files) from a dj-librarian backup (latest by default)")
    parser.add_argument("backup", nargs="?", help="backup folder name")
    parser.set_defaults(func=run)


def run(args) -> None:
    backups = sorted(p for p in store.BACKUP_DIR.iterdir() if p.is_dir()) if store.BACKUP_DIR.exists() else []
    if not backups:
        raise SystemExit("no backups found")
    chosen = store.BACKUP_DIR / args.backup if args.backup else backups[-1]
    writer.ensure_rekordbox_closed()
    if not confirm(f"replace {writer.db_path()} with backup {chosen.name}?"):
        print("cancelled")
        return
    current = writer.restore(chosen, writer.db_path())
    print(f"restored. To undo: dj-librarian restore {current.name}")
