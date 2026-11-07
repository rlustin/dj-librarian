"""Find the tracks with a wrong beat grid, listen to a better grid with clicks, and write it."""

import os
import statistics
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path

from pyrekordbox import AnlzFile

from dj_librarian import beats, grid, library, proposals, store, waveform, writer
from dj_librarian.cache import Cache
from dj_librarian.commands.common import confirm, matches, quiet_stderr, track_name, write_csv
from dj_librarian.player import Player
from dj_librarian.progress import Steps
from dj_librarian.terminal import Keys, accent, bold, clear, dim, replace_first_line, replace_lines, warn

PROBLEMS = ("wrong tempo", "drifts", "shifted")
PREVIEW_SECONDS = 30
ZOOMS = (1.0, 2.0, 4.0, 8.0)  # seconds in the waveform view
WAVE_HEIGHT = 6
LABEL = 12  # the width of the names of the grids, left of the waveform
CSV_FIELDS = ["verdict", "track", "grid_bpm", "grid_fit", "better_bpm", "better_fit", "answer", "id", "path"]


def add_parser(sub) -> None:
    parser = sub.add_parser("check-grids", help="find wrong beat grids, listen to a better grid, and write it")
    parser.add_argument("--playlist", help="only tracks of this playlist")
    parser.add_argument("--track", help="only the tracks whose 'artist - title' contains this text, or with this ID")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=20, help="lines to show (default: 20, all in the CSV)")
    parser.add_argument("--out", default="grids.csv")
    parser.add_argument("--review", action="store_true",
                        help="listen to each better grid with clicks, and accept or reject it")
    parser.add_argument("--apply", action="store_true",
                        help="write the grids that you accepted (rekordbox must be closed)")
    parser.add_argument("--yes", action="store_true", help="with --apply: do not ask for confirmation")
    parser.add_argument("--no-play", action="store_true", help="with --review: do not play the previews")
    parser.set_defaults(func=run)


@dataclass
class Item:
    track: library.Track
    dat: Path
    audio: library.AudioFile
    key: str  # changes when rekordbox writes a new grid, or when the audio file changes
    result: dict
    verdict: str
    better: beats.Proposal | None
    offset: float  # between the rekordbox grid and the envelope, for this file type

    @property
    def extension(self) -> str:
        return Path(self.audio.path).suffix.lower()


def run(args) -> None:
    db = library.open_db()
    tracks = library.load_tracks(db, args.playlist, with_structure=False)
    if args.track:
        tracks = [t for t in tracks if matches(t, args.track)]
        if not tracks:
            raise SystemExit(f"no track matches {args.track!r}")
    items = _items(db, tracks, args.workers)
    decisions = _valid_decisions(items)
    if args.review:
        _review(items, decisions, Player(enabled=not args.no_play))
    elif args.apply:
        _apply(db, items, decisions, args.yes)
    else:
        _report(items, decisions, args)


def _key(dat: Path, audio: library.AudioFile) -> str:
    return f"{beats.CHECK_VERSION}:{os.path.getmtime(dat)}:{audio.size}:{audio.mtime}"


def _check_one(audio: str, dat: str) -> dict:
    times = [e.time / 1000 for e in AnlzFile.parse_file(dat).get_tag("PQTZ").content.entries]
    if len(times) < 8:  # rekordbox found no beat
        return {"version": beats.CHECK_VERSION, "no_grid": True}
    return beats.check(audio, times)


def _items(db, tracks: list[library.Track], workers: int) -> list[Item]:
    steps = Steps()
    steps.step("Find the audio files and the rekordbox grids")
    contents = {c.ID: c for c in db.get_content().all()}
    audio = library.find_files(tracks, steps)
    found = []
    for t in tracks:
        dat = db.get_anlz_paths(contents[t.id]).get("DAT")
        if dat and t.id in audio:
            found.append((t, Path(dat), audio[t.id], _key(Path(dat), audio[t.id])))
    cache = Cache()
    cached = cache.grid_checks()
    todo = [f for f in found if cached.get(f[0].id, ("",))[0] != f[3]]
    steps.done()
    print(f"{len(tracks)} tracks: {len(tracks) - len(found)} without an audio file or a rekordbox grid, "
          f"{len(todo)} to check")
    if todo:
        _run_checks(cache, todo, workers)
        cached = cache.grid_checks()

    # The offset needs many tracks with a right grid: use all the checked tracks, not only the selection
    extensions = {t.id: Path(t.path).suffix.lower() for t in library.load_tracks(db, with_structure=False)}
    offsets = beats.calibrate({i: r for i, (_, r) in cached.items()}, extensions)
    medians = _genre_medians(db)
    items = []
    for t, dat, audio, key in found:
        if t.id not in cached or cached[t.id][0] != key:
            continue  # The check failed
        result = cached[t.id][1]
        verdict, better = beats.verdict(result)
        if verdict == "wrong tempo" and medians.get(t.id):
            better = beats.nearest_family(result, medians[t.id])  # The tempo level of the genre
        offset = beats.offset_for(offsets, Path(audio.path).suffix.lower())
        items.append(Item(t, dat, audio, key, result, verdict, better, offset))
    return items


def _run_checks(cache: Cache, todo: list, workers: int) -> None:
    started, failures = time.time(), []
    with ProcessPoolExecutor(max_workers=workers, initializer=quiet_stderr) as pool:
        futures = {pool.submit(_check_one, audio.path, str(dat)): (t, key) for t, dat, audio, key in todo}
        for done, future in enumerate(as_completed(futures), 1):
            t, key = futures[future]
            try:
                cache.put_grid_check(t.id, key, future.result())
            except Exception as e:  # noqa: BLE001 - one bad file must not stop a long run
                failures.append(f"{track_name(t)}: {type(e).__name__}: {e}")
            elapsed = time.time() - started
            print(f"\r{done}/{len(todo)}  {elapsed / 60:.0f} min elapsed, "
                  f"~{elapsed / done * (len(todo) - done) / 60:.0f} min left  ", end="", flush=True)
    print()
    for failure in failures:
        print(f"failed: {failure}", file=sys.stderr)


def _genre_medians(db) -> dict[str, float]:
    """content ID -> the median BPM of the genre that you gave to the track."""
    tracks = library.load_tracks(db, with_structure=False)
    yours = proposals.your_genres(tracks, proposals.load_guessed_genres(), proposals.load_genre_decisions())
    bpms: dict[str, list[float]] = {}
    for t in tracks:
        if t.id in yours and t.bpm:
            bpms.setdefault(yours[t.id], []).append(t.bpm)
    medians = {g: statistics.median(v) for g, v in bpms.items() if len(v) >= 5}
    return {i: medians[g] for i, g in yours.items() if g in medians}


def _valid_decisions(items: list[Item]) -> dict[str, dict]:
    """Your answers for the current grids. An answer for a grid that changed since then does not count."""
    decisions = store.read_json(store.GRID_DECISIONS, {})
    keys = {item.track.id: item.key for item in items}
    return {i: d for i, d in decisions.items() if keys.get(i) == d["key"]}


def _answer(decision: dict | None) -> str:
    if not decision:
        return ""
    if decision.get("written"):
        return "written by dj-librarian"
    return "keep rekordbox grid" if decision.get("keep") else f"write {decision['bpm']:.2f} BPM"


def _report(items: list[Item], decisions: dict[str, dict], args) -> None:
    counts = Counter(item.verdict for item in items)
    print("\n" + ", ".join(f"{v}: {counts[v]}" for v in ("ok", *PROBLEMS, "no clear beat", "no grid") if counts[v]))
    problems = sorted((i for i in items if i.verdict in PROBLEMS), key=lambda i: i.result["grid_fit"] / i.better.fit)
    if problems:
        print(f"\nGrids that do not fit the audio: {len(problems)}. The worst fit first.")
        for item in problems[: args.limit]:
            answer = _answer(decisions.get(item.track.id))
            print(f"  {item.verdict:11s} {item.result['grid_bpm']:6.2f} -> {item.better.bpm:6.2f} BPM  "
                  f"{track_name(item.track)}" + (f"  [{answer}]" if answer else ""))
        if len(problems) > args.limit:
            print(f"  ... and {len(problems) - args.limit} more (see {args.out})")
    rows = [{"verdict": i.verdict, "track": track_name(i.track), "grid_bpm": _rounded(i.result.get("grid_bpm"), 2),
             "grid_fit": _rounded(i.result.get("grid_fit"), 1), "better_bpm": _rounded(i.better and i.better.bpm, 2),
             "better_fit": _rounded(i.better and i.better.fit, 1), "answer": _answer(decisions.get(i.track.id)),
             "id": i.track.id, "path": i.track.path} for i in items]
    write_csv(args.out, rows, CSV_FIELDS)
    open_problems = sum(i.track.id not in decisions for i in problems)
    accepted = sum(not d.get("keep") for d in decisions.values())
    print(f"\n{len(rows)} tracks -> {args.out}")
    if open_problems:
        print(f"Listen to the {open_problems} better grids with clicks: dj-librarian check-grids --review")
    if accepted:
        print(f"Write the {accepted} grids that you accepted: dj-librarian check-grids --apply "
              "(rekordbox must be closed)")


def _rounded(value: float | None, digits: int) -> float | str:
    return "" if value is None else round(value, digits)


def _save(track_id: str, decision: dict) -> None:
    store.write_json(store.GRID_DECISIONS, store.read_json(store.GRID_DECISIONS, {}) | {track_id: decision})


def _review(items: list[Item], decisions: dict[str, dict], player: Player) -> None:
    pending = [i for i in items if i.verdict in PROBLEMS and i.track.id not in decisions]
    pending.sort(key=lambda i: i.result["grid_fit"] / i.better.fit)
    if not pending:
        print("no grid to review")
        return
    review = GridReview(pending, player)
    review.run()
    print(f"\nThis session: {review.accepted} grids accepted, {review.kept} rekordbox grids kept. "
          "To write the accepted grids: dj-librarian check-grids --apply (rekordbox must be closed)")


class GridReview:
    def __init__(self, items: list[Item], player: Player) -> None:
        self.items, self.player, self.keys = items, player, Keys()
        self.folder = tempfile.mkdtemp(prefix="dj-librarian-preview-")
        self.accepted = self.kept = 0
        self.zoom = 1
        self.look: float | None = None  # where you moved the view with , and . (else it follows the playback)
        self.picture_row, self.drawn = 0, []

    def run(self) -> None:
        index = 0
        try:
            with self.keys:
                while 0 <= index < len(self.items):
                    action = self.ask(index)
                    if action == "quit":
                        break
                    index = max(index - 1, 0) if action == "back" else index + 1
        except KeyboardInterrupt:
            pass
        finally:
            self.player.stop()
            for name in os.listdir(self.folder):
                os.remove(os.path.join(self.folder, name))
            os.rmdir(self.folder)

    def ask(self, index: int) -> str:
        item = self.items[index]
        clear()
        print(f"[{index + 1}/{len(self.items)}] {track_name(item.track)}: load the audio...", flush=True)
        audio = beats.load(item.audio.path)
        duration = len(audio) / beats.SR
        self.audio, self.bass = audio, waveform.low_pass(audio, beats.SR)
        self.levels = waveform.scale(self.audio), waveform.scale(self.bass)
        old = [(e.beat, e.time / 1000) for e in AnlzFile.parse_file(item.dat).get_tag("PQTZ").content.entries]
        env, low = beats.envelopes(audio)
        proposal = beats.on_the_kick(low, item.better)
        if item.verdict == "drifts":
            proposal = beats.follow_old_grid(env, [t for _, t in old], item.offset, proposal)
        bar_start = proposal.downbeat
        if item.verdict != "wrong tempo":  # Same tempo: keep the bars of rekordbox
            bar_start = beats.keep_bar_position(old, beats.new_grid(proposal, item.offset, duration, 0))
        start = max(min(duration / 3, duration - PREVIEW_SECONDS), 0)
        on_new, message, redraw, changed = True, "", True, True
        while True:
            new = beats.new_grid(proposal, item.offset, duration, bar_start)
            if changed:
                self.play(audio, start, new if on_new else old, item.offset, index)
                changed = False
                if self.player.path:
                    self.look = None
            picture = self.picture(start, new, old, on_new, item.offset)
            if redraw:
                self.show(index, proposal, on_new, start, message, picture)
            playing = self.player.path and not self.player.paused and not self.player.ended
            key = self.keys.read(timeout=0.1 if playing else 0.5)
            redraw = key is not None
            if key is None:
                replace_first_line(self.header(index, start))
                picture = self.picture(start, new, old, on_new, item.offset)
                if picture != self.drawn:
                    replace_lines(self.picture_row, picture)
                    self.drawn = picture
                continue
            message = ""
            if key in ("q", "eof"):
                return "quit"
            if key == "b":
                return "back" if index else "next"
            if key == "s":
                return "next"
            if key == "y":
                _save(item.track.id, {"key": item.key, "bpm": proposal.bpm, "phase": proposal.phase,
                                      "downbeat": bar_start, "offset": item.offset})
                self.accepted += 1
                return "next"
            if key == "n":
                _save(item.track.id, {"key": item.key, "keep": True})
                self.kept += 1
                return "next"
            if key in ("h", "x"):
                other = beats.on_the_kick(low, _other_level(item.result, proposal, 0.5 if key == "h" else 2))
                if other.bpm < 50 or other.bpm > 250:
                    message = "this tempo is out of range"
                else:
                    proposal, bar_start, on_new, changed = other, other.downbeat, True, True
            elif key == "d":
                bar_start, on_new, changed = (bar_start + 1) % 4, True, True
            elif key in ("o", "t"):
                step = (0.5 if key == "o" else 1 / 3) * 60 / proposal.bpm
                proposal = beats.refit(env, replace(proposal, phase=proposal.phase + step))
                on_new, changed = True, True
            elif key == "f":
                proposal = beats.refit(env, replace(proposal, phase=beats.first_hit(env)))
                hit = proposal.phase - item.offset  # in rekordbox time
                bar_start = int(hit // (60 / proposal.bpm)) % 4
                start, on_new, changed = 0.0, True, True
            elif key == "c":
                on_new, changed = not on_new, True
            elif key in ("right", "left"):
                step = PREVIEW_SECONDS if key == "right" else -PREVIEW_SECONDS
                start, changed = max(min(start + step, duration - PREVIEW_SECONDS), 0), True
            elif key in ("0", "home"):
                start, changed = 0.0, True
            elif key == " ":
                self.player.pause()
                if not self.player.paused:
                    self.look = None
            elif key == "r":
                changed = True
            elif key in ("+", "=", "-"):
                self.zoom = max(self.zoom - 1, 0) if key != "-" else min(self.zoom + 1, len(ZOOMS) - 1)
            elif key in (",", "."):
                first, span = self.view(start)
                self.look = max(min(first + (span if key == "." else -span), duration - span), 0)

    def view(self, start: float) -> tuple[float, float]:
        """The start and the length of the waveform view: your page, else the page with the playback position."""
        span = ZOOMS[self.zoom]
        if self.look is not None:
            return self.look, span
        position = self.position(start)
        return (start if position is None else start + (position - start) // span * span), span

    def position(self, start: float) -> float | None:
        return start + min(self.player.position(), PREVIEW_SECONDS) if self.player.path else None

    def picture(self, start: float, new, old, on_new: bool, offset: float) -> list[str]:
        """The waveform with the clicks of the two grids, in the time of the audio."""
        width = max(_columns() - LABEL - 1, 40)
        first, span = self.view(start)
        new, old = ([(n, beats.audio_time(t, offset)) for n, t in g] for g in (new, old))
        wave = waveform.render(self.audio, self.bass, beats.SR, first, span, width, WAVE_HEIGHT, self.levels,
                               new if on_new else old)
        return [
            f"{'  better':<{LABEL}}" + waveform.ticks(new, first, span, width, "36"),
            *(" " * LABEL + line for line in wave),
            f"{'  rekordbox':<{LABEL}}" + waveform.ticks(old, first, span, width, "35"),
            " " * LABEL + waveform.playhead(self.position(start), first, span, width),
        ]

    def play(self, audio, start: float, grid_beats: list[tuple[int, float]], offset: float, index: int) -> None:
        if not self.player.available:
            return
        target = os.path.join(self.folder, f"preview-{index}.wav")
        clicks = [(n, beats.audio_time(t, offset)) for n, t in grid_beats]
        beats.preview(audio, start, PREVIEW_SECONDS, clicks, target)
        self.player.play(target, 0)

    def header(self, index: int, start: float) -> str:
        item = self.items[index]
        left = f"[{index + 1}/{len(self.items)}] {item.verdict}"
        right = ""
        if self.player.path:
            state = "■" if self.player.ended else "❚❚" if self.player.paused else "▶"
            right = f"{state} {_clock(start + min(self.player.position(), PREVIEW_SECONDS))}"
        return dim(left) + " " * max(_columns() - len(left) - len(right) - 1, 1) + right

    def show(self, index: int, proposal: beats.Proposal, on_new: bool, start: float, message: str,
             picture: list[str]) -> None:
        item = self.items[index]
        t = item.track
        clear()
        about = [t.album, t.genre or "", str(t.year or "")]
        fit = f"fit {proposal.fit:4.1f}" if proposal.fit else ""
        lines = [
            self.header(index, start), bold(track_name(t)), dim(" · ".join(a for a in about if a)), "",
            f"  rekordbox grid  {item.result['grid_bpm']:6.2f} BPM   fit {item.result['grid_fit']:4.1f}",
            f"  better grid     {proposal.bpm:6.2f} BPM   {fit}",
            dim("  fit: the mean strength of the drum hits on the beats. 1: no relation with the beats."), "",
        ]
        self.picture_row, self.drawn = len(lines) + 1, picture
        lines += [
            *picture,
            dim(f"  up: all the sound · down, red: kick and bass · the lines: the grid that you hear · "
                f"{ZOOMS[self.zoom]:g} s"),
            "",
            accent(f"  You hear the clicks of the {'better' if on_new else 'rekordbox'} grid, from "
                   f"{_clock(start)} to {_clock(start + PREVIEW_SECONDS)}. The high click is beat 1 of the bar."),
            "",
            (f"  {accent('y')} write the better grid · {accent('n')} keep the rekordbox grid · "
             f"{accent('h')} half tempo · {accent('x')} double tempo"),
            (f"  {accent('f')} beat 1 on the first hit of the track · {accent('o')} / {accent('t')} move the grid "
             f"half / a third of a beat · {accent('d')} move beat 1"),
            f"  {accent('c')} clicks of the {'rekordbox' if on_new else 'better'} grid",
            (f"  {accent('←/→')} other part · {accent('0')} start of the track · {accent('space')} pause · "
             f"{accent('r')} again · {accent('+/-')} zoom · {accent(',/.')} pages of the waveform"),
            f"  {accent('s')} skip · {accent('b')} back · {accent('q')} quit (your answers stay saved)",
        ]
        if not self.player.available:
            lines.append(warn("  ffplay is not installed or --no-play is set: you cannot hear the clicks"))
        if message:
            lines.append(warn(f"  {message}"))
        print("\n".join(lines), flush=True)


def _other_level(result: dict, proposal: beats.Proposal, factor: float) -> beats.Proposal:
    """The grid at half or double tempo: from the analysis if it has this tempo, else every other beat."""
    target = proposal.bpm * factor
    for family in result["families"]:
        if abs(family[1] / target - 1) < 0.02:
            return beats.Proposal(*family)
    return replace(proposal, bpm=target, fit=0.0, downbeat=0)


def _apply(db, items: list[Item], decisions: dict[str, dict], yes: bool) -> None:
    changes, problems = [], []
    for item in items:
        decision = decisions.get(item.track.id)
        if not decision or decision.get("keep"):
            continue
        proposal = beats.Proposal(fit=0.0, bpm=decision["bpm"], phase=decision["phase"], downbeat=decision["downbeat"])
        grid_beats = beats.new_grid(proposal, decision["offset"], item.result["duration"], decision["downbeat"])
        try:
            changes.append(grid.prepare_retime(db, item.track, decision["bpm"], grid_beats))
        except grid.GridError as error:
            problems.append((item.track, str(error)))
    print(f"\n{len(changes)} accepted grids to write:")
    for change in changes:
        print(f"  {change.track.bpm or 0:6.2f} -> {change.bpm / 100:6.2f} BPM  {track_name(change.track)}")
    for t, problem in problems:
        print(f"  dj-librarian cannot change {track_name(t)}: {problem}")
    if not changes:
        return
    if not yes and not confirm(f"\nwrite the grids of {len(changes)} tracks?"):
        print("cancelled. dj-librarian wrote nothing.")
        return
    writer.ensure_rekordbox_closed()
    backup = writer.backup(writer.db_path())
    writer.backup_analysis(backup, [p for c in changes for p in (c.dat, c.ext)])
    print(f"backup (master.db and analysis files): {backup}")
    print(f"written: {grid.apply(db, changes)} tracks")
    audio = {item.track.id: item.audio for item in items}
    mark_written({c.track.id: (c.dat, audio[c.track.id]) for c in changes})
    print("\nNext, open rekordbox and make sure that the grids are correct. "
          "Then `dj-librarian apply` moves the cues of dj-librarian to the new phrases.\n"
          "to undo: dj-librarian restore (restores the backup above)")


def _columns() -> int:
    return os.get_terminal_size().columns if sys.stdout.isatty() else 100


def mark_written(files: dict[str, tuple[Path, library.AudioFile]]) -> None:
    """Your answer for the grids that dj-librarian wrote: keep them. If a grid moves the beat away from the loudest
    hits (for example, onto the kick), the next check does not report it again."""
    decisions = store.read_json(store.GRID_DECISIONS, {})
    for track_id, (dat, audio) in files.items():
        decisions[track_id] = {"key": _key(dat, audio), "keep": True, "written": True}
    store.write_json(store.GRID_DECISIONS, decisions)


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"
