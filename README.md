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
openfilmkit check roll-07/masters/*.tif # what each channel holds: clipping, digital stretching
openfilmkit convert roll-07/masters/frame-01.tif
openfilmkit clean roll-07               # dust and holder edges, on new copies
```

Every command takes `--json`. Masters are never changed; every conversion and cleaned copy has a
recipe beside it that says exactly how it was made.

- `scan --full-area` scans the driver's whole area instead of its default selection.
- `scan` and `convert` also look at what each channel of the scan holds, and say so if one is
  clamped at 0 or at full scale (detail lost there) or never uses many of the codes in its range
  (stretched digitally, so fewer real levels than 16 bits). A file can pass every structural check
  and still have that: the GL128 driver's blue offset bug left blue at exactly 0 over much of a
  frame. `openfilmkit check` does the same for any scan, yours or SilverFast's or VueScan's, and
  exits with status 1 if anything is wrong. The findings go in the capture record and the
  conversion's recipe.
- `convert` picks the film's response curve from `openfilmkit stocks` / `openfilmkit profiles`
  (142 profiles digitized from manufacturers' data sheets). `--tiff` writes a 16-bit TIFF at full
  resolution instead of a JPEG (see below). A smaller copy, like the default 1800 px JPEG, averages
  each block of scan pixels, so it has less grain than the scan thinned and keeps small specks for
  `clean` to find.
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
  - If `clean` is stopped partway (Ctrl-C, a machine that sleeps), running the same command again
    picks up the slow work where it stopped, with the same result as an uninterrupted run. The
    saved stages live in `~/.cache/openfilmkit/checkpoints` (or `$OPENFILMKIT_CACHE`) until the
    photo is done, and are cleared after two weeks if it never is.

### The terminal UI

`openfilmkit ui` (or `openfilmkit ui ~/Scans`) shows your rolls, or any folder of photos: each
frame's master, conversions and cleaned copies, what still needs doing, the recipe of the selected
copy in plain words, a preview, and the queue and its workers if one is set up.

- `b` flips the preview between after (the cleaned copy) and before (the original, cropped the
  same way), in place, so small fixes stand out; `B` shows them side by side; `ctrl+shift+b` side
  by side with every fixed pixel marked in red (`ctrl+b` too, for terminals that can't tell them
  apart).
- `z` zooms in (up to ×32) and `Z` back out; a click on the preview zooms in where you clicked,
  `h` `j` `k` `l` (or shift and the arrows) move around, and `escape` shows the whole picture again.
  Every view zooms, the red fixes too, worked out at full size for the part shown.
- `s` marks spots the cleaning missed: click each one on the after picture (zoomed in, for
  precision) and a yellow ring shows where; click a ring to unmark it, `[` and `]` resize the last
  one. `c` then makes a new copy with the same options as the one you marked, filling those spots
  too (`clean --spot`).
- `space` opens the full size in Quick Look (macOS) or the default viewer.
- `m` marks frames (`a` marks every frame that needs cleaning). `v` converts them from their scan
  masters (a JPEG viewing copy, or a 16-bit TIFF at full resolution), and `c` cleans them, with the
  options as checkboxes: here, `--on` a host, or into a queue. Commands run one at a time, with
  their output in a log at the bottom. `C` collects what the queue has finished.

Previews are drawn in the window as well as the terminal allows: in real pixels in Ghostty, Kitty
and WezTerm (and Sixel terminals such as iTerm2 and foot), as colored blocks in Terminal.app or a
plain SSH session; the first blocky preview says so, and points to `space` for the full size where
there's a screen to open it on. It works over SSH too. With pip, the UI is the `ui` extra:
`pip install "openfilmkit[ui]"`. Scanning from the UI comes after the first real scanner runs.

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

### Cleaning on another machine

`clean --on HOST` does the work on another machine over SSH and brings the results back, beside
the originals as usual:

```sh
openfilmkit clean roll-07 --scratches --on pi
```

HOST is any name `ssh` knows, such as one from `~/.ssh/config`. The host needs the same OpenFilmKit
version, `rsync`, and an SSH key so it doesn't ask for a password. If `openfilmkit` isn't on the
path there for SSH commands (common with Homebrew on Linux), say where it is in
`~/.config/openfilmkit/hosts.toml`:

```toml
[hosts.pi]
openfilmkit = "/home/linuxbrew/.linuxbrew/bin/openfilmkit"
jobs = "openfilmkit-jobs"      # where jobs go on the host, from its home folder (the default)
```

If the connection drops or the host goes to sleep, run the same command again: finished photos
are only copied back, and a photo that was under way picks up where it stopped. `--keep-remote`
keeps the job packages afterwards, which are otherwise deleted on both machines once each result
is back and checked.

### Queues and workers

For more than one machine, or a machine that should work through the night on its own, put the
work in a queue: a folder on any machine reachable over SSH (or a mounted path). Workers take
jobs from it one at a time, and you collect the results when it suits you:

```sh
openfilmkit clean roll-07 --scratches --queue home   # pack the photos into the queue
openfilmkit queue status                             # what's waiting, running, done; which workers
openfilmkit queue collect                            # results beside the originals; queue cleared
```

On each machine that should do the work, run `openfilmkit worker`, as a service
(`packaging/openfilmkit-worker.service`). In `~/.config/openfilmkit/hosts.toml`:

```toml
[queues.home]                        # on the machine you submit and collect from
queue = "server:openfilmkit-queue"   # host:path, or a local path

[worker]                             # on a worker
queue = "server:openfilmkit-queue"
memory_gb = 2.5                      # largest job it takes (and only if the machine has it free)
hours = "22-7"                       # start jobs only overnight (optional)
yield_to = "photo-worker.service"    # make way while this service is using memory (optional)
busy_when = ["curl -fs http://localhost/status.json | grep -Eq '\"state\": *\"(recording|finishing)\"'"]
                                     # make way while any of these commands succeeds (optional)
```

A worker takes the oldest job it can do: packed by the same OpenFilmKit code, an estimated memory
need it can meet, inside its hours, and not while a service named in `yield_to` is busy or a
command in `busy_when` succeeds, such as a check that a live capture is running (if either
happens mid-job, the worker stops and comes back to it). Nice and a CPU quota aren't enough
beside live capture, which can't spare even a moment: give it a `busy_when`, and have whatever
starts the capture freeze the worker (`systemctl freeze openfilmkit-worker`) and thaw it after.
A worker frozen for hours carries on after the thaw: it retries the queue for a whole lease
before giving up the job, and backs off only if another worker took the job meanwhile. It claims the job and renews the claim
every two minutes; a claim not renewed for ten minutes lapses, and the next worker starts from
the slow stages the last one pushed (the scratch model's results and every filled window), so a
machine that dies or is switched off loses at most a few minutes of work. A worker that can't
renew its claim stops rather than finish work another may have taken.

Claims keep workers out of each other's way but can't rule out two doing the same job (nothing
rsync does is atomic, and clocks differ). That's harmless by design: a job gives the same file
wherever it runs, and collecting keeps the first result that checks out.

Workers only ever use rsync: each reads the queue and writes only its own `drop/<name>/` folder,
so it can have an SSH key restricted to that (with `rrsync`), and it clears its own folder once a
job has been collected. Collecting checks every result against its recipe and job before taking
it; one that doesn't check out is deleted (or marked rejected, where collecting may not delete
it), and the job runs again. Results go back beside the originals only for jobs packed on the
machine collecting them; for others, `queue collect --to DIR`.

### Job packages

`--on` and `--queue` work with job packages, which can also be made and moved by hand. `clean --pack DIR`
doesn't clean: it writes one job package per photo into `DIR`, a folder that holds the photo, the
options and nothing else it depends on. Copy the packages anywhere OpenFilmKit is installed and
run them there, then bring the results back:

```sh
openfilmkit clean roll-07 --scratches --pack jobs/
openfilmkit job run jobs/*                # here, or on the machine the folder was copied to
openfilmkit job collect jobs/<id>         # result and recipe, beside the original
```

A package is named after its job ID, a checksum of everything that decides the result: the
command, the options, the photo, the OpenFilmKit version and build (a fingerprint of its code,
which tells development versions apart) and the models. Packing the same work twice gives the
same package, and the same photo from two folders is one job whose result goes back to both, running a finished job again does nothing, and a run that's
interrupted leaves the job unfinished, never half done, so it can simply be run again. A job runs
only on the OpenFilmKit code that packed it, since other code can give different pixels
(`openfilmkit job build` shows a machine's build).
`job show` says whether a package is packed, started or done; recipes record the machine a job
ran on.

A stopped job keeps its slow work. The faces, crop and date stamp, the dust mask, the scratch
model's probabilities, the scratch mask and every window MI-GAN has filled are saved in the
package as they're finished (`work/checkpoints/`), so the next `job run`, on this machine or on
whichever machine the package goes to, goes on from there. The result is the same, byte for byte,
as a run that never stopped. The recipe lists the stages that were picked up and the machine that
did them, and the checkpoints are deleted once the job is done. `job remove` deletes packages.

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
