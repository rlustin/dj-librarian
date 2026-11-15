from dj_librarian import claude_genres, store, writer
from dj_librarian.commands import common

from helpers import add_content


def test_a_backup_keeps_the_links_of_the_comments(db, home, monkeypatch):
    content = add_content(db, "7")
    content.Commnt = "https://artist.bandcamp.com/track/a"
    add_content(db, "8")
    db.commit()
    monkeypatch.setattr(writer, "db_path", lambda: home / "master.db")

    common.make_backup("apply", db)

    backup = writer.backups()[-1]
    assert claude_genres.unread_backups([backup]) == []  # The cleanup does not need to read it
    assert store.read_json(store.SOURCE_URLS, None)["urls"] == {"7": "https://artist.bandcamp.com/track/a"}


def test_a_damaged_source_urls_json_does_not_stop_a_write(db, home, monkeypatch, capsys):
    store.SOURCE_URLS.write_text("{")
    monkeypatch.setattr(writer, "db_path", lambda: home / "master.db")
    monkeypatch.setattr(writer, "KEEP_BACKUPS", 1)

    for _ in range(3):
        common.make_backup("apply", db)
    assert len(writer.old_backups()) == 1
    common.clean_backups()

    output = capsys.readouterr().out
    assert "cannot keep the Bandcamp links" in output
    assert "cannot delete the old backups" in output
    assert len(writer.backups()) == 3
