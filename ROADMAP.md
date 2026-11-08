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
| M5 | Cleanup: dust, fibres, holder edges, red eye | Done: `clean`. |
| M6 | Packaging: Homebrew tap for macOS and Linux | Done: `brew install` tested on macOS (arm64) and Fedora (aarch64). |
| M7 | Terminal UI (Textual) | Not started. |
| M8 | First release | 0.1.0, an early release ahead of M4 and M7, so the kit can be installed and tried. |

## Known limits

- The OpticFilm 8200i SE (GL128) has "basic" status in SANE; infrared isn't implemented.
- Windows isn't supported (no SANE).
- Two identical scanners connected at once can't be told apart (no per-unit ID).
- `clean`'s holder crop is conservative, and a long thin scratch that breaks into short pieces can
  be removed piecemeal, leaving a faint dashed trace.

## Open questions

- Flatbeds with film adapters: later release, or out of scope?
- Camera ("DSLR") scanning: later, or out of scope?
- Export of conversion LUTs as `.cube` files.
