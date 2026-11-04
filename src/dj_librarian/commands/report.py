"""Write what dj-librarian proposes for each track to a CSV file. Read-only."""

from collections import Counter

from dj_librarian import library, proposals, tagging
from dj_librarian.commands.common import fmt_time, select, write_csv


def add_parser(sub) -> None:
    parser = sub.add_parser("report", help="propose metadata (read-only) and write it to a CSV file")
    parser.add_argument("--playlist", help="only tracks of this playlist")
    parser.add_argument("--out", default="report.csv")
    parser.add_argument("--track", help="only the track whose 'artist - title' contains this text, or with this ID")
    parser.set_defaults(func=run)


def run(args) -> None:
    selected, context = select(library.open_db(), args)
    stats = Counter()
    rows = []
    for p in selected:
        t, a = p.track, p.analysis
        stats[f"bpm {p.bpm_check}"] += 1
        stats[f"key {p.key_check}"] += 1
        genre_status = f"{p.genre_action} (pending)" if p.genre_pending else p.genre_action
        stats[f"genre {genre_status}"] += 1
        stats[f"energy {p.energy}"] += 1
        stats["cues to write"] += len(p.cues.writes)
        stats["cues to label"] += len(p.cues.labels)
        for reason in p.cues.skipped.values():
            stats[f"cue skipped: {reason.split(' on ')[0]}"] += 1
        rows.append({
            "artist": t.artist, "title": t.title,
            "bpm_rekordbox": t.bpm, "bpm_ours": round(a["bpm"], 2), "bpm_check": p.bpm_check,
            "key_rekordbox": tagging.to_camelot(t.key), "key_ours": p.key_ours, "key_check": p.key_check,
            "genre_current": t.genre or "", "genre_proposed": p.genre_guess.genre or "",
            "genre_confidence": round(p.genre_guess.confidence, 2), "genre_discogs_hint": p.genre_guess.discogs_hint,
            "genre_action": p.genre_action, "genre_to_write": p.genre_write or "",
            "energy": p.energy, "moods": "/".join(p.moods),
            "comment_current": t.comment or "", "comment_proposed": p.comment,
            "hot_cues_existing": " ".join(f"{s}={fmt_time(v)}" for s, v in sorted(t.hot_cues.items())),
            **{f"cue_{n.lower().replace(' ', '_')}": fmt_time(p.cues.positions[n]) for n in tagging.CUE_NAMES},
            "rekordbox_first_chorus": fmt_time(p.cues.rekordbox_chorus),
            "cues_to_write": _fmt_cues(p.cues.writes),
            "cues_skipped": "; ".join(f"{n}: {r}" for n, r in p.cues.skipped.items()),
            "cues_to_label": " ".join(f"{s}={n}" for s, n in sorted(p.cues.labels.items())),
            "path": t.path,
        })

    write_csv(args.out, rows)

    print(f"{len(rows)} tracks in report ({context.analysed}/{context.total} analysed in total) -> {args.out}\n")
    for group in ("bpm", "key", "genre", "energy", "cue"):
        items = sorted((k, v) for k, v in stats.items() if k.startswith(group))
        if group == "energy":
            items.sort(key=lambda kv: int(kv[0].split()[1]))
        print("  " + ",  ".join(f"{k}: {v}" for k, v in items))
    _print_genre_accuracy(context.tracks, context.genres)


def _fmt_cues(cues: dict) -> str:
    return " ".join(f"{slot}={fmt_time(seconds)}" for slot, (_, seconds) in sorted(cues.items()))


def _print_genre_accuracy(tracks, genres) -> None:
    """Leave-one-out: how often the learned guess matches a genre you set yourself."""
    tagged = [t for t in tracks if t.genre and t.id in genres]
    guessed = proposals.load_guessed_genres()
    tagged = [t for t in tagged if guessed.get(t.id) != t.genre]
    learned = [t for t in tagged if genres[t.id].genre]
    if not learned:
        return
    correct = sum(genres[t.id].genre.lower() == t.genre.lower() for t in learned)
    print(f"\n  genre self-check on your {len(tagged)} tagged tracks: confident guess for {len(learned)}, "
          f"of which {correct} ({100 * correct / len(learned):.0f}%) match your tag")
