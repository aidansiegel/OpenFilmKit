"""Capture one linear 48-bit TIFF master with a verified record.

Ported from the author's earlier film-scan tool. The master is written to name.tif.partial and renamed
only after every strip decodes and depth, channels, resolution and (with a
profile) frame size check out. name.tif.json records the scanner, its
identification and USB evidence, the runtime and its patches, the exact
command and settings, and the SHA-256, plus what each channel holds (see
health.py: a well-formed file can still have a channel clamped at 0). Failures
keep the partial data and a failure record. Existing files are never replaced.

scanimage runs apart from the terminal, so Ctrl-C reaches only this program, which then tells
scanimage once and waits: on its first signal scanimage cancels the scan and lets the scanner
park; on a second it exits at once, in the middle of that. (It used to get both, the terminal's
and then ours.) A scan that goes silent, with no word and no growth of the file, is given up on
after STALL seconds, and a display nobody reads any more doesn't stop one.
"""
import codecs
import datetime
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .runtime import tiffinfo

PROCESSING = ('Linear RGB16 capture. No inversion, orange-mask removal, stock profile, color '
              'correction, sharpening, dust removal or resizing. Scanner calibration still applies.')
MIN_FREE = 1024 ** 3
MARGIN = 512 * 1024 ** 2
STALL = 900      # seconds with no word from scanimage and no growth of the file before a scan counts as stuck
LISTEN = 5       # seconds between looks at the file while scanimage says nothing
CANCEL = 30      # seconds scanimage gets to stop the scanner once told to


class CaptureError(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def paths(out):
    out = Path(out).expanduser().absolute()
    return {'out': out, 'partial': out.with_name(out.name + '.partial'),
            'record': out.with_name(out.name + '.json'),
            'log': out.with_name(out.name + '.scan.log')}


def check_destination(p, estimate):
    out = p['out']
    if out.suffix.lower() not in ('.tif', '.tiff'):
        raise CaptureError('use a .tif or .tiff output filename')
    if out.parts[:2] == ('/', 'Volumes') and len(out.parts) > 2:
        volume = Path(*out.parts[:3])
        if not volume.is_mount():
            raise CaptureError(f'external volume is not mounted: {volume}')
    for path in p.values():
        if path.exists():
            raise CaptureError(f'refusing to replace existing file: {path}')
    out.parent.mkdir(parents=True, exist_ok=True)
    needed = max(MIN_FREE, (estimate or 0) + MARGIN)
    free = shutil.disk_usage(out.parent).free
    if free < needed:
        raise CaptureError(f'only {free / 1024 ** 3:.2f} GiB free at {out.parent}; '
                           f'need {needed / 1024 ** 3:.2f} GiB')


def command(runtime, device, plan, extra=()):
    args = [str(runtime.scanimage), f'--device-name={device}']
    if plan.get('source') is not None:
        args.append(f"--source={plan['source']}")
    args += [f"--mode={plan['mode']}", f"--depth={plan['depth']}", f"--resolution={plan['dpi']}"]
    for name, value in (plan.get('geometry') or {}).items():
        args += [f'-{name}', str(value)]
    args += [*extra, '--format=tiff', '--progress']
    return args


def inspect_tiff(path):
    # -D decodes every strip, so this checks more than the header.
    try:
        done = subprocess.run([tiffinfo(), '-D', str(path)], capture_output=True, text=True, errors='replace',
                              timeout=600, check=True)
    except subprocess.CalledProcessError as e:
        raise CaptureError(f'TIFF check failed: {(e.stderr or str(e)).strip()}') from e
    except subprocess.TimeoutExpired as e:
        raise CaptureError('TIFF check (tiffinfo) took more than 10 minutes and was stopped') from e
    report = done.stdout
    if re.search(r'read error|unexpected end|truncated', done.stderr or '', re.I):
        raise CaptureError(f'TIFF check failed: {done.stderr.strip()}')   # said, by some builds, with exit status 0
    if not re.search(r'Bits/Sample:\s*16\b', report):
        raise CaptureError('TIFF is not 16 bits per channel')
    if not re.search(r'Samples/Pixel:\s*3\b', report):
        raise CaptureError('TIFF does not have three RGB channels')
    dims = re.search(r'Image Width:\s*(\d+)\s+Image Length:\s*(\d+)', report)
    res = re.search(r'Resolution:\s*([\d.]+),\s*([\d.]+)\s+pixels/inch', report)
    if not dims or min(map(int, dims.groups())) <= 0 or not res:
        raise CaptureError('TIFF dimensions or resolution are missing')
    # scanimage doesn't check its own writes (a disk that fills mid-scan still ends with status 0),
    # and writes no compression: a file shorter than its pixels was cut short.
    if Path(path).stat().st_size < int(dims[1]) * int(dims[2]) * 6:
        raise CaptureError('TIFF is shorter than its image: the scan was cut short (is the disk full?)')
    return {'width': int(dims[1]), 'height': int(dims[2]), 'dpi_x': float(res[1]),
            'dpi_y': float(res[2]), 'bits_per_channel': 16, 'channels': 3,
            'decoded_all_strips': True}


def channel_health(path):
    """health.check() of the captured pixels. Where the check can't be made at all (a scan-only
    install, without numpy) that is noted in its place; pixels that can't be read back are a
    failed capture."""
    try:
        from . import health
        from .films.convert import raw_open
    except ImportError as e:
        return {'error': str(e), 'channels': {}, 'warnings': []}
    try:
        return health.check(raw_open(path))
    except (ValueError, OSError) as e:
        raise CaptureError(f"the scan can't be read back: {e}") from e


def check_tiff(info, plan, prof):
    dpi = plan['dpi']
    if info['dpi_x'] != dpi or info['dpi_y'] != dpi:
        raise CaptureError(f'TIFF resolution differs from the requested {dpi} dpi: {info}')
    frame = (prof or {}).get('frame_mm')
    if frame and (info['width'] < int(frame['min_width'] / 25.4 * dpi) or
                  info['height'] < int(frame['min_height'] / 25.4 * dpi)):
        raise CaptureError(f'capture is smaller than a full frame: {info}')


def _write_record(path, record):
    """The record, whole or not at all: written beside itself, then renamed over the last one."""
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(record, indent=2) + '\n')
    os.replace(tmp, path)


def _listen(stream, heard):
    """Pass on what scanimage says, as it says it, to the queue `heard`; None when it has finished.
    In a thread of its own, so that neither a silent scanner nor a display that has stopped
    taking text holds the other up."""
    decode = codecs.getincrementaldecoder('utf-8')(errors='replace')   # whatever bytes a driver prints
    while True:
        said = stream.read1(1024)
        if not said:
            break
        heard.put(decode.decode(said))
    heard.put(None)


def _stop(process):
    """Stop scanimage the way it stops itself: one interrupt, on which it cancels the scan and
    lets the scanner park, and CANCEL seconds to do it in; only then harder. Ctrl-C again
    while waiting goes straight to that."""
    if process.poll() is not None:
        return
    try:
        if os.name == 'posix':
            process.send_signal(signal.SIGINT)
            process.wait(timeout=CANCEL)
            return
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        pass
    process.terminate()
    try:
        process.wait(timeout=15)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.kill()
        process.wait()


def capture(runtime, out, device, plan, prof, scanner, film, options_text, estimate=None,
            extra=(), progress=sys.stderr):
    """Scan to `out`. Returns the final record; raises CaptureError on failure."""
    tiffinfo()   # the capture is verified with it: if it's missing, say so now, not after the scan
    p = paths(out)
    check_destination(p, estimate)
    cmd = command(runtime, device, plan, extra)
    record = {
        'openfilmkit': {'version': __version__, 'record_format': 1},
        'status': 'starting', 'started_utc': now(),
        'scanner': scanner, 'runtime': runtime.summary(),
        'scanimage_version': runtime.version(), 'command': cmd, 'plan': plan,
        'settings': options_text, 'film': film, 'processing': PROCESSING,
        'output': str(p['out']),
    }
    with p['record'].open('x') as f:
        json.dump(record, f, indent=2)

    process, heard, verified = None, queue.Queue(), None
    try:
        with p['partial'].open('xb') as image, p['log'].open('x', buffering=1) as log:
            process = subprocess.Popen(cmd, env=runtime.env(), stdout=image, stderr=subprocess.PIPE,
                                       start_new_session=os.name == 'posix')
            threading.Thread(target=_listen, args=(process.stderr, heard), daemon=True).start()
            shown, size, since = True, 0, time.monotonic()
            while True:
                try:
                    said = heard.get(timeout=LISTEN)
                except queue.Empty:   # calibrating or warming up, it says nothing for a while; the file tells too
                    grown = p['partial'].stat().st_size
                    if grown != size:
                        size, since = grown, time.monotonic()
                    elif time.monotonic() - since > STALL:
                        raise CaptureError(f'scanimage has said nothing and written nothing for {STALL // 60} minutes: '
                                           f"the scanner or its connection is stuck; see {p['log']}")
                    continue
                if said is None:
                    break
                since = time.monotonic()
                log.write(said)
                if shown:
                    try:
                        progress.write(said)
                        progress.flush()
                    except (OSError, ValueError):   # nobody is reading any more: the scan goes on, and the log has it all
                        shown = False
            code = process.wait()
            image.flush()
            os.fsync(image.fileno())
        if code:
            raise CaptureError(f"scanimage exited with status {code}; see {p['log']}")
        info = inspect_tiff(p['partial'])
        check_tiff(info, plan, prof)
        health = channel_health(p['partial'])
        with p['partial'].open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        if p['out'].exists():
            raise CaptureError(f"{p['out']} appeared during capture; not replacing it")
        # All that the record will say is known before the file takes its name, so that whatever
        # stops this between the two, the record written then is still the true one.
        verified = dict(record, channel_health=health, status='captured_verified', completed_utc=now(), sha256=digest,
                        bytes=p['partial'].stat().st_size, tiff=info)
        p['partial'].rename(p['out'])
        _write_record(p['record'], verified)
        return verified
    except BaseException as exc:
        if process:
            _stop(process)
            with p['log'].open('a') as log:   # what it said while stopping
                while not heard.empty():
                    log.write(heard.get() or '')
        if verified is not None and p['out'].exists() and not p['partial'].exists():
            record = verified   # the master is whole, verified and under its name: only its record was behind
        else:
            record.update(status='failed_or_interrupted', ended_utc=now(), error=str(exc) or
                          type(exc).__name__, partial=str(p['partial']))
        try:
            _write_record(p['record'], record)
        except OSError:   # the drive has gone too, say: the first trouble is the one to report
            pass
        raise
