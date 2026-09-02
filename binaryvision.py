#!/usr/bin/env python3
"""
BinaryVision — Kali-style terminal binary art visualizer.

Renders an input image as an animated stream of 0s and 1s (or ASCII-art
density characters) directly in the terminal, in the visual spirit of
Kali Linux's "Hollywood" cosmetic terminal tool.

This program is purely a visual/cosmetic terminal animation. It performs
no network activity, no scanning, and no offensive security actions of
any kind — it only reads a local image file and draws characters.

Author: BinaryVision project
License: MIT
"""

import argparse
import os
import queue
import random
import select
import shutil
import sys
import time

try:
    import termios
    import tty
    HAS_TERMIOS = True
except ImportError:  # pragma: no cover - non-POSIX platforms
    HAS_TERMIOS = False

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:
    sys.stderr.write(
        "Error: Pillow is required but not installed.\n"
        "Install it with:  pip3 install Pillow\n"
    )
    sys.exit(1)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

# Density ramp used for --mode ascii, ordered from "empty" to "dense".
ASCII_RAMP = " .:-=+*#%@"

COLOR_PALETTES = {
    "green": (60, 255, 110),
    "cyan": (60, 230, 255),
    "red": (255, 70, 60),
    "white": (235, 235, 235),
}

ANSI_HIDE_CURSOR = "\x1b[?25l"
ANSI_SHOW_CURSOR = "\x1b[?25h"
ANSI_HOME = "\x1b[H"
ANSI_CLEAR = "\x1b[2J"
ANSI_RESET = "\x1b[0m"
ANSI_ALT_ON = "\x1b[?1049h"
ANSI_ALT_OFF = "\x1b[?1049l"
ANSI_DIM = "\x1b[2m"
ANSI_BOLD = "\x1b[1m"

FRAME_INTRO_SECONDS = 1.4   # length of the "materializing" intro for binary/ascii modes
STEADY_GLITCH_RATE = 0.012  # fraction of cells that flicker per frame once settled
SCAN_SWEEP_SECONDS = 1.8    # time for the scan bar to cross the whole image once


# --------------------------------------------------------------------------
# Image loading & validation
# --------------------------------------------------------------------------

class BinaryVisionError(Exception):
    """Raised for user-facing, recoverable errors (bad file, bad args, etc)."""


def validate_image_path(path: str) -> None:
    if not os.path.exists(path):
        raise BinaryVisionError(f"File not found: '{path}'")
    if not os.path.isfile(path):
        raise BinaryVisionError(f"Not a regular file: '{path}'")
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise BinaryVisionError(
            f"Unsupported file extension '{ext}'. Supported types: {supported}"
        )


def load_image(path: str) -> Image.Image:
    """Load and validate an image, returning an RGB PIL Image."""
    validate_image_path(path)
    try:
        img = Image.open(path)
        img.load()  # force full decode now so corrupt files fail fast, here
    except UnidentifiedImageError as exc:
        raise BinaryVisionError(f"Could not decode image '{path}': not a valid image file") from exc
    except Exception as exc:  # noqa: BLE001 - surface any decode problem clearly
        raise BinaryVisionError(f"Could not decode image '{path}': {exc}") from exc
    return img.convert("RGB")


# --------------------------------------------------------------------------
# Image -> character grid conversion
# --------------------------------------------------------------------------

def compute_grid_size(img_w: int, img_h: int, max_cols: int, max_rows: int,
                       char_aspect: float = 0.5):
    """
    Work out how many character columns/rows to render the image at,
    fitting inside (max_cols, max_rows) while preserving the image's
    approximate visual aspect ratio.

    char_aspect compensates for terminal character cells being roughly
    twice as tall as they are wide.
    """
    max_cols = max(1, max_cols)
    max_rows = max(1, max_rows)
    img_ratio = img_h / float(img_w)

    cols = max_cols
    rows = max(1, int(round(cols * img_ratio * char_aspect)))

    if rows > max_rows:
        rows = max_rows
        cols = max(1, int(round(rows / (img_ratio * char_aspect))))
        cols = min(cols, max_cols)

    return max(1, cols), max(1, rows)


def build_brightness_grid(img: Image.Image, cols: int, rows: int):
    """Resize+grayscale the image into a cols x rows grid of 0-255 ints."""
    gray = img.convert("L").resize((cols, rows), Image.LANCZOS)
    pixels = list(gray.getdata())
    return [pixels[r * cols:(r + 1) * cols] for r in range(rows)]


def brightness_to_char(value: int, mode: str, threshold: int, invert: bool) -> str:
    if invert:
        value = 255 - value
    if mode == "ascii":
        idx = int(value / 255.0 * (len(ASCII_RAMP) - 1))
        return ASCII_RAMP[idx]
    # binary / scan / matrix all render as pure 0/1
    return "1" if value >= threshold else "0"


def build_target_grid(brightness_grid, mode: str, threshold: int, invert: bool):
    return [
        [brightness_to_char(v, mode, threshold, invert) for v in row]
        for row in brightness_grid
    ]


def brightness_level(value: int, levels: int = 6) -> int:
    """Bucket a 0-255 brightness value into 0..levels-1 for color shading."""
    lvl = int(value * levels / 256)
    return max(0, min(levels - 1, lvl))


# --------------------------------------------------------------------------
# Terminal color helpers
# --------------------------------------------------------------------------

def truecolor_fg(r: int, g: int, b: int) -> str:
    return f"\x1b[38;2;{r};{g};{b}m"


def scaled_color(base_rgb, level: int, levels: int = 6) -> str:
    scale = 0.22 + (level / (levels - 1)) * 0.78
    r, g, b = base_rgb
    return truecolor_fg(int(r * scale), int(g * scale), int(b * scale))


def render_row(chars, brightnesses, base_rgb, no_color: bool) -> str:
    """
    Render one row of characters with brightness-shaded color, run-length
    encoding consecutive equal-shade runs so we emit only a handful of
    ANSI escape sequences per row instead of one per character.
    """
    if no_color or base_rgb is None:
        return "".join(chars)

    parts = []
    buf = []
    last_level = None
    for ch, b in zip(chars, brightnesses):
        level = brightness_level(b)
        if level != last_level:
            if buf:
                parts.append("".join(buf))
                buf = []
            parts.append(scaled_color(base_rgb, level))
            last_level = level
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    parts.append(ANSI_RESET)
    return "".join(parts)


# --------------------------------------------------------------------------
# Non-blocking keyboard input (POSIX only; degrades gracefully elsewhere)
# --------------------------------------------------------------------------

class KeyListener:
    """
    Reads single keypresses from stdin in the background without blocking
    the render loop. Falls back to "no input available" when stdin isn't
    an interactive TTY (e.g. piped input, non-interactive test runs).
    """

    def __init__(self):
        self.enabled = HAS_TERMIOS and sys.stdin.isatty()
        self._old_settings = None
        self._fd = None
        self._queue = queue.Queue()
        self._running = False

    def __enter__(self):
        if self.enabled:
            self._fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)  # cbreak keeps Ctrl+C -> SIGINT working
        self._running = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._running = False
        if self.enabled and self._old_settings is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
        return False

    def poll(self):
        """Return a single pending keypress character, or None."""
        if not self.enabled:
            return None
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            try:
                return sys.stdin.read(1)
            except Exception:  # noqa: BLE001
                return None
        return None


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------

class BinaryVisionApp:
    HEADER_LINES = 6   # box (3) + blank + 2 status lines
    FOOTER_LINES = 2   # blank + controls line

    def __init__(self, args):
        self.args = args
        self.color_rgb = None if args.no_color else COLOR_PALETTES[args.color]
        self.no_color = args.no_color

        self.image = load_image(args.image)
        self.img_w, self.img_h = self.image.size

        term_cols, term_rows = shutil.get_terminal_size(fallback=(80, 24))
        self.term_cols, self.term_rows = term_cols, term_rows

        avail_cols = max(10, term_cols - 2)
        avail_rows = max(5, term_rows - self.HEADER_LINES - self.FOOTER_LINES)

        self.cols, self.rows = compute_grid_size(self.img_w, self.img_h, avail_cols, avail_rows)

        self.brightness_grid = build_brightness_grid(self.image, self.cols, self.rows)
        self.target_grid = build_target_grid(
            self.brightness_grid, args.mode, args.threshold, args.invert
        )

        self.mode = args.mode
        self.fps = max(1, min(60, args.fps))
        self.frame_interval = 1.0 / self.fps

        self.paused = False
        self.quit = False
        self.start_time = time.perf_counter()
        self.pause_accum = 0.0
        self._pause_started_at = None

        # Mode-specific state
        self._init_mode_state()

    # -- setup -------------------------------------------------------------

    def _init_mode_state(self):
        chars = "01"
        self.random_grid = [
            [random.choice(chars) for _ in range(self.cols)] for _ in range(self.rows)
        ]

        if self.mode == "scan":
            self.revealed = [[False] * self.cols for _ in range(self.rows)]
            self.scan_pos = 0.0

        if self.mode == "matrix":
            self.revealed = [[False] * self.cols for _ in range(self.rows)]
            self.head_pos = []
            self.speed = []
            self.trail_len = []
            for c in range(self.cols):
                col_vals = [self.brightness_grid[r][c] for r in range(self.rows)]
                norm = (sum(col_vals) / len(col_vals) / 255.0) if col_vals else 0.5
                self.speed.append(0.35 + norm * 1.65)
                self.trail_len.append(int(3 + norm * 7))
                self.head_pos.append(-random.uniform(0, max(self.rows, 1)))

    def restart(self):
        self.start_time = time.perf_counter()
        self.pause_accum = 0.0
        self._pause_started_at = None
        self._init_mode_state()

    # -- timing --------------------------------------------------------

    def elapsed(self) -> float:
        now = time.perf_counter()
        pause_time = self.pause_accum
        if self.paused and self._pause_started_at is not None:
            pause_time += now - self._pause_started_at
        return now - self.start_time - pause_time

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self._pause_started_at = time.perf_counter()
        else:
            if self._pause_started_at is not None:
                self.pause_accum += time.perf_counter() - self._pause_started_at
            self._pause_started_at = None

    # -- per-frame state update ------------------------------------------

    def _update_binary_ascii(self, elapsed):
        """Materialize intro, then settle into a steady image with light glitch."""
        if elapsed < FRAME_INTRO_SECONDS:
            reveal_prob = elapsed / FRAME_INTRO_SECONDS
        else:
            reveal_prob = 1.0 - STEADY_GLITCH_RATE

        chars = "01" if self.mode != "ascii" else ASCII_RAMP
        grid = [[None] * self.cols for _ in range(self.rows)]
        for r in range(self.rows):
            target_row = self.target_grid[r]
            for c in range(self.cols):
                if random.random() < reveal_prob:
                    grid[r][c] = target_row[c]
                else:
                    grid[r][c] = random.choice(chars)
        return grid, reveal_prob

    def _update_scan(self, elapsed):
        rows = self.rows
        sweep = (elapsed / SCAN_SWEEP_SECONDS) * rows
        self.scan_pos = sweep % (rows * 2)  # ping-pong across 0..rows..0
        if self.scan_pos <= rows:
            band = self.scan_pos
        else:
            band = (rows * 2) - self.scan_pos

        grid = [[None] * self.cols for _ in range(self.rows)]
        revealed_count = 0
        for r in range(self.rows):
            if r <= band:
                for c in range(self.cols):
                    self.revealed[r][c] = True
            row_revealed = self.revealed[r]
            target_row = self.target_grid[r]
            for c in range(self.cols):
                if row_revealed[c]:
                    revealed_count += 1
                    if random.random() < STEADY_GLITCH_RATE:
                        grid[r][c] = random.choice("01")
                    else:
                        grid[r][c] = target_row[c]
                else:
                    grid[r][c] = random.choice("01")
        # highlight the scan line itself
        band_row = int(band)
        if 0 <= band_row < self.rows:
            grid[band_row] = ["1" if random.random() < 0.5 else "0" for _ in range(self.cols)]

        progress = revealed_count / float(self.rows * self.cols)
        return grid, progress, band_row

    def _update_matrix(self, elapsed, dt):
        rows, cols = self.rows, self.cols
        grid = [[" "] * cols for _ in range(rows)]
        levels_grid = [[0] * cols for _ in range(rows)]
        revealed_count = 0

        speed_scale = self.fps / 24.0  # keep apparent speed ~fps independent
        for c in range(cols):
            self.head_pos[c] += self.speed[c] * max(0.2, min(3.0, speed_scale)) * 0.6
            if self.head_pos[c] - self.trail_len[c] > rows:
                self.head_pos[c] = -random.uniform(0, rows * 0.6)

            head = self.head_pos[c]
            trail = self.trail_len[c]
            for r in range(rows):
                dist = head - r
                if 0 <= dist < 1:
                    grid[r][c] = random.choice("01")
                    levels_grid[r][c] = 5
                    if random.random() < 0.18:
                        self.revealed[r][c] = True
                elif 1 <= dist < trail + 1:
                    fade = 1.0 - (dist / (trail + 1))
                    grid[r][c] = random.choice("01")
                    levels_grid[r][c] = max(1, int(fade * 4))
                elif self.revealed[r][c]:
                    grid[r][c] = self.target_grid[r][c]
                    levels_grid[r][c] = max(1, brightness_level(self.brightness_grid[r][c]) - 1)
                else:
                    if random.random() < 0.02:
                        grid[r][c] = random.choice("01")
                        levels_grid[r][c] = 0
                    else:
                        grid[r][c] = " "
                        levels_grid[r][c] = 0

        for r in range(rows):
            revealed_count += sum(1 for c in range(cols) if self.revealed[r][c])
        progress = revealed_count / float(rows * cols)
        return grid, levels_grid, progress

    # -- rendering -----------------------------------------------------

    def _header_lines(self, status_line: str, progress_line: str):
        width = max(46, min(self.term_cols, 100))
        inner = width - 2
        top = "\u2554" + ("\u2550" * inner) + "\u2557"
        bottom = "\u255a" + ("\u2550" * inner) + "\u255d"
        title = " BINARYVISION // IMAGE SCAN"
        title_line = "\u2551" + title.ljust(inner) + "\u2551"

        def clip(s):
            return s if len(s) <= self.term_cols else s[: self.term_cols - 1]

        lines = [clip(top), clip(title_line), clip(bottom), clip(status_line), clip(progress_line)]
        return lines

    def _status_text(self):
        fname = os.path.basename(self.args.image)
        return (
            f" file: {fname}  |  image: {self.img_w}x{self.img_h}  |  "
            f"term: {self.term_cols}x{self.term_rows}  |  grid: {self.cols}x{self.rows}"
        )

    def _progress_text(self, extra: str):
        state = "PAUSED" if self.paused else "RUNNING"
        return f" mode: {self.mode}  |  color: {'off' if self.no_color else self.args.color}  |  fps: {self.fps}  |  {extra}  |  status: {state}"

    def render_frame(self, dt: float) -> str:
        elapsed = self.elapsed()

        if self.mode in ("binary", "ascii"):
            grid, reveal_prob = self._update_binary_ascii(elapsed)
            level_source = self.brightness_grid
            extra = f"reveal: {min(100, int(reveal_prob * 100))}%"
        elif self.mode == "scan":
            grid, progress, _band = self._update_scan(elapsed)
            level_source = self.brightness_grid
            extra = f"reveal: {int(progress * 100)}%"
        else:  # matrix
            grid, level_grid, progress = self._update_matrix(elapsed, dt)
            level_source = None
            extra = f"reveal: {int(progress * 100)}%"

        out_lines = self._header_lines(self._status_text(), self._progress_text(extra))

        pad_w = self.term_cols
        body_lines = []
        for r in range(self.rows):
            row_chars = grid[r]
            if self.mode == "matrix":
                if self.no_color:
                    line = "".join(row_chars)
                else:
                    line = self._render_matrix_row(row_chars, level_grid[r])
            else:
                brightnesses = self.brightness_grid[r]
                line = render_row(row_chars, brightnesses, self.color_rgb, self.no_color)
            body_lines.append(line)

        footer = [
            "",
            " [SPACE] pause/resume   [R] restart   [Q] quit   [Ctrl+C] exit",
        ]

        all_lines = out_lines + body_lines + footer
        # Pad/truncate each visible line so leftover characters from a
        # previous, longer frame never linger on screen.
        padded = []
        for line in all_lines:
            visible_len = _visible_length(line)
            if visible_len < pad_w:
                line = line + " " * (pad_w - visible_len)
            padded.append(line)

        return ANSI_HOME + "\n".join(padded) + ANSI_RESET

    def _render_matrix_row(self, chars, levels):
        parts = []
        buf = []
        last_level = None
        for ch, lvl in zip(chars, levels):
            if lvl != last_level:
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append(scaled_color(self.color_rgb, lvl))
                last_level = lvl
            buf.append(ch)
        if buf:
            parts.append("".join(buf))
        parts.append(ANSI_RESET)
        return "".join(parts)

    # -- main loop -------------------------------------------------------

    def run(self):
        with KeyListener() as keys:
            sys.stdout.write(ANSI_ALT_ON + ANSI_HIDE_CURSOR + ANSI_CLEAR)
            sys.stdout.flush()
            try:
                last = time.perf_counter()
                while not self.quit:
                    frame_start = time.perf_counter()
                    dt = frame_start - last
                    last = frame_start

                    key = keys.poll()
                    if key:
                        self._handle_key(key)

                    if self.quit:
                        break

                    frame = self.render_frame(dt)
                    sys.stdout.write(frame)
                    sys.stdout.flush()

                    elapsed_render = time.perf_counter() - frame_start
                    sleep_for = self.frame_interval - elapsed_render
                    if sleep_for > 0:
                        time.sleep(sleep_for)
            except KeyboardInterrupt:
                pass
            finally:
                sys.stdout.write(ANSI_SHOW_CURSOR + ANSI_ALT_OFF)
                sys.stdout.flush()

    def _handle_key(self, key: str):
        if key in ("q", "Q"):
            self.quit = True
        elif key == " ":
            self.toggle_pause()
        elif key in ("r", "R"):
            self.restart()


def _visible_length(s: str) -> int:
    """Length of a string ignoring ANSI escape sequences."""
    length = 0
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "\x1b" and i + 1 < n and s[i + 1] == "[":
            j = i + 2
            while j < n and not s[j].isalpha():
                j += 1
            i = j + 1
        else:
            length += 1
            i += 1
    return length


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="binaryvision.py",
        description="Render an image as an animated binary/ASCII hacker-terminal visualization.",
        epilog=(
            "Examples:\n"
            "  python3 binaryvision.py photo.jpg\n"
            "  python3 binaryvision.py photo.jpg --mode binary\n"
            "  python3 binaryvision.py photo.jpg --mode scan --color green\n"
            "  python3 binaryvision.py photo.jpg --mode matrix --fps 30\n"
            "  python3 binaryvision.py photo.jpg --mode ascii --color cyan --no-color\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("image", help="Path to the input image (png, jpg, jpeg, bmp, webp)")
    parser.add_argument(
        "--mode", choices=["binary", "ascii", "matrix", "scan"], default="scan",
        help="Display mode (default: scan)",
    )
    parser.add_argument(
        "--color", choices=sorted(COLOR_PALETTES.keys()), default="green",
        help="Color palette (default: green)",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    parser.add_argument("--fps", type=int, default=24, help="Target animation frame rate (default: 24)")
    parser.add_argument(
        "--threshold", type=int, default=128,
        help="Brightness threshold 0-255 used for 0/1 mapping (default: 128)",
    )
    parser.add_argument("--invert", action="store_true", help="Invert the brightness mapping")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if not (0 <= args.threshold <= 255):
        sys.stderr.write("Error: --threshold must be between 0 and 255.\n")
        return 2
    if args.fps < 1:
        sys.stderr.write("Error: --fps must be at least 1.\n")
        return 2

    try:
        app = BinaryVisionApp(args)
    except BinaryVisionError as exc:
        sys.stderr.write(f"BinaryVision error: {exc}\n")
        return 1
    except Exception as exc:  # noqa: BLE001 - final safety net for a clean CLI error
        sys.stderr.write(f"Unexpected error: {exc}\n")
        return 1

    try:
        app.run()
    except Exception as exc:  # noqa: BLE001
        # Make sure the terminal is always left in a sane state.
        sys.stdout.write(ANSI_SHOW_CURSOR + ANSI_ALT_OFF)
        sys.stdout.flush()
        sys.stderr.write(f"BinaryVision crashed: {exc}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
