import numpy as np

from dj_librarian import waveform

SR = 1000


def test_ticks_show_the_beat_to_half_a_character():
    beats = [(1, 0.0), (2, 0.2), (3, 0.55)]  # 20 dot columns: 0, 4 and 11

    line = waveform.ticks(beats, start=0.0, seconds=1.0, width=10, code="36")

    assert line == "▌ ▏  ▕    "  # beat 1 is thick, beat 3 is in the right half of its character


def test_render_draws_the_sound_up_and_the_bass_down():
    audio = np.zeros(SR, dtype=np.float32)
    bass = np.zeros(SR, dtype=np.float32)
    audio[500:600] = 1.0  # a hit in the middle, on the two dot columns of the character 5
    bass[500:600] = 0.5

    lines = waveform.render(audio, bass, SR, 0.0, 1.0, width=10, height=4, levels=(1.0, 1.0), heard=[])

    assert [line[5] for line in lines] == ["⣿", "⣿", "⣿", " "]  # full height up, half height down
    assert all(set(line[:5] + line[6:]) == {" "} for line in lines)


def test_render_draws_the_grid_that_you_hear_without_colour():
    silence = np.zeros(SR, dtype=np.float32)

    lines = waveform.render(silence, silence, SR, 0.0, 1.0, width=10, height=2, levels=(1.0, 1.0),
                            heard=[(1, 0.3)])

    assert lines == ["   │      "] * 2


def test_playhead():
    line = waveform.playhead(1.5, start=1.0, seconds=1.0, width=20)

    assert line.startswith("0:01.0") and line.endswith("0:02.0") and line[10] == "▲"
