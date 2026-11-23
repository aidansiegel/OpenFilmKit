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

With the genesys backend scanimage runs at SANE_DEBUG_GENESYS=4 (unless that is set already), so
the log has the front-end codes and white levels the kit's GL128 patch chose; frontend() reads them
into the record. The terminal gets only what scanimage itself says, not the driver's debug lines.
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

from . import __version__, files
from .runtime import tiffinfo

PROCESSING = ('Linear RGB16 capture. No inversion, orange-mask removal, stock profile, color '
              'correction, sharpening, dust removal or resizing. Scanner calibration still applies.')
MIN_FREE = 1024 ** 3
MARGIN = 512 * 1024 ** 2
STALL = 900      # seconds with no word from scanimage and no growth of the file before a scan counts as stuck
LISTEN = 5       # seconds between looks at the file while scanimage says nothing
STOPPED = 'stopped before it finished (Stop, Ctrl-C, or the program closed)'
OPEN_RETRIES = 1  # times a scan whose scanner wouldn't open is tried again (it nearly always opens the second time)
OPEN_WAIT = 3     # seconds before that
OPEN_FAILED = re.compile(r'^scanimage: open of device .* failed: ', re.M)
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
    drive = files.unmounted(out)   # /Volumes/NAME, /mnt/NAME, /media/...: else onto the disk under it
    if drive:
        raise CaptureError(f'external volume is not mounted: {drive}')
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


# A SANE debug line: "[19:59:59.560828] [genesys] ...", or without the time in older builds.
DEBUG_LINE = re.compile(r'(?:\[\d\d:\d\d:\d\d\.\d+\] )?\[[\w.-]+\] ')
TRIPLE = r'\((-?\d+),(-?\d+),(-?\d+)\)'
CHANNELS = ('red', 'green', 'blue')
SEARCH_WARNINGS = ('barely moved', 'too dark', 'still moving')
CLIPPED = 0.01   # share of the white strip's columns at the ADC ceiling above which a channel counts as clipped


def scan_env(runtime, device):
    """The environment scanimage runs in: the runtime's, and for genesys the driver's debug level 4,
    at which the GL128 patch logs the front-end codes and white levels. A level already set stays."""
    env = runtime.env()
    if str(device).partition(':')[0] == 'genesys':
        env.setdefault('SANE_DEBUG_GENESYS', '4')
    return env


class _Display:
    """Of what scanimage says, the part for the terminal: all but the driver's debug lines (and the
    indented blocks some of them open with "{" and close with "}"), which only the log keeps.
    Given text in pieces of any size; a line is passed on once it ends, at a newline or carriage return."""
    def __init__(self):
        self.rest, self.block = '', False

    def feed(self, text, end=False):
        pieces = re.split(r'(?<=[\r\n])', self.rest + text)
        self.rest = '' if end else pieces.pop()
        shown = []
        for piece in pieces:
            if self.block:
                self.block = not piece.startswith('}')
            elif DEBUG_LINE.match(piece):
                self.block = piece.rstrip().endswith('{')
            else:
                shown.append(piece)
        return ''.join(shown)


def _triple(m, at=1):
    return [int(m[at]), int(m[at + 1]), int(m[at + 2])]


def frontend(log):
    """What the GL128 patch logged of the front end (the analog gains and offsets) and the white and
    dark references, from the text of a scan log; {} if it logged nothing. Only what was logged is
    there; where a line comes more than once, the last counts."""
    fe, white, warmups = {}, {}, 0
    for line in log.splitlines():
        at = line.find('GL128 ')
        if at < 0:
            continue
        line = line[at:].strip()
        if line.startswith(('GL128 AFE search: off', 'GL128 AFE search: failed')):
            fe['search'] = False   # fixed codes, asked for or fallen back to; a search line before doesn't undo it
        elif line.startswith('GL128 AFE search:'):
            fe.setdefault('search', True)
        if any(w in line for w in SEARCH_WARNINGS) or line.startswith('GL128 AFE search: failed'):
            fe.setdefault('warnings', []).append(line)
        if m := re.match(r'GL128 AFE warm-up read (\d+):', line):
            warmups = max(warmups, int(m[1]))
        elif m := re.search(r'wide strip at gain 0 ' + TRIPLE + r' after (\d+) read', line):
            fe['wide_strip_gain0'], fe['warmup_reads'] = _triple(m), int(m[4])
        elif m := re.match(rf'GL128 AFE codes: gains white {TRIPLE} image {TRIPLE}, offsets white {TRIPLE} '
                           rf'dark {TRIPLE}', line):
            fe['gains'] = {'white': _triple(m), 'image': _triple(m, 4)}
            fe['offsets'] = {'white': _triple(m, 7), 'dark': _triple(m, 10)}
        elif m := re.match(r'GL128 shading white ch(\d) min=(\d+) mean=(\d+) max=(\d+) '
                           r'at_adc_ceiling=(\d+)/(\d+)', line):
            white[int(m[1])] = {'min': int(m[2]), 'mean': int(m[3]), 'max': int(m[4]),
                                'at_adc_ceiling': int(m[5]), 'columns': int(m[6])}
        elif m := re.match(rf'GL128 shading dark even={TRIPLE} odd={TRIPLE}', line):
            fe['dark'] = {'even': _triple(m), 'odd': _triple(m, 4)}
        elif line.startswith('GL128 image gain:'):
            fe['image_gain'] = {name: [int(v) for v in values.split(',')]
                                for name, values in re.findall(r'(\w+) \((-?\d+(?:,-?\d+)*)\)', line)}
    if warmups and 'warmup_reads' not in fe:
        fe['warmup_reads'] = warmups
    if white:
        fe['white'] = [white.get(c) for c in range(max(white) + 1)]
    return fe


def _names(channels):
    return channels[0] if len(channels) == 1 else f"{', '.join(channels[:-1])} and {channels[-1]}"


def frontend_problems(fe):
    """What looks wrong in a record's front end, in a short line each: a white reference at the ADC
    ceiling (the gains are too high, and the shading can't correct what it can't see), or a
    warning from the front-end search."""
    if not fe:
        return []
    said = []
    clipped = [CHANNELS[c] if c < 3 else f'channel {c}' for c, w in enumerate(fe.get('white') or [])
               if w and w['columns'] and w['at_adc_ceiling'] > CLIPPED * w['columns']]
    if clipped:
        said.append(f'White reference clipped in {_names(clipped)}: the front end is set too hot.')
    for w in fe.get('warnings', []):
        m = re.search(r'\bch(\d)\b', w)
        ch = CHANNELS[int(m[1])] if m and int(m[1]) < 3 else 'a channel'
        if 'barely moved' in w:
            line = f"The front-end search couldn't set {ch}'s black offset and used a fixed one."
        elif 'too dark' in w and m:
            line = f'The front-end search found the lamp-on strip too dark in {ch}: is the lamp lit?'
        elif 'too dark' in w:
            line = 'The front-end search found the white strip too dark (is the lamp lit?) and kept the fixed codes.'
        elif 'failed' in w:
            line = 'The front-end search failed and kept the fixed codes.'
        else:
            line = 'The lamp was still warming up when the front-end gains were set.'
        if line not in said:
            said.append(line)
    return said


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


def _logged_frontend(path):
    """{'frontend': ...} from the scan log at `path`, or {} when it has none or can't be read."""
    try:
        fe = frontend(path.read_text(encoding='utf-8', errors='replace'))
    except OSError:
        return {}
    return {'frontend': fe} if fe else {}


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


def open_failed(text):
    """Whether scanimage said it couldn't open the scanner: then it stopped before anything moved."""
    return bool(OPEN_FAILED.search(text))


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
        # The log in UTF-8 whatever the system's own encoding: what scanimage says is decoded with
        # U+FFFD for bytes that aren't text, and that has no place in cp1252, say.
        with p['partial'].open('xb') as image, p['log'].open('x', buffering=1, encoding='utf-8') as log:
            shown = True
            for attempt in range(1 + OPEN_RETRIES):
                process = subprocess.Popen(cmd, env=scan_env(runtime, device), stdout=image, stderr=subprocess.PIPE,
                                           start_new_session=os.name == 'posix')
                threading.Thread(target=_listen, args=(process.stderr, heard), daemon=True).start()
                size, since, display, this_time = 0, time.monotonic(), _Display(), []
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
                    if said is not None:
                        since = time.monotonic()
                        log.write(said)
                        this_time.append(said)
                    text = display.feed(said or '', end=said is None)
                    if shown and text:
                        try:
                            progress.write(text)
                            progress.flush()
                        except (OSError, ValueError):   # nobody is reading any more: the scan goes on, and the log has it all
                            shown = False
                    if said is None:
                        break
                code = process.wait()
                if not (code and open_failed(''.join(this_time)) and os.fstat(image.fileno()).st_size == 0 and attempt < OPEN_RETRIES):
                    break
                # The scanner wasn't ready to open (seen on the first scan of a session): nothing moved and
                # nothing was written, so wait and ask once more. The log keeps both tries.
                note = f'The scanner wasn’t ready; trying again in {OPEN_WAIT} seconds.\n'
                log.write(note)
                if shown:
                    try:
                        progress.write(note); progress.flush()
                    except (OSError, ValueError):
                        shown = False
                process.stderr.close()   # the first try's: it has said all it will
                time.sleep(OPEN_WAIT)
                heard = queue.Queue()
            image.flush()
            os.fsync(image.fileno())
        if code:
            raise CaptureError(f"scanimage exited with status {code}; see {p['log']}")   # the scanner's own words are in what it said
        info = inspect_tiff(p['partial'])
        check_tiff(info, plan, prof)
        health = channel_health(p['partial'])
        front = _logged_frontend(p['log'])
        with p['partial'].open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        # All that the record will say is known before the file takes its name, so that whatever
        # stops this between the two, the record written then is still the true one.
        verified = dict(record, channel_health=health, status='captured_verified', completed_utc=now(), sha256=digest,
                        bytes=p['partial'].stat().st_size, tiff=info, **front)
        try:
            files.place(p['partial'], p['out'])   # never over a file that took the name meanwhile
        except FileExistsError:
            raise CaptureError(f"{p['out']} appeared during capture; not replacing it") from None
        _write_record(p['record'], verified)
        return verified
    except BaseException as exc:
        if process:
            _stop(process)
            try:
                with p['log'].open('a', encoding='utf-8') as log:   # what it said while stopping
                    while not heard.empty():
                        log.write(heard.get() or '')
            except OSError:   # the drive is full or gone, often why it failed: that is the error to report
                pass
        if verified is not None and p['out'].exists() and (not p['partial'].exists()
                                                           or files.mine(p['partial'], p['out'])):
            record = verified   # the master is whole, verified and under its name: only its record was behind
            try:
                p['partial'].unlink(missing_ok=True)   # its second name, if stopped just after the link
            except OSError:
                pass
        else:
            record.update(status='failed_or_interrupted', ended_utc=now(), error=str(exc) or (STOPPED if isinstance(exc, KeyboardInterrupt) else
                          type(exc).__name__), partial=str(p['partial']), **_logged_frontend(p['log']))
        try:
            _write_record(p['record'], record)
        except OSError:   # the drive has gone too, say: the first trouble is the one to report
            pass
        raise
