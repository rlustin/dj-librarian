import csv
from argparse import Namespace

import pytest

from dj_librarian.commands import report

from helpers import add_content, fill_and_review


@pytest.fixture
def library(db, analysed):
    fill_and_review(db, analysed)
    add_content(db, "new")
    db.commit()
    return db


def _run(tmp_path, **fields) -> dict[str, dict]:
    out = tmp_path / "report.csv"
    report.run(Namespace(**({"playlist": None, "track": None, "out": str(out)} | fields)))
    with open(out, newline="") as f:
        return {row["title"]: row for row in csv.DictReader(f)}


def test_report_writes_a_row_for_each_analysed_track(library, tmp_path, capsys):
    rows = _run(tmp_path)

    assert len(rows) == 14
    assert rows["No genre"].items() >= {"genre_action": "fill", "genre_proposed": "Jungle", "genre_to_write": "Jungle",
                                        "key_ours": "8A", "cue_start": "0:00.5", "cues_to_write": "A=0:00.5"}.items()
    assert rows["Odd one"].items() >= {"genre_current": "Grime", "genre_action": "review",
                                       "genre_to_write": ""}.items()
    assert rows["Odd one"]["comment_proposed"].startswith("E6 ")
    output = capsys.readouterr().out
    assert f"14 tracks in report (14/15 analysed in total) -> {tmp_path / 'report.csv'}" in output
    assert "genre fill: 1" in output and "genre review (pending): 1" in output
    assert "genre self-check on your 13 tagged tracks: confident guess for 13, of which 12 (92%)" in output


def test_report_of_one_track(library, tmp_path):
    assert list(_run(tmp_path, track="fill")) == ["No genre"]


def test_report_stops_if_the_track_is_not_found(library, tmp_path):
    with pytest.raises(SystemExit, match="--track must match exactly one track, matched 0"):
        _run(tmp_path, track="unknown")


def test_report_stops_if_the_track_has_no_analysis(library, tmp_path):
    with pytest.raises(SystemExit, match="none of the selected tracks has an analysis"):
        _run(tmp_path, track="new")
