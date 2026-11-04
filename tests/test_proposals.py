from dj_librarian import proposals

from helpers import track


def test_your_genres():
    tracks = [
        track("tagged", "Jungle"),
        track("guessed", "Techno"),
        track("answered", "Techno"),
        track("removed", "Bass"),
        track("kept", "Grime"),
        track("empty"),
    ]
    guessed = {"guessed": "Techno", "answered": "Techno"}
    decisions = {"answered": "Electro", "removed": "", "kept": None}

    assert proposals.your_genres(tracks, guessed, decisions) == {
        "tagged": "Jungle",  # your genre in rekordbox
        "answered": "Electro",  # your answer in review-genres wins
        "kept": "Grime",  # you kept the genre in rekordbox
    }


def test_genre_decisions_are_saved_one_by_one():
    proposals.save_genre_decisions({})
    proposals.save_genre_decision("1", "Jungle")
    proposals.save_genre_decision("2", None)

    assert proposals.load_genre_decisions() == {"1": "Jungle", "2": None}


def test_no_cue_is_generated_without_a_record():
    proposals.store.GENERATED_CUES.unlink(missing_ok=True)

    assert proposals.load_generated_cues() == set()
