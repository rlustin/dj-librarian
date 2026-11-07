# dj-librarian

dj-librarian adds metadata to a rekordbox 7 library from an analysis of the audio:

- **Genre**: dj-librarian learns your genres from the tracks that you tagged. Then it fills the empty
  genres, or asks you when it is not sure.
- **Energy and mood**: dj-librarian writes them at the start of the comment, for example
  `E7 Dark/Deep | your note`. Your note stays.
- **Hot cues**: *Start*, *Intro end* and *Full energy*, on the phrases of the rekordbox beat grid.
  dj-librarian does not move or delete your own cues. It can give a label to your cue without a label at the same place.
- **Checks**: duplicates, genre names with two spellings, tracks that do not sound like their genre,
  and tracks that rekordbox analysed at half tempo.

dj-librarian uses [Essentia](https://essentia.upf.edu/) for the audio analysis and
[pyrekordbox](https://github.com/dylanljones/pyrekordbox) to read and write the rekordbox database.

## Before you start

dj-librarian writes into the rekordbox database (`master.db`) and, for `double-bpm` and `check-grids`, into the analysis
files of rekordbox. These formats have no public documentation. Read these rules:

- Close rekordbox before a command writes. dj-librarian stops if rekordbox is open.
- Before each write, dj-librarian makes a backup. `dj-librarian restore` puts the last backup back.
- Each command that writes shows the changes first and asks you to confirm them.
- Keep your own backup of the rekordbox library too.

dj-librarian runs on macOS only, and was tested with rekordbox 7. It does not run on Windows: Essentia has no
Windows package, and dj-librarian uses terminal functions that Windows does not have. rekordbox has no Linux version.

dj-librarian is not affiliated with or endorsed by AlphaTheta or Pioneer DJ.

## Install

You need:

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.14 if necessary)
- rekordbox 7, with your tracks analysed in rekordbox
- optional: `ffplay` from [FFmpeg](https://ffmpeg.org/), to listen to the tracks during the review
- optional: the [Claude Code](https://claude.com/claude-code) CLI, for `ask-claude`

```sh
git clone https://github.com/rlustin/dj-librarian.git
cd dj-librarian
uv sync
uv run dj-librarian --help
```

If pyrekordbox cannot open `master.db`, read the pyrekordbox documentation about the database key.

## Use

The usual order:

```sh
uv run dj-librarian analyze          # analyse the audio files (hours for a large library; you can stop and continue)
uv run dj-librarian report           # write the proposals to report.csv, write nothing to rekordbox
uv run dj-librarian check            # find duplicates, odd genre names, tracks unlike their genre
uv run dj-librarian check-grids      # find wrong beat grids (add --review to listen, then --apply to write)
uv run dj-librarian dedupe           # merge the copies of the same track (add --apply to write)
uv run dj-librarian review-genres    # answer the genre questions, track by track, with the audio
uv run dj-librarian apply            # write genres, comments and cues into rekordbox
```

Other commands:

- `dj-librarian double-bpm`: double the BPM and the beat grid of tracks at half the BPM of their genre.
  Try one track first with `--track`, then make sure in rekordbox that the grid is correct.
- `dj-librarian ask-claude`: ask Claude for the genre of tracks that dj-librarian cannot place.
  `review-genres` shows the answers as suggestions. This command uses your Claude plan.
  It sends to Anthropic, for each track: artist, title, album, label, year, folder name, your comment,
  the Bandcamp link, and the names and genres of similar tracks in your library.
- `dj-librarian review-genres --outliers`: listen to the tracks that do not sound like their genre.
- `dj-librarian restore`: put a backup back. Before it does, it makes a backup of the current state.

Use `--help` on each command for its options. Most commands accept `--playlist` to work on one playlist.

### check-grids

`check-grids` compares the rekordbox beat grid with the drum hits of the audio. The first run takes
about 3 minutes for each 100 tracks (audio files on a network share, 6 workers). You can stop it and continue later.
Then dj-librarian checks only the tracks with a new grid or a new file.
It finds these problems:

- **wrong tempo**: the grid does not follow the beat, for example 126.67 BPM for a track at 95 BPM.
- **drifts**: the tempo is almost right, so the grid moves away from the beat during the track.
- **shifted**: the tempo is right, but the beats of the grid are between the beats of the track.

dj-librarian does not check tracks without a clear beat (ambient, broken rhythms).
In many tracks, the hi-hats or claps on the off-beat are louder than the kick. Thus dj-librarian does not report
a grid half a beat from the strongest hits: rekordbox usually puts it on the kick. The better grid goes on
the kicks if they are clear, else on the beat of rekordbox for a grid that drifts. If the clicks are still
between the kicks, press `o`.
For a wrong tempo, dj-librarian proposes the tempo level (half, normal, double) nearest to the
median BPM of the genre. `--review` plays 30 seconds of each track with a click on each beat.
It also shows the waveform: all the sound upwards, the kick and bass band downwards (red), with the marks of
the two grids. The lines through the waveform are the grid that you hear. The view follows the playback.

| Key | Action |
|-----|--------|
| `y` / `n` | write the better grid / keep the rekordbox grid |
| `h` / `x` | half / double tempo |
| `f` | put beat 1 on the first hit of the track, and play from the start |
| `o` / `t` | move the grid by half a beat / a third of a beat, if the clicks are between the kicks |
| `d` | move beat 1 of the bar (the high click) by one beat |
| `c` | hear the clicks of the rekordbox grid, then of the better grid again |
| `←` / `→`, space, `r` | another part of the track, pause, play again |
| `0` (or Home) | play from the start of the track, to see and hear the first beats |
| `+` / `-` | zoom the waveform in or out (1, 2, 4 or 8 seconds) |
| `,` / `.` | previous / next page of the waveform (for example, during a pause) |
| `s` / `b` / `q` | skip / back / stop |

`--apply` writes the grids that you accepted. The phrases of rekordbox move with the grid. dj-librarian keeps
the grids that it wrote as your answer: the next check does not report them again, unless the grid or the
file changes.
Then `dj-librarian apply` moves the cues of dj-librarian to the new phrases. Your own cues do not move.

### review-genres

Each track plays from its full-energy part. Press one key, without Enter:

| Key | Action |
|-----|--------|
| `1`-`3`, `c` | pick a genre that dj-librarian or Claude proposes |
| `/` | type a genre. Tab completes your genre names. |
| `k` | keep the current genre |
| `a`, then a genre | give the genre to all open tracks of the same release |
| `s` / `b` | skip the track / go back to the previous track and change the answer |
| `←` / `→`, space, `r` | move 30 seconds, pause, play again from the start |
| `q` | stop. dj-librarian saves each answer at once, so you can continue later. |

### How dj-librarian uses your genres

dj-librarian learns only from the genres that you gave: in rekordbox, or in `review-genres`.
It does not learn from its own guesses, because then errors add up. If dj-librarian is sure
(at least half of the vote of the 10 most similar tagged tracks), `apply` fills the genre.
Else, `review-genres` asks you. The more tracks you tag, the better the guesses.

## Files

dj-librarian keeps its files in `~/.cache/dj-librarian`. Set `DJ_LIBRARIAN_HOME` to use another folder.

| File | Contents |
|------|----------|
| `analysis.sqlite` | the audio analysis of each track |
| `genre_decisions.json` | your answers in `review-genres` |
| `grid_decisions.json` | your answers in `check-grids --review` |
| `generated_genres.json`, `generated_cues.json` | what dj-librarian wrote, so that a later run can change only that |
| `outliers_reviewed.json` | your answers in `review-genres --outliers` |
| `claude_genres.json` | the answers of `ask-claude` |
| `source_urls.json` | the Bandcamp links found in the comments and in the backups, for `ask-claude` |
| `backups/` | a copy of `master.db` before each write |
| `models/` | the Essentia models, downloaded at the first `analyze` |

Do not delete `genre_decisions.json`, `grid_decisions.json` and `outliers_reviewed.json`: they hold your work.

## Development

```sh
uv run pytest
uvx ruff check src tests
```

## License

dj-librarian is under the [MIT license](LICENSE-MIT) or the [Apache License 2.0](LICENSE-APACHE), at your option.

The dependencies of dj-librarian have their own licenses. [Essentia](https://essentia.upf.edu/) is under
AGPL-3.0: if you distribute dj-librarian together with Essentia, the AGPL-3.0 applies to that distribution.
`analyze` downloads models from the [Essentia models](https://essentia.upf.edu/models.html) site.
These models are under CC BY-NC-SA 4.0 (non-commercial use). dj-librarian does not include them.

Unless you state otherwise, any contribution that you submit for inclusion in dj-librarian is under
the same two licenses, without additional terms or conditions.
