from dj_librarian import store


def test_write_json_then_read_json(tmp_path):
    path = tmp_path / "folder" / "answers.json"

    store.write_json(path, {"1": "Décembre"})

    assert store.read_json(path, {}) == {"1": "Décembre"}
    assert [p.name for p in path.parent.iterdir()] == ["answers.json"]


def test_read_json_gives_the_default_without_a_file(tmp_path):
    assert store.read_json(tmp_path / "missing.json", {}) == {}


def test_the_tests_do_not_use_your_files():
    assert "dj-librarian-test-" in str(store.HOME)
