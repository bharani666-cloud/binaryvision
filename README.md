# BinaryVision

A Kali-Linux-style terminal visualizer that turns any photo into an animated
stream of `0`s and `1`s (or brightness-shaded ASCII art), rendered live in
your terminal with a green/cyan "hacker terminal" aesthetic — in the visual
spirit of the Kali `hollywood` cosmetic tool.

**BinaryVision is a purely visual/cosmetic terminal animation.** It reads a
local image file and draws characters to your terminal. It performs **no**
network activity, scanning, exploitation, credential access, persistence, or
any other offensive-security function of any kind.

```
╔══════════════════════════════════════════════════════════════════════╗
║ BINARYVISION // IMAGE SCAN                                            ║
╚══════════════════════════════════════════════════════════════════════╝
 file: photo.jpg  |  image: 1920x1080  |  term: 120x40  |  grid: 118x33
 mode: scan  |  color: green  |  fps: 24  |  reveal: 74%  |  status: RUNNING
 01001110101101100101101101101011010101001101011010110101101101010110
 10110101101011010101011010101101011010101101010110101101011010101101
 11010110101101011010110101101101011010101101011010110101101011010101
 ...
```

---

## 1. Installation on Kali Linux

BinaryVision needs only Python 3 (already installed on Kali) and Pillow.

```bash
# Clone or copy the project onto your machine, then:
cd binaryvision

# (Recommended) use a virtual environment to avoid touching system packages
python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

If you prefer not to use a virtualenv on Kali (which uses an externally
managed Python environment), install with:

```bash
pip install -r requirements.txt --break-system-packages
```

or via apt:

```bash
sudo apt install python3-pil
```

No other dependencies are required — BinaryVision draws its "hacker
terminal" look with raw ANSI escape sequences, so it works in any standard
Linux terminal (including Kali's default terminal, GNOME Terminal, xterm,
tmux, etc.) without needing `rich` or `colorama`.

---

## 2. Usage

```bash
python3 binaryvision.py <image>
```

### Examples

```bash
python3 binaryvision.py photo.jpg
python3 binaryvision.py photo.jpg --mode binary
python3 binaryvision.py photo.jpg --mode scan --color green
python3 binaryvision.py photo.jpg --mode matrix --fps 30
python3 binaryvision.py photo.jpg --mode ascii --color cyan
python3 binaryvision.py photo.jpg --no-color
python3 binaryvision.py photo.jpg --threshold 100 --invert
```

### Supported image formats

`.png`, `.jpg` / `.jpeg`, `.bmp`, `.webp`

The file is validated before anything else runs: BinaryVision checks that
the path exists, is a regular file, has a supported extension, and can
actually be decoded by Pillow — giving a clean, human-readable error instead
of a stack trace if something's wrong.

### Command-line options

| Flag | Values | Default | Description |
|---|---|---|---|
| `image` | path | — | Input image file (required, positional) |
| `--mode` | `binary`, `ascii`, `matrix`, `scan` | `scan` | Display/animation mode |
| `--color` | `green`, `cyan`, `red`, `white` | `green` | Color palette |
| `--no-color` | flag | off | Disable all color output |
| `--fps` | integer | `24` | Target animation frame rate (1–60) |
| `--threshold` | 0–255 | `128` | Brightness cutoff for the 0/1 mapping |
| `--invert` | flag | off | Invert the brightness mapping |

### Controls (while running)

| Key | Action |
|---|---|
| `Space` | Pause / resume the animation |
| `R` | Restart the animation from scratch |
| `Q` | Quit |
| `Ctrl+C` | Safe emergency exit |

BinaryVision runs in the terminal's alternate screen buffer, so your normal
shell scrollback is restored untouched the moment you quit.

---

## 3. Display modes

- **`binary`** — Pure `0`/`1` binary art. A short "materializing" intro of
  random noise resolves into the true image, then holds steady with a subtle
  flicker/glitch (a small percentage of characters randomly flip each frame)
  for that live-terminal feel.
- **`ascii`** — Same idea, but characters are drawn from a 10-level density
  ramp (`" .:-=+*#%@"`) based on brightness instead of just `0`/`1`,
  producing classic ASCII-art shading.
- **`scan`** — A horizontal scan bar continuously sweeps the image
  top-to-bottom-to-top. Rows behind the bar are revealed as true image data;
  rows ahead of it are still random noise — a laser-scanner reveal effect.
  *(Default mode.)*
- **`matrix`** — Continuous falling-code "digital rain," Matrix-style. Each
  column's fall speed and trail length are driven by that column's average
  image brightness (brighter regions of the photo rain faster and denser).
  As drops fall, they gradually and permanently reveal the true image
  underneath, so the picture slowly emerges from the rain over time.

---

## 4. How brightness becomes `0`s and `1`s

1. **Resize to fit the terminal.** BinaryVision reads your terminal's
   current width/height (`shutil.get_terminal_size`), reserves a few lines
   for the header/status/footer UI, and computes a character grid that fits
   in the remaining space while preserving the image's aspect ratio. Terminal
   character cells are roughly twice as tall as they are wide, so the height
   calculation is scaled by a `0.5` correction factor to avoid a
   vertically-stretched result.
2. **Convert to grayscale.** The resized image is converted to 8-bit
   grayscale (`Image.convert("L")`), giving one brightness value from 0
   (black) to 255 (white) per character cell — effectively "one pixel per
   character."
3. **Threshold to binary.** Each brightness value is compared against
   `--threshold` (default `128`): values at or above the threshold render as
   `1`, values below render as `0`. `--invert` flips which side is which.
   In `ascii` mode, instead of a hard threshold, brightness is mapped
   proportionally into a 10-character density ramp for smoother shading.
4. **Color the result.** Each character's on-screen color intensity is also
   scaled by that same brightness value (bucketed into 6 shading levels and
   run-length-encoded per row so only a handful of ANSI color codes are
   emitted per line, not one per character) — so brighter parts of your
   photo glow brighter green/cyan/red/white, and dark parts stay dim, giving
   the grid a genuine visual resemblance to the source image.

---

## 5. Performance notes

- The grid size is derived from the terminal's dimensions, so BinaryVision
  never renders more characters than can actually fit on screen.
- All image processing (resize + grayscale) happens once, up front, using
  Pillow's C-accelerated resampling — not per-pixel Python loops.
- Every frame is assembled into a single string and written to `stdout` in
  one `write()` call, using `\x1b[H` (cursor-home) instead of a full screen
  clear, which avoids visible flicker and keeps CPU usage low even at 30+
  FPS.
- Per-row coloring is run-length encoded (consecutive same-brightness
  characters share one color code), which keeps the number of ANSI escape
  sequences per frame small regardless of terminal width.
- Keyboard input is read non-blocking (`select` + `cbreak` mode) once per
  frame, so pausing/quitting is always responsive without a dedicated
  polling thread eating CPU.

---

## 6. Project structure

```text
binaryvision/
├── binaryvision.py     # Main application (single file, no build step)
├── requirements.txt    # Pillow
├── README.md
└── examples/            # (place sample images here to try the tool)
```

---

## 7. Troubleshooting

- **"Pillow is required but not installed"** — run
  `pip install -r requirements.txt` (add `--break-system-packages` on Kali
  if you're not using a virtualenv).
- **"File not found"** / **"Unsupported file extension"** / **"Could not
  decode image"** — BinaryVision validates the path, extension, and actual
  image content before running; the error message tells you exactly which
  check failed.
- **Colors look wrong / no colors** — some minimal terminals (or terminal
  multiplexers in certain modes) don't support 24-bit truecolor. Try
  `--no-color` for a plain monochrome render.
- **Animation looks choppy over SSH** — lower `--fps` (e.g. `--fps 12`) to
  reduce the amount of data sent per second.
- **Terminal looks "stuck" after a crash** — run `reset` in your shell to
  restore normal terminal state (BinaryVision restores the terminal on
  every normal exit path, including `Ctrl+C`, but a hard kill signal can
  still leave things in a bad state).

---

## 8. License

MIT — see [LICENSE](LICENSE).
"# binaryvision" 
