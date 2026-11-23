# Roadmap

OpenFilmKit is built in milestones. Status as of November 2026.

## Principles

- Command line first: every step is a subcommand with `--json`, and the terminal UI and the desktop app
  sit on the same core. The desktop app is for the people who want this most and won't open a
  terminal (library patrons, family members); it calls the same functions, so anything saved from it
  has the same recipe and can be redone from the command line.
- `scanimage` talks to SANE, one process per operation, so a driver crash can't take the kit down
  and every scan records a command anyone can rerun.
- Masters are never touched. Every later step writes a new file plus a recipe that reproduces it.
- The driver decides what's possible (resolutions, modes, areas), read live from `scanimage -A`.
  Scanner profiles add only what SANE doesn't know: tested settings, frame size, required patches.
- A scanner is identified by its driver's internal model ID, derived from the name the driver
  reports and confirmed against the USB descriptors.

## Milestones

| | Milestone | Status |
|---|---|---|
| M0 | Name, license, repository | Done: OpenFilmKit, GPL-3.0-or-later. |
| M1 | Build SANE from a pinned commit plus `patches/` | Done: `tools/build_sane.py`, `packaging/sane-pin.json`. |
| M2 | Capture: `doctor`, `scanners`, `options`, `roll new`, `scan` | Done: tested on an OpticFilm 8200i SE on November 18, 2026 (a roll of negatives at 3600 dpi, plus a same-frame A/B that confirmed the blue-offset patch). |
| M3 | Film profiles and conversion | Done: 142 data-sheet profiles; `stocks`, `profiles`, `roll set`, `convert`. |
| M4 | Scanner color profiling (IT8 target, ArgyllCMS) | Not started; needs an IT8 transparency target to finish. The library's 8200i SE didn't come with one. |
| M5 | Cleanup: dust, fibers, holder edges, red eye, scratches | Done: `clean`, and `clean --scratches` (trained models, opt-in). |
| M6 | Packaging: Homebrew tap for macOS and Linux | Done: `brew install` tested on macOS (arm64) and Fedora (aarch64). |
| M7 | Terminal UI (Textual) | In progress: `openfilmkit ui` browses rolls with previews (before/after, fixes marked), recipes, cleaning (here, on a host, into a queue) and the queue. Next: a scan screen, after the first real scanner runs. |
| M8 | First release | 0.1.0, an early release ahead of M4 and M7, so the kit can be installed and tried. |
| M9 | Remote processing: run conversion and cleanup on another machine, or queue jobs for several | Done for `clean` in 0.6.0: `clean --on HOST` and a queue several workers take jobs from (`clean --queue`, `openfilmkit worker`), on job packages with stage checkpoints, so stopped work resumes to the same result on any machine. Next: `convert` jobs. |
| M11 | Desktop app (Electron, `gui/`) | In progress: open a roll, frames with thumbnails, a live preview with brightness, contrast, warmth, color and turn, hold to see the automatic version, save a picture or a 16-bit TIFF, and a scan screen. Next: automatic turn, crop and color balance (the November 21 bake-off found these are what make the automatic result disappointing), dust cleaning by default, then packaging that carries its own Python and SANE so it runs from a removable disk. The scan screen hasn't been run against a scanner yet. |
| M10 | Flatbeds with film adapters | Planned; a flatbed can be attached for testing. A holder holds several frames, so `scan --holder NAME` will preview the whole holder, find the frames (holder windows, then the gaps between frames, fitted to the format's spacing), let you review the boxes, and scan each frame as its own master; nothing after `scan` changes. `roll add` will also bring in frames the scanner's own software has already separated, with a record of where they came from. |

## Known limits

- The OpticFilm 8200i SE (GL128) has "basic" status in SANE; infrared isn't implemented. Its
  frames come out about 0.18 mm short of the 25 mm the driver offers (7,034 rows at 7200 dpi), at
  every resolution. Its green channel skips 10-60% of its codes, changing from scan to scan
  though every scan at one resolution reused the same calibration; it rose over the first 40
  minutes of a session, then held, cause not known. About 1.4 of 16 bits at worst; red and blue
  use every code.
- genesys never expires a cached calibration for film (only flatbed entries expire), and keeps
  one per resolution in `~/.sane/<model>.cal`: a scan can reuse a calibration made weeks earlier
  by another driver build.
- Scanning on Windows isn't supported (no SANE); `convert` and `clean` run there on raw scans from
  SilverFast or VueScan.
- Two identical scanners connected at once can't be told apart (no per-unit ID).
- `clean`'s holder crop is conservative. Without `--scratches`, a long thin scratch that breaks into
  short pieces can be removed piecemeal, leaving a faint dashed trace.
- Soft, out-of-focus dust on busy patterns (a ribbon bar, foliage) is mostly left in place; it's
  marked as an expected failure in the tests. `clean --spot` fills what's left by hand.

## Open questions

- Camera ("DSLR") scanning: later, or out of scope?
- Export of conversion LUTs as `.cube` files.
