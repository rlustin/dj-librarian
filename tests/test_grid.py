from types import SimpleNamespace

import pytest
from pyrekordbox import AnlzFile

from dj_librarian import grid

import anlz
from helpers import track

TEMPO = 8500  # 85 BPM x 100
BEATS = [((i % 4) + 1, TEMPO, 200 + round(i * 60000 / 85)) for i in range(16)]
PHRASES = [{"index": 1, "beat": 1, "kind": 1}, {"index": 2, "beat": 9, "kind": 2, "beat_fill": 15}]


class FakeDb:
    def __init__(self, paths: dict, bpm: int = TEMPO, length: int = 12) -> None:
        self.content = SimpleNamespace(ID="1", BPM=bpm, Length=length)
        self.paths = paths
        self.commits = 0

    def get_content(self, ID):
        return self.content

    def get_anlz_paths(self, content):
        return self.paths

    def commit(self):
        self.commits += 1


@pytest.fixture
def files(tmp_path):
    dat, ext = tmp_path / "ANLZ0000.DAT", tmp_path / "ANLZ0000.EXT"
    dat.write_bytes(anlz.dat(BEATS))
    ext.write_bytes(anlz.ext(BEATS, PHRASES, end_beat=17))
    return {"DAT": dat, "EXT": ext}


def test_prepare_doubles_the_grid(files):
    change = grid.prepare_double(FakeDb(files, length=60), track(bpm=85.0))

    entries = AnlzFile.parse(change.dat_data).get_tag("PQTZ").content.entries
    assert len(entries) == 2 * len(BEATS)
    assert {e.tempo for e in entries} == {2 * TEMPO}
    assert [e.beat for e in entries[:8]] == [1, 2, 3, 4, 1, 2, 3, 4]
    # The old beats stay in place. The new beats are in the middle.
    assert [e.time for e in entries[::2]] == [ms for _, _, ms in BEATS]
    assert entries[1].time == round((BEATS[0][2] + BEATS[1][2]) / 2)
    # Old downbeats and old beats 3 are new downbeats
    old_downbeats = [ms for b, _, ms in BEATS if b in (1, 3)]
    assert [e.time for e in entries if e.beat == 1] == old_downbeats


def test_prepare_keeps_the_phrases_at_the_same_time(files):
    change = grid.prepare_double(FakeDb(files, length=60), track(bpm=85.0))

    pssi = AnlzFile.parse(change.ext_data).get_tag("PSSI").content
    assert [e.beat for e in pssi.entries] == [1, 17]  # old beat n -> new beat 2n - 1
    assert pssi.entries[1].beat_fill == 29
    assert pssi.end_beat == 33


def test_prepare_clears_the_extended_grid(files):
    change = grid.prepare_double(FakeDb(files, length=60), track(bpm=85.0))

    pqt2 = AnlzFile.parse(change.ext_data).get_tag("PQT2")
    assert pqt2.content.entry_count == 0
    assert pqt2.struct.len_tag == pqt2.struct.len_header


def test_prepare_does_not_add_a_beat_after_the_end_of_the_track(files):
    end_seconds = BEATS[-1][2] // 1000 + 1  # The midpoint after the last beat is after this time
    change = grid.prepare_double(FakeDb(files, length=end_seconds), track(bpm=85.0))

    assert len(AnlzFile.parse(change.dat_data).get_tag("PQTZ").content.entries) == 2 * len(BEATS) - 1


def test_prepare_refuses_a_bpm_that_is_not_the_grid_tempo(files):
    with pytest.raises(grid.GridError, match="not the BPM of the beat grid"):
        grid.prepare_double(FakeDb(files, bpm=8600), track(bpm=86.0))


def test_prepare_refuses_a_grid_with_more_than_one_tempo(files):
    files["DAT"].write_bytes(anlz.dat(BEATS[:8] + [(b, TEMPO + 100, ms) for b, _, ms in BEATS[8:]]))
    with pytest.raises(grid.GridError, match="more than one tempo"):
        grid.prepare_double(FakeDb(files), track(bpm=85.0))


def test_prepare_refuses_a_track_without_analysis_files():
    with pytest.raises(grid.GridError, match="no analysis files"):
        grid.prepare_double(FakeDb({}), track(bpm=85.0))


def test_apply_writes_the_files_and_the_bpm(files):
    db = FakeDb(files, length=60)
    change = grid.prepare_double(db, track(bpm=85.0))

    assert grid.apply(db, [change]) == 1

    assert files["DAT"].read_bytes() == change.dat_data
    assert files["EXT"].read_bytes() == change.ext_data
    assert db.content.BPM == 2 * TEMPO
    assert db.commits == 1


def _new_beats(bpm: float, first: float, count: int) -> list[tuple[int, float]]:
    return [((k % 4) + 1, first + k * 60 / bpm) for k in range(count)]


def test_retime_writes_the_new_grid(files):
    db = FakeDb(files, length=60)
    new = _new_beats(95.0, 0.25, 40)

    change = grid.prepare_retime(db, track(bpm=85.0), 95.0, new)

    entries = AnlzFile.parse(change.dat_data).get_tag("PQTZ").content.entries
    assert [(e.beat, e.time) for e in entries] == [(n, round(t * 1000)) for n, t in new]
    assert {e.tempo for e in entries} == {9500}
    assert change.bpm == 9500
    assert AnlzFile.parse(change.ext_data).get_tag("PQT2").content.entry_count == 0


def test_retime_keeps_the_phrases_at_the_same_time(files):
    new = _new_beats(95.0, 0.25, 40)

    change = grid.prepare_retime(FakeDb(files, length=60), track(bpm=85.0), 95.0, new)

    pssi = AnlzFile.parse(change.ext_data).get_tag("PSSI").content
    old_times = [ms / 1000 for _, _, ms in BEATS]
    for entry, old_beat in zip(pssi.entries, (1, 9)):
        assert abs(new[entry.beat - 1][1] - old_times[old_beat - 1]) <= 60 / 95 / 2


def test_apply_sets_the_new_bpm(files):
    db = FakeDb(files, length=60)
    change = grid.prepare_retime(db, track(bpm=85.0), 95.0, _new_beats(95.0, 0.25, 40))

    grid.apply(db, [change])

    assert db.content.BPM == 9500
    assert files["DAT"].read_bytes() == change.dat_data
