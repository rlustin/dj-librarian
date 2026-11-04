"""Ask Claude, through the Claude Code CLI, for the genre of tracks that dj-librarian cannot place."""

import random
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from dj_librarian import checks, claude_genres, library, proposals
from dj_librarian.commands.common import load_checks
from dj_librarian.progress import LiveStatus, Steps


def add_parser(sub) -> None:
    parser = sub.add_parser(
        "ask-claude", help="ask Claude how to tag tracks without a confident genre guess, or unlike their genre")
    parser.add_argument("--playlist", help="only tracks of this playlist")
    parser.add_argument("--limit", type=int, help="ask about at most N tracks")
    parser.add_argument("--model", default=claude_genres.MODEL,
                        help=f"Claude model alias (default: {claude_genres.MODEL})")
    parser.add_argument("--no-web", action="store_true", help="do not let Claude search the web")
    parser.add_argument("--refresh", action="store_true",
                        help="also ask again if the prompt changed or you tagged 10%% more tracks since the answer")
    parser.add_argument("--workers", type=int, default=3, help="parallel Claude calls")
    parser.add_argument("--self-check", type=int, metavar="N",
                        help="instead, ask about N tracks you tagged (genre hidden) and report agreement")
    parser.set_defaults(func=run)


def run(args) -> None:
    db = library.open_db()
    steps = Steps()
    selected, context = proposals.build(db, args.playlist, with_cues=False, steps=steps)
    by_id = {t.id: t for t in context.tracks}
    guessed = proposals.load_guessed_genres()
    decisions = proposals.load_genre_decisions()
    yours = proposals.your_genres(context.tracks, guessed, decisions)
    vocab = claude_genres.vocabulary(yours)
    peers = claude_genres.Peers(context.tracks, yours)
    tags = len(yours)
    steps.step("Find the Bandcamp links in the comments and the backups")
    urls = claude_genres.source_urls(context.tracks)
    steps.done()
    web = not args.no_web

    outliers: dict[str, str] = {}
    if args.self_check:
        # Tracks you tagged yourself, genre hidden: how often does Claude agree with you?
        tagged = [p for p in selected if p.track.id in yours]
        todo = random.Random(0).sample(tagged, min(args.self_check, len(tagged)))
    else:
        answers = claude_genres.load_answers()

        def ask(p: proposals.Proposal, outlier: bool = False) -> bool:
            answer = answers.get(p.track.id)
            if answer is None or outlier and not answer.get("outlier"):
                return True
            return args.refresh and claude_genres.is_stale(answer, tags)

        # Tracks without a genre or with an old guess, and without a confident guess now
        todo = [p for p in selected if p.genre_action in ("unknown", "suggest") and p.track.id not in decisions
                and ask(p)]
        _, _, _, report = load_checks(db, Steps(quiet=True))
        reviewed = checks.load_outliers_reviewed()
        # Tracks that do not sound like your genre, and that you did not check in `review-genres --outliers`
        outliers = {o.track.id: o.genre for o in report.outliers if reviewed.get(o.track.id) != o.genre}
        more = [p for p in selected if p.track.id in outliers and ask(p, outlier=True)]
        print(f"{len(todo)} tracks without a genre, {len(more)} tracks that do not sound like your genre")
        todo += more
        if args.limit is not None:
            todo = todo[: args.limit]
    todo.sort(key=lambda p: p.track.path)  # tracks of the same release go together
    batches = [todo[i:i + claude_genres.BATCH_SIZE] for i in range(0, len(todo), claude_genres.BATCH_SIZE)]
    print(f"{len(todo)} tracks in {len(batches)} calls to Claude ({args.model}, web search {'on' if web else 'off'})")
    if not todo:
        return

    status = LiveStatus(f"ask-claude ({args.model}, web search {'on' if web else 'off'})",
                        total_jobs=len(batches), total_items=len(todo))

    def run(job: int, batch: list[proposals.Proposal]):
        status.start(job, _batch_label(batch), len(batch))
        blocks = [claude_genres.describe(p, by_id, urls.get(p.track.id), peers, outliers.get(p.track.id))
                  for p in batch]
        prompt = claude_genres.build_prompt(blocks, vocab)
        return claude_genres.ask(prompt, args.model, web, on_activity=lambda text: status.activity(job, text))

    results: dict[str, dict] = {}
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {pool.submit(run, job, batch): (job, batch) for job, batch in enumerate(batches)}
    pending = set(futures)
    try:
        while pending:
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            for future in done:
                job, batch = futures[future]
                try:
                    answers_ = future.result()
                except Exception as e:  # noqa: BLE001 - one failed call must not stop the others
                    status.finish(job, [], error=str(e)[:120])
                    continue
                titles = {p.track.id: f"{p.track.artist} - {p.track.title}" for p in batch}
                lines = []
                for answer in answers_:
                    if answer["id"] in titles:
                        results[answer["id"]] = answer | {
                            "model": args.model, "version": claude_genres.PROMPT_VERSION, "tags": tags,
                            "outlier": answer["id"] in outliers}
                        lines.append(f"{titles[answer['id']]} → {answer['genre']} ({answer['confidence']})")
                if not args.self_check:
                    claude_genres.save_answers(claude_genres.load_answers() | results)
                status.finish(job, lines)
            status.render()
    except KeyboardInterrupt:
        print("\nstop: cancel the queued calls and stop the calls in progress...")
        pool.shutdown(wait=False, cancel_futures=True)
        claude_genres.stop_all()
        if not args.self_check:
            print(f"dj-librarian saved {len(results)} answers. Run the command again to continue.")
        return
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    if args.self_check:
        _print_claude_self_check([p for p in todo if p.track.id in results], results, yours)
    else:
        new = Counter(a["genre"] for a in results.values() if a["is_new_genre"])
        print(f"dj-librarian saved {len(results)} answers. "
              "Review them with `dj-librarian review-genres` (candidate c).")
        if new:
            print("new genres proposed: " + ", ".join(f"{g} ({n})" for g, n in new.most_common()))


def _batch_label(batch: list[proposals.Proposal]) -> str:
    folders = {p.track.path.rsplit("/", 1)[0] for p in batch}
    first = batch[0].track
    name = folders.pop().rsplit("/", 1)[-1] if len(folders) == 1 else f"{first.artist} - {first.title} and others"
    return f"{name} ({len(batch)} track{'s' if len(batch) > 1 else ''})"


def _print_claude_self_check(checked, results, yours) -> None:
    if not checked:
        return
    right = [p for p in checked if results[p.track.id]["genre"].lower() == yours[p.track.id].lower()]
    print(f"Claude matched your genre on {len(right)}/{len(checked)} tracks ({100 * len(right) / len(checked):.0f}%)")
    by_confidence = Counter((results[p.track.id]["confidence"], p in right) for p in checked)
    for level in ("high", "medium", "low"):
        total = by_confidence[(level, True)] + by_confidence[(level, False)]
        if total:
            print(f"  {level} confidence: {by_confidence[(level, True)]}/{total} right")
    print("disagreements (yours -> Claude):")
    for p in checked:
        if p not in right:
            a = results[p.track.id]
            print(f"  {p.track.artist} - {p.track.title}: {yours[p.track.id]} -> {a['genre']} ({a['confidence']}): {a['reason']}")
