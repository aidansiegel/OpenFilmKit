# Roadmap

OpenFilmKit is built in milestones. Status as of November 2026.

## Principles

- Command line first: every step is a subcommand with `--json`; a terminal UI comes later, on the
  same core. No GUI.
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
| M2 | Capture: `doctor`, `scanners`, `options`, `roll new`, `scan` | Done on SANE's test backend; first hardware run pending. |
| M3 | Film profiles and conversion | Done: 142 data-sheet profiles; `stocks`, `profiles`, `roll set`, `convert`. |
| M4 | Scanner color profiling (IT8 target, ArgyllCMS) | Not started; needs an IT8 transparency target to finish. |
| M5 | Cleanup: dust, fibers, holder edges, red eye, scratches | Done: `clean`, and `clean --scratches` (trained models, opt-in). |
| M6 | Packaging: Homebrew tap for macOS and Linux | Done: `brew install` tested on macOS (arm64) and Fedora (aarch64). |
| M7 | Terminal UI (Textual) | Not started. |
| M8 | First release | 0.1.0, an early release ahead of M4 and M7, so the kit can be installed and tried. |
| M9 | Remote processing: run conversion and cleanup on another machine, or queue jobs for several | Done for `clean` in 0.6.0: `clean --on HOST` and a queue several workers take jobs from (`clean --queue`, `openfilmkit worker`), on job packages with stage checkpoints, so stopped work resumes to the same result on any machine. Next: `convert` jobs. |

## Known limits

- The OpticFilm 8200i SE (GL128) has "basic" status in SANE; infrared isn't implemented.
- Scanning on Windows isn't supported (no SANE); `convert` and `clean` run there on raw scans from
  SilverFast or VueScan.
- Two identical scanners connected at once can't be told apart (no per-unit ID).
- `clean`'s holder crop is conservative. Without `--scratches`, a long thin scratch that breaks into
  short pieces can be removed piecemeal, leaving a faint dashed trace.
- Soft, out-of-focus dust on busy patterns (a ribbon bar, foliage) is mostly left in place; it's
  marked as an expected failure in the tests. `clean --spot` fills what's left by hand.

## Open questions

- Flatbeds with film adapters: later release, or out of scope?
- Camera ("DSLR") scanning: later, or out of scope?
- Export of conversion LUTs as `.cube` files.
