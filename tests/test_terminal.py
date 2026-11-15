import pytest

from dj_librarian.terminal import Keys, plural


def _keys(typed: str) -> Keys:
    keys = Keys()
    keys.tty, keys.typed = True, typed
    return keys


@pytest.mark.parametrize(("typed", "result"), [
    ("1k", ["1", "k"]),
    ("\x1b[C\x1b[D", ["right", "left"]),
    ("\x1bOC", ["right"]),  # arrow keys in application mode
    ("\x1b1", ["escape", "1"]),
    ("\x1b[3~q", ["unknown", "q"]),  # the Delete key
])
def test_read_splits_the_keys(typed, result):
    keys = _keys(typed)

    assert [keys.read() for _ in result] == result
    assert keys.typed == ""


def test_line_takes_a_line_typed_fast(capsys):
    keys = _keys("zouk bass\rk")

    assert keys.line("genre: ", []) == "zouk bass"
    assert keys.read() == "k"


@pytest.mark.parametrize(("n", "result"), [(0, "0 tracks"), (1, "1 track"), (2, "2 tracks")])
def test_plural(n, result):
    assert plural(n, "track") == result


@pytest.mark.parametrize(("n", "result"), [(0, "0 copies"), (1, "1 copy"), (2, "2 copies")])
def test_plural_with_another_plural_form(n, result):
    assert plural(n, "copy", "copies") == result
