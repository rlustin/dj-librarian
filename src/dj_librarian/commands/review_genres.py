"""Listen to the tracks and answer the genre questions, one track at a time."""

import difflib
import shutil
import textwrap
from collections import Counter
from dataclasses import dataclass

from dj_librarian import checks, library, proposals, tagging
from dj_librarian.commands.common import load_checks, neighbours, track_name
from dj_librarian.player import SEEK_SECONDS, Player
from dj_librarian.progress import Steps
from dj_librarian.terminal import Keys, accent, bold, clear, dim, replace_first_line, warn

CLAUDE_RANK = {"high": 0, "medium": 1, "low": 2}
SIMILAR_EXAMPLES = 2
CLOSE_SPELLING = 0.75  # difflib ratio. "jungel" and "Jungle" have 0.83.


def add_parser(sub) -> None:
    parser = sub.add_parser("review-genres", help="approve or reject proposed genre changes, interactively")
    parser.add_argument("--playlist", help="only tracks of this playlist")
    parser.add_argument("--no-play", action="store_true", help="do not play the tracks during the review")
    parser.add_argument("--outliers", action="store_true",
                        help="instead, review the tracks that do not sound like their genre (see `dj-librarian check`)")
    parser.add_argument("--genre", help="with --outliers: only the tracks of this genre")
    parser.set_defaults(func=run)


@dataclass
class Question:
    proposal: proposals.Proposal
    kind: str
    current: str | None  # your genre now. An answer that `apply` did not write yet is also your genre.
    old_guess: bool  # the current genre is a guess of dj-librarian
    outlier: checks.Outlier | None = None

    @property
    def track(self) -> library.Track:
        return self.proposal.track


def release(t: library.Track) -> tuple[str, str] | None:
    """Tracks of the same release have the same album name and are in the same folder."""
    album = (t.album or "").strip().lower()
    return (album, t.path.rsplit("/", 1)[0]) if album else None


def keep_answer(q: Question, decisions_before: dict[str, str | None]) -> str | None:
    """The answer to save when you keep the current genre."""
    if q.outlier:
        return decisions_before.get(q.track.id)  # A genre that you gave in an earlier review stays
    # Keep an old guess: it becomes your genre, so dj-librarian learns from it
    return q.current if q.old_guess else None


def resolve_genre(text: str, names: dict[str, str]) -> tuple[str, str]:
    """("known", your spelling), ("close", a genre with almost the same spelling) or ("new", the text)."""
    if text.lower() in names:
        return "known", names[text.lower()]
    close = difflib.get_close_matches(text.lower(), list(names), n=1, cutoff=CLOSE_SPELLING)
    return ("close", names[close[0]]) if close else ("new", text)


def run(args) -> None:
    db = library.open_db()
    selected, context = proposals.build(db, args.playlist, with_cues=False, steps=Steps())
    decisions = proposals.load_genre_decisions()
    guessed = proposals.load_guessed_genres()
    used = Counter(t.genre for t in context.tracks if t.genre and guessed.get(t.id) != t.genre)
    used.update(g for g in decisions.values() if g)
    # If a name has two spellings, the most used one comes last and wins
    names = {g.lower(): g for g, _ in reversed(used.most_common())}

    def question(p: proposals.Proposal, kind: str) -> Question:
        old_guess = bool(p.track.genre) and guessed.get(p.track.id) == p.track.genre
        return Question(p, kind, p.track.genre, old_guess)

    if args.outliers:
        _, _, _, report = load_checks(db, Steps())
        by_id = {p.track.id: p for p in selected}
        # An answer to a genre suggestion does not count. Only an answer to this check does.
        reviewed = checks.load_outliers_reviewed()
        questions = [
            Question(by_id[o.track.id], "not like its genre", o.genre, False, o)
            for o in report.outliers
            if o.track.id in by_id and reviewed.get(o.track.id) != o.genre
            and (not args.genre or o.genre.lower() == args.genre.lower())
        ]
    else:
        reviews = [question(p, "your genre is not like similar tracks") for p in selected
                   if p.genre_pending and p.genre_action == "review"]
        # Claude's answers first, most confident first.
        # On one library, high-confidence answers matched the tags 9 times in 10.
        suggestions = sorted(
            (p for p in selected if p.genre_pending and p.genre_action == "suggest"),
            key=lambda p: (0, CLAUDE_RANK[p.claude["confidence"]]) if p.claude else (1, -p.genre_guess.confidence),
        )
        questions = reviews + [question(p, "old guess" if p.track.genre else "no genre") for p in suggestions]
    if not questions:
        print("no genre change to review")
        return

    review = Review(db, questions, names, {t.id: t for t in context.tracks}, Player(enabled=not args.no_play))
    review.run()
    to_write = sum(proposals.load_genre_decisions().get(p.track.id) not in (None, p.track.genre or "")
                   for p in selected)
    approved = sum(review.answers[q.track.id] not in (None, q.current) for q in questions
                   if q.track.id in review.answers)
    print(f"\nThis session: {len(review.answers)} answers, {approved} genres approved. "
          f"The next `dj-librarian apply` writes {to_write} genre changes from your answers.")


class Review:
    def __init__(self, db, questions: list[Question], names: dict[str, str], tracks: dict[str, library.Track],
                 player: Player) -> None:
        self.db, self.questions, self.names, self.tracks, self.player = db, questions, names, tracks, player
        self.decisions_before = proposals.load_genre_decisions()
        self.answers: dict[str, str | None] = {}  # this session: content ID -> the saved answer
        self.keys = Keys()

    def run(self) -> None:
        index = 0
        try:
            with self.keys:
                while index < len(self.questions):
                    action = self.ask(index)
                    if action == "quit":
                        break
                    if action == "back":
                        index = max(index - 1, 0)
                        continue
                    index = next((n for n in range(index + 1, len(self.questions))
                                  if self.questions[n].track.id not in self.answers), len(self.questions))
        except KeyboardInterrupt:
            pass
        finally:
            self.player.stop()

    def save(self, q: Question, answer: str | None) -> None:
        proposals.save_genre_decision(q.track.id, answer)
        if q.outlier:
            checks.save_outlier_reviewed(q.track.id, answer or q.current)
        self.answers[q.track.id] = answer

    def mates(self, q: Question) -> list[Question]:
        """The other open tracks of the same release."""
        key = release(q.track)
        return [m for m in self.questions if key and m is not q and release(m.track) == key
                and m.track.id not in self.answers]

    def ask(self, index: int) -> str:
        """Play the track and wait for an answer. Return "next", "back" or "quit"."""
        q = self.questions[index]
        candidates = q.proposal.genre_guess.candidates
        start = _preview_start(self.db, q.proposal)
        message = ""
        if self.player.available:
            self.player.play(q.track.path, start)
        whole_release, redraw = False, True
        while True:
            if redraw:
                self.show(index, whole_release, message)
            key = self.keys.read(timeout=0.5)
            redraw = key is not None
            if key is None:
                replace_first_line(self.header(index))  # The play position
                continue
            message, genre = "", None
            if key in ("q", "eof"):
                return "quit"
            if key == "b":
                if index == 0:
                    message = "this is the first track"
                    continue
                return "back"
            if key == "s":
                return "next"
            if key in ("right", ">", "left", "<"):
                self.player.seek(SEEK_SECONDS if key in ("right", ">") else -SEEK_SECONDS)
            elif key == " ":
                self.player.pause()
            elif key == "r":
                self.player.play(q.track.path, start)
            elif key == "a":
                whole_release = bool(self.mates(q)) and not whole_release
                if not self.mates(q):
                    message = "no other open track of this release"
            elif key == "escape":
                whole_release = False
            elif key == "k" and not whole_release:
                self.save(q, keep_answer(q, self.decisions_before))
                return "next"
            elif key.isdigit() and 1 <= int(key) <= len(candidates):
                genre = candidates[int(key) - 1][0]
            elif key == "c" and q.proposal.claude:
                genre = q.proposal.claude["genre"]
            elif key == "/":
                genre = self.type_genre()
            if genre:
                for mate in self.mates(q) if whole_release else []:
                    self.save(mate, genre)
                self.save(q, genre)
                return "next"

    def type_genre(self) -> str | None:
        text = self.keys.line("  genre (Tab completes, empty to cancel): ", sorted(self.names.values()))
        if not text or not text.isprintable():
            return None
        kind, name = resolve_genre(text, self.names)
        if kind == "known" or kind == "close" and self.keys.ask(f'  Did you mean "{name}"?'):
            return name
        if self.keys.ask(f'  "{text}" is a new genre. Add it?'):
            self.names[text.lower()] = text
            return text
        return None

    def header(self, index: int) -> str:
        q = self.questions[index]
        left = f"[{index + 1}/{len(self.questions)}] {q.kind}"
        right = ""
        if self.player.path:
            duration = q.proposal.analysis["duration"]
            state = "■" if self.player.ended else "❚❚" if self.player.paused else "▶"
            right = f"{state} {_clock(min(self.player.position(), duration))} / {_clock(duration)}"
        width = shutil.get_terminal_size().columns
        return dim(left) + " " * max(width - len(left) - len(right) - 1, 1) + right

    def show(self, index: int, whole_release: bool, message: str) -> None:
        q, width = self.questions[index], shutil.get_terminal_size().columns
        p, t = q.proposal, q.track
        clear()
        lines = [self.header(index), bold(track_name(t))]
        about = [t.album, t.label, str(t.year or ""), f"{t.bpm:.0f} BPM" if t.bpm else "",
                 f"E{p.energy} {'/'.join(p.moods)}"]
        lines += [dim(" · ".join(a for a in about if a)), ""]

        note = " (old guess of dj-librarian)" if q.old_guess else " (yours)" if q.outlier else ""
        lines.append(f"  now  {q.current or '(empty)'}{dim(note)}")
        if q.outlier:
            lines.append(dim(f"       None of the {tagging.KNN_K} nearest tagged tracks has this genre. "
                             f"Nearest: {neighbours(q.outlier.neighbours)}"))
        if t.id in self.answers:
            answer = self.answers[t.id]
            lines.append(accent(f"  your answer: {answer if answer not in (None, q.current) else 'keep'}"))
        lines.append("")

        candidates = p.genre_guess.candidates
        column = max([len(g) for g, _ in candidates] + [len(p.claude["genre"]) if p.claude else 0, 12])
        if p.claude:
            new = ", new genre" if p.claude["is_new_genre"] else ""
            agrees = ", same as yours" if q.outlier and p.claude["genre"] == q.current else ""
            lines.append(f"  {accent('c')}  {p.claude['genre']:<{column}}  "
                         f"Claude, {p.claude['confidence']}{new}{agrees}")
            indent = " " * (column + 7)
            reason = textwrap.fill(p.claude["reason"], max(width - len(indent), 30),
                                   initial_indent=indent, subsequent_indent=indent)
            lines.append(dim(reason))
            if p.claude["alternatives"]:
                lines.append(dim(f"{indent}also possible: {', '.join(p.claude['alternatives'])}"))
        for k, (genre, share) in enumerate(candidates, 1):
            examples = [track_name(self.tracks[i]) for i, g, _ in p.genre_guess.neighbours
                        if g == genre and i in self.tracks][:SIMILAR_EXAMPLES]
            like = f"like {', '.join(examples)}" if examples else ""
            line = f"  {accent(k)}  {genre:<{column}}  {share:4.0%}  "
            room = max(width - column - 14, 10)
            lines.append(line + dim(like if len(like) <= room else like[: room - 1] + "…"))
        lines += ["", dim(f"  Discogs model: {p.genre_guess.discogs_hint}"), ""]

        mates = self.mates(q)
        if whole_release:
            others = "the other open track" if len(mates) == 1 else f"the {len(mates)} other open tracks"
            lines.append(warn(f'  Same genre for this track and {others} of "{t.album}". Pick a genre, or press Esc.'))
        else:
            pick = "/".join(([f"1-{len(candidates)}"] if candidates else []) + (["c"] if p.claude else []))
            keys = [f"{accent(pick)} pick"] if pick else []
            keys += [f"{accent('/')} type", f"{accent('k')} keep {q.current or 'empty'}"]
            if mates:
                keys.append(f"{accent('a')} whole release ({len(mates) + 1})")
            lines.append("  " + " · ".join(keys))
            lines.append(f"  {accent('s')} skip · {accent('b')} back · {accent('q')} quit (your answers stay saved)")
        if self.player.path:
            lines.append(f"  {accent('←/→')} {SEEK_SECONDS} s · {accent('space')} pause · {accent('r')} restart")
        elif self.player.available:
            lines.append(warn(f"  cannot play: file not found. Make sure that its drive is mounted: {t.path}"))
        if message:
            lines.append(warn(f"  {message}"))
        print("\n".join(lines), flush=True)


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def _preview_start(db, p: proposals.Proposal) -> float:
    """The most representative part to listen to: full energy, else intro end, else a third in."""
    track = library.load_tracks(db, only={p.track.id})[0]  # beat grid of this track only (cached)
    positions = tagging.plan_cues(track, p.analysis).positions
    return positions.get("Full energy") or positions.get("Intro end") or p.analysis["duration"] / 3
