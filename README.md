# OpenFilmKit

Free film scanning and negative conversion from the command line, for macOS and Linux, built on
[SANE](http://www.sane-project.org/).

**Status: early development.** There's nothing useful to install yet. This repository and the
`openfilmkit` package name are placeholders until the first release.

## What it's for

- Capturing linear 48-bit TIFF masters from the film scanners SANE supports, with a record of
  exactly how each scan was made (scanner, driver build, settings, checksum).
- Identifying the scanner by the driver's own model ID, cross-checked against its USB descriptors,
  so settings meant for one hardware revision never get used on another.
- Converting color negatives and slides with response profiles digitized from manufacturers'
  published data sheets, while the masters stay untouched and every conversion keeps a recipe
  that reproduces it.
- Doing all of it from the command line, with `--json` output for scripts, plus a terminal
  interface on top.

The first scanner it targets is the Plustek OpticFilm 8200i SE (GL128 chip), using a SANE build
with a fix for a blue-channel offset bug that has been reported upstream.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

Copyright © 2026 Aidan Siegel &lt;openfilmkit@aidansiegel.com&gt;
