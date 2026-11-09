<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/aidansiegel/OpenFilmKit/main/docs/openfilmkit-dark.svg">
    <img src="https://raw.githubusercontent.com/aidansiegel/OpenFilmKit/main/docs/openfilmkit-light.svg" alt="OpenFilmKit: film spilling out of an open box" width="128">
  </picture>
</p>

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

`pip install "openfilmkit[clean]"` installs conversion and cleanup only, on macOS, Linux or
Windows. `scan` needs the SANE runtime that the Homebrew formula builds (or build it yourself from
a checkout with `tools/build_sane.py`); on Windows, scan with SilverFast or VueScan instead (below).

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
  (142 profiles digitized from manufacturers' data sheets). `--tiff` writes a 16-bit TIFF at full
  resolution instead of a JPEG (see below).
- `clean` removes dust and fibers and trims film-holder edges, always on a new copy. Eyes and
  mouths, and the camera's date stamp (bright or printed faintly, upright or down the side), are
  left untouched by dust removal; faces are found in any orientation, so sideways frames are
  covered. Options:
  - `--no-dust` trims holder edges only; run `clean` again later for dust.
  - `--no-crop` keeps the holder edges; `--no-faces` skips face detection.
  - `--color` also corrects levels and color cast; `--straighten DEGREES`, `--lift-shadows GAMMA`
    and `--red-eye` do what they say.
  - `--scratches` also finds scratches and larger debris with a trained model and fills them with
    a second model (see below).
  - `--spot X,Y[,R]` fills a spot that's left, from its surroundings like a dust fill: X,Y are
    pixels in a copy cleaned with the same options (the crop comes out the same), R a radius
    (default 0.4 % of the short side). Repeat it for more spots; the recipe lists them.
  - On a roll folder, it cleans only conversions without a copy made with the same options.

### Files you get

| File | From | What it is |
|---|---|---|
| Master | `scan` | Linear 48-bit TIFF straight from the scanner, never changed. |
| Processed master | `convert --tiff`, then `clean` | 16-bit TIFF, sRGB with the profile embedded, lossless compression, full resolution. The file to edit, archive or upload (Immich takes it as the original). |
| Viewing copy | `convert`, then `clean` | 8-bit JPEG for sharing and web pages; `clean` can also make one from a processed master (`--output frame.jpg`). |

`clean` works on a 16-bit TIFF at full depth: it makes every decision on an 8-bit copy, exactly
as for a JPEG, and applies color, cropping, straightening, shadows and dust fills at 16 bits, so
smooth skies and shadows keep tens of thousands of levels instead of 256. The processed master is
sRGB for now: until the scanner is profiled (M4 in the roadmap), conversion assumes sRGB
primaries, so a wider color space would hold no extra color.

### Scratches

`clean --scratches` adds a second pass for what dust removal leaves: scratches, fibers and debris.
Microsoft's scratch-detection model from "Bringing Old Photos Back to Life" finds them, and
MI-GAN, an inpainting model, fills them in from what surrounds them. Faces get a lower threshold
and extra speck finders, catchlights and the camera's date stamp are protected, and dark marks are
left as scene. Only the damaged spots are filled, typically about 1 % of a scan; the rest of the
photo is untouched. Because a model draws what goes into those spots, the pass is off by default,
and the recipe says which model filled how many pixels and what share of the picture that is.
`--spare-long-lines` keeps long straight lines (tile grout, wires) the model can mistake for
scratches.

The two models (about 100 MB) are downloaded once, on first use, from this project's
[models-1 release](https://github.com/aidansiegel/OpenFilmKit/releases/tag/models-1) and checked
against their SHA-256; `openfilmkit models --fetch` gets them ahead of time. Both run in OpenCV.
A 73-megapixel scan, 16-bit included, takes about five minutes and at most 2.5 GB of memory.

### Job packages: cleaning on another machine

`clean --pack DIR` doesn't clean: it writes one job package per photo into `DIR`, a folder that
holds the photo, the options and nothing else it depends on. Copy the packages anywhere
OpenFilmKit is installed (a small always-on machine, say) and run them there, then bring the
results back:

```sh
openfilmkit clean roll-07 --scratches --pack jobs/
openfilmkit job run jobs/*                # here, or on the machine the folder was copied to
openfilmkit job collect jobs/<id>         # result and recipe, beside the original
```

A package is named after its job ID, a checksum of everything that decides the result: the
command, the options, the photo, the OpenFilmKit version and the models. Packing the same work
twice gives the same package, running a finished job again does nothing, and a run that's
interrupted leaves the job unfinished, never half done, so it can simply be run again. A job runs
only on the OpenFilmKit version that packed it, since another version can give different pixels.
`job show` says whether a package is packed, started or done; recipes record the machine a job
ran on. Running jobs over SSH and a shared queue for several machines are next (see ROADMAP.md).

## Scans from SilverFast or VueScan

`convert` also reads the raw scans SilverFast and VueScan save, so a scanner OpenFilmKit can't
drive yet, or a Windows PC (where SANE doesn't run), still gets the same conversion and cleanup.
Save the scan before any inversion or color correction:

- **SilverFast:** save the raw HDR (48-bit) or HDRi (64-bit, with infrared) scan, not a converted
  positive. Tested with an HDRi scan from SilverFast 9 and an OpticFilm 8200i.
- **VueScan:** in the Output tab, save a raw file as 48-bit RGB or 64-bit RGBI (raw files need the
  Professional edition). Not yet tested with a real VueScan file.

Uncompressed and compressed (LZW, ZIP) files both work. The infrared channel stays in your file
but isn't used yet. 8-bit scans are refused, and an already-inverted positive won't convert
correctly.

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
- Scratch detection: Microsoft, [Bringing Old Photos Back to Life](https://github.com/microsoft/Bringing-Old-Photos-Back-to-Life)
  (Wan et al., CVPR 2020), MIT license; the official checkpoint converted to ONNX.
- Scratch fill: Picsart AI Research, [MI-GAN](https://github.com/Picsart-AI-Research/MI-GAN)
  (Sargsyan et al., ICCV 2023), MIT license (code and weights); the network from the authors'
  ONNX pipeline. `tools/build_models.py` rebuilds both model files from the authors' originals.
- Face detection: YuNet (Shiqi Yu and others), `face_detection_yunet_2023mar.onnx` from the
  [OpenCV Zoo](https://github.com/opencv/opencv_zoo), MIT license
  (`openfilmkit/data/face_detection_yunet_LICENSE`), run through OpenCV.
- Test photos: Sally K. Ride (NASA S84-37256, 1984) and Grace M. Hopper (U.S. Navy NH 96919-KN,
  1984), both public domain, and real dust from a scan of blank film (CC0); the tests check that
  `clean` leaves an undamaged portrait alone and finds real and added damage. Sources in
  `tests/fixtures/README.md`.
- Scanner and model tables: generated from [SANE](http://www.sane-project.org/) (sane-backends,
  GPL-2.0-or-later with the SANE exception) at the commit in `packaging/sane-pin.json`.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

Copyright © 2026 Aidan Siegel &lt;openfilmkit@aidansiegel.com&gt;
