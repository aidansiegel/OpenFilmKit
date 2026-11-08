"""Capture one linear 48-bit TIFF master with a verified record.

Ported from the author's earlier film-scan tool. The master is written to name.tif.partial and renamed
only after every strip decodes and depth, channels, resolution and (with a
profile) frame size check out. name.tif.json records the scanner, its
identification and USB evidence, the runtime and its patches, the exact
command and settings, and the SHA-256. Failures keep the partial data and a
failure record. Existing files are never replaced.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .runtime import tiffinfo

PROCESSING = ('Linear RGB16 capture. No inversion, orange-mask removal, stock profile, color '
              'correction, sharpening, dust removal or resizing. Scanner calibration still applies.')
MIN_FREE = 1024 ** 3
MARGIN = 512 * 1024 ** 2


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
    report = subprocess.run([tiffinfo(), '-D', str(path)], capture_output=True, text=True,
                            timeout=600, check=True).stdout
    if not re.search(r'Bits/Sample:\s*16\b', report):
        raise CaptureError('TIFF is not 16 bits per channel')
    if not re.search(r'Samples/Pixel:\s*3\b', report):
        raise CaptureError('TIFF does not have three RGB channels')
    dims = re.search(r'Image Width:\s*(\d+)\s+Image Length:\s*(\d+)', report)
    res = re.search(r'Resolution:\s*([\d.]+),\s*([\d.]+)\s+pixels/inch', report)
    if not dims or min(map(int, dims.groups())) <= 0 or not res:
        raise CaptureError('TIFF dimensions or resolution are missing')
    return {'width': int(dims[1]), 'height': int(dims[2]), 'dpi_x': float(res[1]),
            'dpi_y': float(res[2]), 'bits_per_channel': 16, 'channels': 3,
            'decoded_all_strips': True}


def check_tiff(info, plan, prof):
    dpi = plan['dpi']
    if info['dpi_x'] != dpi or info['dpi_y'] != dpi:
        raise CaptureError(f'TIFF resolution differs from the requested {dpi} dpi: {info}')
    frame = (prof or {}).get('frame_mm')
    if frame and (info['width'] < int(frame['min_width'] / 25.4 * dpi) or
                  info['height'] < int(frame['min_height'] / 25.4 * dpi)):
        raise CaptureError(f'capture is smaller than a full frame: {info}')


def capture(runtime, out, device, plan, prof, scanner, film, options_text, estimate=None,
            extra=(), progress=sys.stderr):
    """Scan to `out`. Returns the final record; raises CaptureError on failure."""
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

    process = None
    try:
        with p['partial'].open('xb') as image, p['log'].open('x') as log:
            process = subprocess.Popen(cmd, env=runtime.env(), stdout=image,
                                       stderr=subprocess.PIPE, text=True)
            for char in iter(lambda: process.stderr.read(1), ''):
                progress.write(char)
                progress.flush()
                log.write(char)
            code = process.wait()
            image.flush()
            os.fsync(image.fileno())
        if code:
            raise CaptureError(f"scanimage exited with status {code}; see {p['log']}")
        info = inspect_tiff(p['partial'])
        check_tiff(info, plan, prof)
        with p['partial'].open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        if p['out'].exists():
            raise CaptureError(f"{p['out']} appeared during capture; not replacing it")
        p['partial'].rename(p['out'])
        record.update(status='captured_verified', completed_utc=now(), sha256=digest,
                      bytes=p['out'].stat().st_size, tiff=info)
        p['record'].write_text(json.dumps(record, indent=2) + '\n')
        return record
    except BaseException as exc:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        record.update(status='failed_or_interrupted', ended_utc=now(), error=str(exc) or
                      type(exc).__name__, partial=str(p['partial']))
        p['record'].write_text(json.dumps(record, indent=2) + '\n')
        if isinstance(exc, subprocess.CalledProcessError):
            raise CaptureError(f'TIFF check failed: {exc.stderr or exc}') from exc
        raise
