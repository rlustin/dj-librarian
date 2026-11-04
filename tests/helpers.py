from dj_librarian.library import Track


def track(id: str = "1", genre: str | None = None, title: str = "Title", **fields) -> Track:
    defaults = {"artist": "Artist", "path": f"/music/{id}.mp3", "bpm": 120.0, "key": None, "comment": None}
    return Track(id=id, title=title, genre=genre, **(defaults | fields))
