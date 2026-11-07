"""A zoomed waveform with beat grids, drawn in the terminal with braille characters (2 x 4 dots each)."""

import numpy as np

from dj_librarian import terminal

BRAILLE = 0x2800
DOT = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [row][column] in one character
ALL, BASS = "37", "31"
BEAT, BAR = "48;5;238", "48;5;242"  # background of the lines of the grid that you hear


def _paint(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if terminal.COLOUR else text


def low_pass(audio: np.ndarray, sr: int) -> np.ndarray:
    import essentia.standard as es

    return es.LowPass(cutoffFrequency=150, sampleRate=sr)(audio)


def scale(audio: np.ndarray) -> float:
    """The level of the loud peaks: the full height of the waveform."""
    return float(np.percentile(np.abs(audio[::16]), 99.9)) or 1.0


def _peaks(signal: np.ndarray, edges: np.ndarray) -> np.ndarray:
    edges = np.clip(edges, 0, len(signal))
    peaks = np.zeros(len(edges) - 1, dtype=np.float32)
    for k in np.flatnonzero(edges[1:] > edges[:-1]):
        peaks[k] = np.abs(signal[edges[k]:edges[k + 1]]).max()
    return peaks


def _positions(grid_beats, start: float, seconds: float, width: int) -> dict[int, tuple[int, int]]:
    """Character column -> (dot column 0 or 1, beat number in the bar), for the beats in the view."""
    columns = {}
    for number, t in grid_beats:
        dot = int((t - start) / seconds * width * 2)
        if 0 <= dot < width * 2:
            columns[dot // 2] = (dot % 2, number)
    return columns


def ticks(grid_beats, start: float, seconds: float, width: int, code: str) -> str:
    """One line: a thin mark on each beat, a thick mark on beat 1. The left or right half of a character shows
    the position to half a character."""
    line = [" "] * width
    for x, (half, number) in _positions(grid_beats, start, seconds, width).items():
        mark = ("▌", "▐")[half] if number == 1 else ("▏", "▕")[half]
        line[x] = _paint(mark, "1;" + code if number == 1 else code)
    return "".join(line)


def render(audio: np.ndarray, bass: np.ndarray, sr: int, start: float, seconds: float, width: int, height: int,
           levels: tuple[float, float], heard) -> list[str]:
    """`height` lines from `start` for `seconds`: all the sound upwards, the kick and bass band downwards.
    `heard`: (beat number, audio time) of the grid that you hear, drawn as lines through the waveform."""
    edges = (start * sr + np.arange(width * 2 + 1) * seconds * sr / (width * 2)).astype(int)
    top, bottom = height // 2 * 4, (height - height // 2) * 4  # dots
    dots = np.zeros((top + bottom, width * 2), dtype=bool)
    for column, size in enumerate(np.minimum(np.round(_peaks(audio, edges) / levels[0] * top), top).astype(int)):
        dots[top - size: top, column] = True
    for column, size in enumerate(np.minimum(np.round(_peaks(bass, edges) / levels[1] * bottom), bottom).astype(int)):
        dots[top: top + size, column] = True
    lines_at = _positions(heard, start, seconds, width)
    lines = []
    for row in range(height):
        colour = ALL if row * 4 < top else BASS
        cells = []
        for x in range(width):
            block = dots[row * 4: row * 4 + 4, x * 2: x * 2 + 2]
            code = sum(DOT[r][c] for r in range(4) for c in range(2) if block[r, c])
            if x in lines_at and not terminal.COLOUR:
                cells.append(chr(BRAILLE + code) if code else "│")
            elif x in lines_at:
                cells.append(f"\x1b[{colour};{BAR if lines_at[x][1] == 1 else BEAT}m{chr(BRAILLE + code)}\x1b[0m")
            else:
                cells.append(_paint(chr(BRAILLE + code), colour) if code else " ")
        lines.append("".join(cells))
    return lines


def playhead(position: float | None, start: float, seconds: float, width: int) -> str:
    """The times at the two ends of the view, and a mark at the position of the playback."""
    left, right = _clock(start), _clock(start + seconds)
    line = list(left + " " * max(width - len(left) - len(right), 1) + right)[:width]
    if position is not None and start <= position < start + seconds:
        line[int((position - start) / seconds * width)] = _paint("▲", "1")
    return "".join(line)


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"
