"""Build small rekordbox analysis files for the tests, with the structs of pyrekordbox."""

from pyrekordbox.anlz import structs

PHRASE = {
    "index": 0, "beat": 0, "kind": 1, "u1": 0, "k1": 0, "u2": 0, "k2": 0, "u3": 0, "b": 0,
    "beat_2": 0, "beat_3": 0, "beat_4": 0, "u4": 0, "k3": 0, "u5": 0, "fill": 0, "beat_fill": 0,
}


def _file(tags: list[bytes]) -> bytes:
    body = b"".join(tags)
    header = structs.AnlzFileHeader.build({
        "type": "PMAI", "len_header": 28, "len_file": 28 + len(body),
        "u1": 1, "u2": 0x10000, "u3": 0x10000, "u4": 0,
    })
    return header + body


def _tag(kind: str, len_header: int, content: dict) -> bytes:
    data = structs.AnlzTag.build({"type": kind, "len_header": len_header, "len_tag": 0, "content": content})
    return data[:8] + len(data).to_bytes(4, "big") + data[12:]


def dat(beats: list[tuple[int, int, int]]) -> bytes:
    """`beats`: (beat number in the bar, tempo x 100, time in ms)."""
    entries = [{"beat": b, "tempo": t, "time": ms} for b, t, ms in beats]
    return _file([_tag("PQTZ", 24, {"entry_count": len(entries), "entries": entries})])


def ext(beats: list[tuple[int, int, int]], phrases: list[dict], end_beat: int) -> bytes:
    tempo = beats[0][1]
    pqt2 = {
        "u1": 0x01000002, "bpm": [{"beat": 1, "tempo": tempo, "time": beats[0][2]}] * 2,
        "entry_count": len(beats), "u3": len(beats), "u4": 0, "u5": 0,
        "entries": [{"beat": b, "unkown": 0} for b, _, _ in beats],
    }
    pssi = {
        "version": 0, "len_entries": len(phrases), "mood": 1, "u1": bytes(6), "end_beat": end_beat,
        "u2": bytes(2), "bank": 0, "u3": bytes(1), "entries": [PHRASE | p for p in phrases],
    }
    return _file([_tag("PQT2", 56, pqt2), _tag("PSSI", 32, pssi)])
