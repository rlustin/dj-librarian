import argparse

from dj_librarian.commands import (
    analyze,
    apply,
    ask_claude,
    check,
    dedupe,
    double_bpm,
    report,
    restore,
    review_genres,
)

# In the order of the help: the usual order of use
COMMANDS = [analyze, report, check, dedupe, double_bpm, review_genres, apply, ask_claude, restore]


def main() -> None:
    parser = argparse.ArgumentParser(prog="dj-librarian", description="Metadata helper for rekordbox libraries")
    sub = parser.add_subparsers(required=True)
    for command in COMMANDS:
        command.add_parser(sub)
    args = parser.parse_args()
    args.func(args)
