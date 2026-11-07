"""Single key presses, colours and a line prompt with completion, for the interactive commands."""

import os
import re
import readline  # libedit reads the terminal settings at import: import it before the switch to single keys
import select
import sys
import termios
import tty
from typing import Self

COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
SCREEN = sys.stdout.isatty()
ARROWS = {"C": "right", "D": "left", "A": "up", "B": "down", "H": "home", "F": "end"}
ESCAPE = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z~]|O[A-Za-z])?")


def _style(code: str):
    return lambda text: f"\x1b[{code}m{text}\x1b[0m" if COLOUR else str(text)


bold, dim, accent, warn = _style("1"), _style("2"), _style("1;36"), _style("33")


def clear() -> None:
    if SCREEN:
        print("\x1b[2J\x1b[H", end="", flush=True)


def replace_lines(row: int, lines: list[str]) -> None:
    """Write `lines` from the line `row` (1 is the top) of the screen, then put the cursor back."""
    if SCREEN:
        text = "".join(f"\x1b[{row + k};1H\x1b[2K{line}" for k, line in enumerate(lines))
        print(f"\x1b7{text}\x1b8", end="", flush=True)


def replace_first_line(text: str) -> None:
    replace_lines(1, [text])


class Keys:
    """Read one key press at a time. Outside a terminal, read the input one character at a time."""

    def __init__(self) -> None:
        self.tty = sys.stdin.isatty()
        self.saved = None
        self.typed = ""  # keys that came in one read, after the key that read() returned

    def __enter__(self) -> Self:
        if self.tty:
            self.saved = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin)
        return self

    def __exit__(self, *exc) -> None:
        if self.saved:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.saved)

    def read(self, timeout: float | None = None) -> str | None:
        """A character, or the name of a special key ("left", "escape"). None after `timeout` seconds."""
        if not self.tty:
            return sys.stdin.read(1) or "eof"
        if not self.typed:
            if not select.select([sys.stdin], [], [], timeout)[0]:
                return None
            # os.read, not sys.stdin.read: the buffer of sys.stdin can hide the rest of an escape sequence from select
            self.typed = os.read(sys.stdin.fileno(), 64).decode(errors="ignore")
            if not self.typed:
                return "eof"
        if escape := ESCAPE.match(self.typed):
            self.typed = self.typed[escape.end():]
            sequence = escape.group()
            return "escape" if sequence == "\x1b" else ARROWS.get(sequence[-1], "unknown")
        key, self.typed = self.typed[0], self.typed[1:]
        return key

    def ask(self, question: str) -> bool:
        print(f"{question} [y/n] ", end="", flush=True)
        while (answer := self.read()) not in ("y", "n", "escape", "eof"):
            pass
        print(answer if answer in ("y", "n") else "")
        return answer == "y"

    def line(self, prompt: str, completions: list[str]) -> str:
        """A line of text. Tab completes from `completions`. The terminal is in line mode during the input."""
        # Keys typed fast after the key that opened the prompt are the start of the text, or all of it
        line, enter, rest = self.typed.replace("\r", "\n").partition("\n")
        if enter:
            self.typed = rest
            print(prompt + line)
            return line.strip()
        self.typed = ""

        def complete(text: str, state: int) -> str | None:
            found = [c for c in completions if c.lower().startswith(text.lower())]
            return found[state] if state < len(found) else None

        old = readline.get_completer(), readline.get_completer_delims()
        readline.set_completer(complete)
        readline.set_completer_delims("")  # A genre name can contain spaces
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        if self.saved:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.saved)
        readline.set_startup_hook(lambda: readline.insert_text(line))
        try:
            return input(prompt).strip()
        except EOFError:
            return ""
        finally:
            readline.set_startup_hook(None)
            readline.set_completer(old[0])
            readline.set_completer_delims(old[1])
            if self.saved:
                tty.setcbreak(sys.stdin)
