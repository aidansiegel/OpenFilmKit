# OpenFilmKit

Free film scanning and negative conversion from the command line, for macOS and Linux, built on
[SANE](http://www.sane-project.org/).

**Status: early development.** Capture, conversion and cleanup work; the first scanner it targets
is the Plustek OpticFilm 8200i SE. Expect changes.

## Install

```sh
brew install aidansiegel/openfilmkit/openfilmkit
openfilmkit doctor
```

The formula builds OpenFilmKit's own SANE runtime (a pinned upstream commit plus the fixes in
`patches/`), kept separate from Homebrew's `sane-backends`. On Linux, `openfilmkit doctor` prints
the one command that installs the USB permission rule.

`pip install "openfilmkit[clean]"` installs conversion and cleanup only: `convert` and `clean`
work on scans from any software, but `scan` needs the SANE runtime that the Homebrew formula
builds (or build it yourself from a checkout with `tools/build_sane.py`).

## Use

```sh
openfilmkit doctor                      # driver, scanner, identification
openfilmkit roll new roll-07 --film color-negative --stock kodak-gold200
openfilmkit scan roll-07 --frames 6     # linear 48-bit TIFF masters, one frame at a time
openfilmkit convert roll-07/masters/frame-01.tif
openfilmkit clean roll-07               # dust and holder edges, on new copies
```

Every command takes `--json`. Masters are never changed; every conversion and cleaned copy has a
recipe beside it that says exactly how it was made.

- `scan --full-area` scans the driver's whole area instead of its default selection.
- `convert` picks the film's response curve from `openfilmkit stocks` / `openfilmkit profiles`
  (142 profiles digitized from manufacturers' data sheets).
- `clean --no-dust` trims holder edges only; run `clean` again later for dust.

## Scanners

`openfilmkit scanners` lists the film scanners SANE supports and the ones OpenFilmKit has profiles
for. A scanner is identified by its driver's internal model ID, confirmed against its USB
descriptors, so settings meant for one hardware revision are never used on another.

## Data sources

- Film response curves: digitized from manufacturers' published characteristic-curve charts
  (Kodak, Fujifilm, Agfa, Konica; most via the [125px film document archive](https://125px.com/docs/film/)),
  and from Columbia University's [Database of Response Functions](https://www.cs.columbia.edu/CAVE/software/softlib/dorf.php)
  (M. D. Grossberg and S. K. Nayar, "Modeling the Space of Camera Response Functions", IEEE PAMI 2004).
  Each profile records its source, page and SHA-256; see `docs/FILM-STOCK-RESEARCH.md` and
  `tools/research/datasheets.json`. The data sheets themselves are not included.
- Scanner and model tables: generated from [SANE](http://www.sane-project.org/) (sane-backends,
  GPL-2.0-or-later with the SANE exception) at the commit in `packaging/sane-pin.json`.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

Copyright © 2026 Aidan Siegel &lt;openfilmkit@aidansiegel.com&gt;
