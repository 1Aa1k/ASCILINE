#!/usr/bin/env python3
"""
ASCILINE logo.

Prints the ASCILINE wordmark as an ANSI-shadow block banner. With --animate it
glitches the letters through ASCILINE's own density ramp (@%#*+=-:.) in the
terminal -- the logo doing what the engine does.

Usage:
    python logo.py              # print the static banner
    python logo.py --animate    # run the density-ramp glitch (Ctrl-C to stop)
    python logo.py --no-color   # plain, no ANSI color
"""
import sys
import time
import random
import shutil

# Static wordmark + play arrow, rendered once (ANSI-shadow). Kept inline so the
# script has zero dependencies.
WORDMARK = r"""
 █████╗ ███████╗ ██████╗██╗██╗     ██╗███╗   ██╗███████╗  ██
██╔══██╗██╔════╝██╔════╝██║██║     ██║████╗  ██║██╔════╝  █████
███████║███████╗██║     ██║██║     ██║██╔██╗ ██║█████╗    ████████
██╔══██║╚════██║██║     ██║██║     ██║██║╚██╗██║██╔══╝    ████████
██║  ██║███████║╚██████╗██║███████╗██║██║ ╚████║███████╗  █████
╚═╝  ╚═╝╚══════╝ ╚═════╝╚═╝╚══════╝╚═╝╚═╝  ╚═══╝╚══════╝  ██
""".strip("\n")

TAGLINE = "Watch anything as ASCII."
RAMP = "@%#*+=-:."  # ASCILINE's density ramp = the glitch alphabet

# Column ranges of each glyph (incl. the play arrow), used to glitch one at a time.
LETTERS = [
    (0, 7), (8, 15), (16, 23), (24, 26), (27, 34),
    (35, 37), (38, 47), (48, 55), (58, 65),
]

CYAN, DIM, RESET = "\033[36m", "\033[2m", "\033[0m"


def _color(s, code, enabled):
    return f"{code}{s}{RESET}" if enabled else s


def render_static(color=True):
    out = _color(WORDMARK, CYAN, color)
    return f"{out}\n\n   {_color(TAGLINE, DIM, color)}"


def animate(color=True, seconds=None):
    """Glitch random letters through the density ramp until interrupted."""
    rows = WORDMARK.split("\n")
    width = max(len(r) for r in rows)
    grid = [list(r.ljust(width)) for r in rows]
    home = "\033[H"
    sys.stdout.write("\033[2J\033[?25l")  # clear + hide cursor
    start = time.time()
    try:
        while seconds is None or time.time() - start < seconds:
            lo, hi = random.choice(LETTERS)
            steps = random.randint(5, 9)
            for s in range(steps):
                frame = []
                for r, row in enumerate(grid):
                    line = []
                    for c, ch in enumerate(row):
                        if ch != " " and lo <= c <= hi and random.random() < 0.55:
                            line.append(RAMP[random.randrange(len(RAMP))])
                        else:
                            line.append(ch)
                    frame.append("".join(line))
                art = "\n".join(frame)
                sys.stdout.write(home + _color(art, CYAN, color) + "\n")
                sys.stdout.flush()
                time.sleep(0.05)
            # settle back to the clean glyph
            sys.stdout.write(home + _color(WORDMARK, CYAN, color) + "\n")
            sys.stdout.flush()
            time.sleep(random.uniform(0.3, 1.6))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h")  # restore cursor
        sys.stdout.flush()


def main(argv):
    color = "--no-color" not in argv
    if "--animate" in argv:
        animate(color=color)
    else:
        print(render_static(color=color))


if __name__ == "__main__":
    main(sys.argv[1:])
