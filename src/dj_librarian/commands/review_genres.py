"""Listen to the tracks and answer the genre questions, one track at a time."""

from collections import Counter

from dj_librarian import checks, library, proposals, tagging
from dj_librarian.commands.common import fmt_time, load_checks, neighbours
from dj_librarian.player import SEEK_SECONDS, Player
from dj_librarian.progress import Steps


def add_parser(sub) -> None:
    parser = sub.add_parser("review-genres", help="approve or reject proposed genre changes, interactively")
    parser.add_argument("--playlist", help="only tracks of this playlist")
    parser.add_argument("--no-play", action="store_true", help="do not play the tracks during the review")
    parser.add_argument("--outliers", action="store_true",
                        help="instead, review the tracks that do not sound like their genre (see `dj-librarian check`)")
    parser.add_argument("--genre", help="with --outliers: only the tracks of this genre")
    parser.set_defaults(func=run)


def run(args) -> None:
    db = library.open_db()
    selected, context = proposals.build(db, args.playlist, with_cues=False, steps=Steps())
    decisions = proposals.load_genre_decisions()
    guessed = proposals.load_guessed_genres()
    used = Counter(t.genre for t in context.tracks if t.genre and guessed.get(t.id) != t.genre)
    used.update(g for g in decisions.values() if g)
    # If a name has two spellings, the most used one comes last and wins
    names = {g.lower(): g for g, _ in reversed(used.most_common())}
    reviews = [p for p in selected if p.genre_pending and p.genre_action == "review"]
    outliers = {}
    if args.outliers:
        _, _, _, report = load_checks(db, Steps())
        outliers = {o.track.id: o for o in report.outliers
                    if not args.genre or o.genre.lower() == args.genre.lower()}
        # Earlier answers do not count: most come from a suggestion, before this check existed
        reviewed = checks.load_outliers_reviewed()
        reviews = [p for p in selected if p.track.id in outliers
                   and reviewed.get(p.track.id) != outliers[p.track.id].genre]
        order = {i: n for n, i in enumerate(outliers)}
        reviews.sort(key=lambda p: order[p.track.id])
    # Claude's answers first, most confident first: high-confidence answers matched your tags 9 times in 10
    rank = {"high": 0, "medium": 1, "low": 2}
    suggestions = sorted(
        (p for p in selected if p.genre_pending and p.genre_action == "suggest" and not args.outliers),
        key=lambda p: (0, rank[p.claude["confidence"]]) if p.claude else (1, -p.genre_guess.confidence),
    )
    pending = reviews + suggestions
    if not pending:
        print("no genre change to review")
        return
    player = Player(enabled=not args.no_play)
    answered = approved_now = 0
    from_claude = sum(bool(p.claude) for p in suggestions)
    if args.outliers:
        print(f"{len(reviews)} tracks that do not sound like their genre, the most different first.")
    else:
        print(f"{len(reviews)} tracks where your genre is different from similar tracks, then "
              f"{len(suggestions)} tracks without a genre or with an old guess of dj-librarian "
              f"({from_claude} with a suggestion from Claude, first).")
    print("dj-librarian saves each answer. You can stop at any time and continue later.")
    if player.available:
        print(f"Each track plays from its full-energy part: > / < to move {SEEK_SECONDS}s, r to restart.")
    print()
    try:
        for n, p in enumerate(pending, 1):
            candidates = p.genre_guess.candidates
            print(f"[{n}/{len(pending)}] {p.track.artist} - {p.track.title}")
            outlier = outliers.get(p.track.id)
            # An approved answer is the current genre, also before `apply` writes it
            current_genre = outlier.genre if outlier else p.track.genre
            old_guess = not outlier and bool(current_genre) and guessed.get(p.track.id) == current_genre
            current = f"{current_genre} (old guess of dj-librarian)" if old_guess else current_genre or "(empty)"
            print(f"    current: {current}    Discogs model: {p.genre_guess.discogs_hint}")
            if outlier:
                print(f"    nearest tagged tracks: {neighbours(outlier.neighbours)}")
            if p.claude:
                new = ", new genre" if p.claude["is_new_genre"] else ""
                agrees = ", same as yours" if outlier and p.claude["genre"] == current_genre else ""
                print(f"    c. {p.claude['genre']}  (Claude, {p.claude['confidence']}{new}{agrees}): {p.claude['reason']}")
                if p.claude["alternatives"]:
                    print(f"       also possible: {', '.join(p.claude['alternatives'])}")
            for k, (genre, share) in enumerate(candidates, 1):
                print(f"    {k}. {genre}  ({share:.0%} of the most similar tracks)")
            start = _preview_start(db, p)
            if player.available and not player.play(p.track.path, start):
                print(f"    (cannot play: file not found. Make sure that its drive is mounted: {p.track.path})")
            keep = "[k]eep current" if current_genre else "[k]eep empty"
            pick = f"{'c, ' if p.claude else ''}1-{len(candidates)}"
            prompt = f"    {pick} to pick, {keep}, [s]kip, [q]uit, or type a genre: "
            while True:
                answer = input(prompt).strip()
                if answer in (">", "<", "r"):
                    if answer == "r":
                        player.play(p.track.path, start)
                    else:
                        player.seek(SEEK_SECONDS if answer == ">" else -SEEK_SECONDS)
                    if player.available:
                        print(f"    play from {fmt_time(player.offset)}")
                    continue
                choices = {"q", "s", "k", ""} | ({"c"} if p.claude else set())
                if answer.lower() in choices or answer.isdigit() and 1 <= int(answer) <= len(candidates):
                    break
                if typed := _typed_genre(answer, names):
                    answer = typed
                    break
            player.stop()
            if answer.lower() == "q":
                break
            if answer.lower() == "s" or not answer:
                continue
            answered += 1
            if answer.lower() == "c" and p.claude:
                decisions[p.track.id] = p.claude["genre"]
            elif answer.isdigit() and 1 <= int(answer) <= len(candidates):
                decisions[p.track.id] = candidates[int(answer) - 1][0]
            elif answer.lower() == "k" and outlier:
                decisions.setdefault(p.track.id, None)
            elif answer.lower() == "k":
                # Keep an old guess: it becomes your genre, so dj-librarian learns from it
                decisions[p.track.id] = current_genre if old_guess else None
            else:
                decisions[p.track.id] = answer
            approved_now += decisions[p.track.id] not in (None, current_genre)
            proposals.save_genre_decision(p.track.id, decisions[p.track.id])
            if outlier:
                checks.save_outlier_reviewed(p.track.id, decisions[p.track.id] or current_genre)
    finally:
        player.stop()
    decisions = proposals.load_genre_decisions()
    to_write = sum(decisions.get(p.track.id) not in (None, p.track.genre or "") for p in selected)
    print(f"\nThis session: {answered} answers, {approved_now} genres approved. "
          f"The next `dj-librarian apply` writes {to_write} genre changes from your answers.")


def _typed_genre(text: str, names: dict[str, str]) -> str | None:
    """Your spelling of a genre that you use, or a new genre after you confirm it. None: ask again."""
    if not text.isprintable():
        print("    this is not a genre name: it contains control characters (for example, an arrow key)")
        return None
    if text.lower() in names:
        return names[text.lower()]
    if input(f'    "{text}" is a new genre. Type y to add it: ').strip().lower() == "y":
        names[text.lower()] = text
        return text
    return None


def _preview_start(db, p: proposals.Proposal) -> float:
    """The most representative part to listen to: full energy, else intro end, else a third in."""
    track = library.load_tracks(db, only={p.track.id})[0]  # beat grid of this track only (cached)
    positions = tagging.plan_cues(track, p.analysis).positions
    return positions.get("Full energy") or positions.get("Intro end") or p.analysis["duration"] / 3
