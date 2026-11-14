from dj_librarian import store, writer
from dj_librarian.commands import restore


def test_list_shows_the_command_of_each_backup(home, capsys):
    (home / "master.db").write_bytes(b"db")
    old = store.BACKUP_DIR / "2026-10-25_202245"
    old.mkdir(parents=True)
    first = writer.backup(home / "master.db", "apply")
    second = writer.backup(home / "master.db", "check-grids")
    writer.backup_analysis(second, [home / "master.db"])

    restore.show(writer.backups())

    lines = capsys.readouterr().out.splitlines()[1:]
    assert lines[0].split() == [old.name, "?", "0", "MB"]
    assert lines[1].split()[:2] == [first.name, "apply"]
    assert lines[2].split()[:2] == [second.name, "check-grids"]
    assert "1 analysis file" in lines[2] and "files" not in lines[2]


def test_list_shows_a_damaged_manifest(home, capsys):
    (home / "master.db").write_bytes(b"db")
    backup = writer.backup(home / "master.db", "check-grids")
    (backup / writer.ANALYSIS_MANIFEST).write_text("{")

    restore.show(writer.backups())

    assert f"damaged {writer.ANALYSIS_MANIFEST}" in capsys.readouterr().out
