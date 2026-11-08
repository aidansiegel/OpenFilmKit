"""Command line. Every command takes --json for scripts and models.

Exit status: 0 ok, 1 error, 2 no scanner found, 3 scanner found but not
safely identified, 130 interrupted.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, capture, identify, options as opts, roll, scanners
from .runtime import Runtime, RuntimeMissing, udev_status

DATA = Path(__file__).parent / 'data'


class Fail(Exception):
    def __init__(self, message, code=1, detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail


class Out:
    """Human text to stdout, or to stderr with JSON on stdout."""
    def __init__(self, as_json):
        self.as_json = as_json
        self.stream = sys.stderr if as_json else sys.stdout

    def say(self, text=''):
        print(text, file=self.stream, flush=True)

    def error(self, text):
        print(text, file=sys.stderr, flush=True)

    def result(self, data):
        if self.as_json:
            json.dump(data, sys.stdout, indent=1, default=str)
            print()


def detect(runtime):
    table = identify.load_table()
    listing = identify.list_scanners(runtime.scanimage, env=runtime.env())
    results, unclaimed = identify.identify(listing, identify.usb_devices(), table)
    for r in results:
        prof = scanners.profile(r['id'])
        r['profile'] = prof['name'] if prof else None
    return results, unclaimed


def select(runtime, device=None):
    """The one scanner to use, and the device name to open it by."""
    results, unclaimed = detect(runtime)
    if device:
        results = [r for r in results if r['device'] == device]
        if not results:
            raise Fail(f'no scanner named {device}; see `openfilmkit doctor`', 2)
    if not results:
        hint = ''.join(f"\n  {u['problem']}" for u in unclaimed)
        raise Fail('no scanner found' + (hint or '. Check power, USB, and close other scanner '
                                         'software.'), 2, {'unclaimed': unclaimed})
    if len(results) > 1:
        names = ', '.join(r['device'] for r in results)
        raise Fail(f'{len(results)} scanners found ({names}); choose one with --device', 2)
    r = results[0]
    explicit_ambiguous = device and r['status'] == identify.AMBIGUOUS
    if r['status'] not in identify.SCANNABLE and not explicit_ambiguous:
        raise Fail('scanner not safely identified: ' + '; '.join(r['problems']), 3, r)
    # With one scanner, open the backend's default device: macOS libusb
    # numbers can change between processes, so a listed name may go stale.
    return r, (device or r['backend'])


def cmd_doctor(args, out):
    runtime = Runtime.find(args.runtime)
    info = {'openfilmkit': __version__, 'runtime': runtime.summary(), 'scanimage': runtime.version()}
    out.say(f"{info['scanimage']}\nruntime {runtime.prefix}")
    out.say(f"SANE {info['runtime']['commit'][:8]}, patches: "
            f"{', '.join(info['runtime']['patches']) or 'none'}")
    results, unclaimed = detect(runtime)
    info.update(scanners=results, unclaimed=unclaimed)
    for r in results:
        out.say(f"\n{r['device']}: {r['vendor']} {r['model']}")
        out.say(f"  id {r['id'] or '-'}   status {r['status']}   profile {r['profile'] or 'none'}")
        if r.get('usb'):
            u = r['usb']
            out.say(f"  USB {u['vendor']}:{u['product']} rev {u['bcd']} serial {u['serial']}")
        for problem in r['problems']:
            out.say(f'  ! {problem}')
    for u in unclaimed:
        out.say(f"\n! {u['problem']}")
    udev = udev_status(runtime)
    if udev is not None:
        info['linux_usb_rule'] = udev
        if udev.get('fix'):
            out.say('\n! The USB permission rule is not installed, so only root can open scanners.'
                    f"\n  Install it with:\n  {udev['fix']}")
    if not results and not unclaimed:
        out.say('\nNo scanner found. Check power and USB, and close other scanner software.')
    if args.dest:
        dest = Path(args.dest).expanduser()
        if dest.exists():
            free = shutil.disk_usage(dest).free
            info['dest'] = {'path': str(dest), 'free_bytes': free}
            out.say(f'\n{dest}: {free / 1024 ** 3:.1f} GiB free '
                    f'(about {free // (440 * 1024 ** 2)} full frames at 7200 dpi)')
        else:
            info['dest'] = {'path': str(dest), 'free_bytes': None}
            out.say(f'\n! {dest} does not exist (drive not mounted?)')
    out.result(info)
    if not results:
        return 2
    return 0 if all(r['status'] in identify.SCANNABLE for r in results) else 3


def cmd_scanners(args, out):
    tested = scanners.all_profiles()
    data = {'profiles': [{'id': p['id'], 'name': p['name'],
                          'hardware_record': bool(p['hardware_record'])} for p in tested]}
    out.say('Profiles in this kit:')
    for p in tested:
        mark = 'used on hardware' if p['hardware_record'] else 'not yet tried'
        out.say(f"  {p['id']:<40} {mark}")
    path = DATA / 'sane-scanners.json'
    if path.is_file():
        sane = json.loads(path.read_text())
        devices = [d for d in sane['devices'] if d['devicetype'] == 'scanner']
        if not args.all:
            devices = [d for d in devices if d['film'] == 'dedicated'
                       and 'unsupported' not in (d['backend'], d['status'])]
        data['sane_commit'] = sane.get('sane_commit')
        data['sane'] = devices
        out.say(f"\nSANE {'scanners' if args.all else 'film scanners'} "
                f"({len(devices)}, from SANE {str(sane.get('sane_commit'))[:8]}):")
        for d in devices:
            out.say(f"  {d['status'] or '?':<11} {d['backend']:<10} {d['mfg'] or ''} {d['model']}"
                    f"{'  ' + d['usb'] if d['usb'] else ''}")
    out.result(data)
    return 0


def cmd_options(args, out):
    runtime = Runtime.find(args.runtime)
    r, name = select(runtime, args.device)
    text = runtime.run(['--device-name', name, '--all-options']).stdout
    out.say(text)
    out.result({'scanner': r, 'options': opts.parse(text)})
    return 0


def cmd_roll_new(args, out):
    record = roll.create(args.path, args.film, args.stock, args.year, args.note)
    out.say(f'Roll created: {Path(args.path).expanduser().absolute()}')
    out.result(record)
    return 0


def cmd_roll_set(args, out):
    from .films import stocks
    try:
        choice = stocks.roll_choice(args.stock, args.profile, args.made_in, args.year,
                                    args.edge_markings, args.calibration, args.recover_clipped_blue)
    except ValueError as e:
        raise Fail(str(e))
    if not roll.is_roll(args.path):
        raise Fail(f'{args.path} is not a roll folder; run `openfilmkit roll new` first')
    record = roll.set_choice(args.path, choice)
    out.say(json.dumps(choice, indent=2))
    out.result(record)
    return 0


def cmd_stocks(args, out):
    from .films import stocks
    items = stocks.stocks()
    for item in items:
        kind = ' [slide]' if item.get('film') == 'slide' else ''
        out.say(f"{item['id']:<23} {item['name']}{kind} | default: {item['default_profile']}")
        if item['candidate_profiles']:
            out.say('  Proposed alternatives: ' + ', '.join(item['candidate_profiles']))
    out.say('Generic means no stock-specific numerical response is available; '
            'all conversions need visual review.')
    out.result(items)
    return 0


def cmd_profiles(args, out):
    from .films import stocks
    items = [stocks.profile('generic')] + [stocks.profile(i) for i in stocks.profile_ids()]
    listing = []
    for item in items:
        if item['id'] == 'generic':
            out.say('generic — no stock-specific curve')
        else:
            out.say(f"{item['id']} — {item['name']}")
            out.say('  ' + item['calibration'])
            out.say('  Source: ' + item['source'])
        listing.append({k: item.get(k) for k in ('id', 'name', 'kind', 'film', 'calibration',
                                                 'source')})
    out.result(listing)
    return 0


def cmd_convert(args, out):
    try:
        from .films import convert, stocks
    except ImportError as e:
        raise Fail(f'conversion needs numpy and Pillow ({e.name} is missing)')
    source = Path(args.source).expanduser().resolve()
    if args.output is None:
        roll_dir = roll.containing(source)
        if not roll_dir:
            raise Fail('choose --output (the source is not in a roll folder)')
        args.output = str(roll.next_derived(roll_dir, source.stem))
    try:
        record = convert.convert(args)
    except ValueError as e:
        raise Fail(str(e))
    model = stocks.profile(record['film_selection']['profile_id'])
    out.say(record['film_selection']['stock'] + ' → ' + model['name'])
    out.say(model['calibration'])
    out.say(f"Saved starting conversion: {record['output']}")
    out.say(f"Processing record: {record['output']}.json")
    out.result(record)
    return 0


def cmd_clean(args, out):
    try:
        from . import clean
    except ImportError as e:
        raise Fail(f'clean needs OpenCV, numpy and Pillow ({e.name} is missing)')
    sources = []
    for s in args.sources:
        path = Path(s).expanduser()
        if path.is_dir():
            if not roll.is_roll(path):
                raise Fail(f'{path} is a folder but not a roll')
            sources += clean.roll_sources(path, clean.settings(
                args.crop, args.dust, args.faces, args.colour, args.straighten,
                args.lift_shadows, args.red_eye))
        else:
            sources.append(path)
    if args.output and len(sources) != 1:
        raise Fail('--output needs exactly one source')
    if not sources:
        out.say('Nothing to clean: no conversions without a copy cleaned with these settings.')
    records = []
    for source in sources:
        try:
            record = clean.clean(source, args.output, crop=args.crop, dust=args.dust,
                                 protect_faces=args.faces, colour=args.colour,
                                 straighten=args.straighten, lift_shadows=args.lift_shadows,
                                 red_eye=args.red_eye)
        except clean.CleanError as e:
            out.error(f'openfilmkit: {source}: {e}')
            records.append({'source': str(source), 'error': str(e)})
            continue
        summary = ', '.join(
            f"{s['step']} {s.get('pixels_filled', '')}".strip() if s['step'] == 'dust' else s['step']
            for s in record['steps'] if s['step'] != 'crop' or s['changed'])
        out.say(f"{source.name} → {Path(record['output']).name} ({summary or 'no changes needed'})")
        records.append(record)
    out.result(records)
    return 0 if all('error' not in r for r in records) else 1


def film_info(args, roll_dir):
    base = roll.load(roll_dir) if roll_dir else {}
    film = args.film or base.get('film') or 'unknown'
    return {'film': film, 'stock': args.stock or base.get('stock', ''),
            'note': args.note or '', 'roll': base or None}


def cmd_scan(args, out):
    target = Path(args.target).expanduser().absolute()
    roll_dir = None
    if target.is_dir():
        if not roll.is_roll(target):
            raise Fail(f'{target} is a folder but not a roll; run `openfilmkit roll new {args.target}`')
        roll_dir = target
    elif args.frames != 1:
        raise Fail('--frames needs a roll folder, not a file name')

    runtime = Runtime.find(args.runtime)
    scanner, name = select(runtime, args.device)
    prof = scanners.profile(scanner['id'])
    listing = runtime.run(['--device-name', name, '--all-options']).stdout
    options = opts.parse(listing)
    try:
        plan, warnings = scanners.plan(options, prof, args.dpi, args.source,
                                       runtime.summary()['patches'], args.full_area)
    except scanners.PlanError as e:
        raise Fail(str(e))
    for w in warnings + scanner['problems']:
        out.say(f'! {w}')
    estimate = scanners.estimate_bytes(options, prof, plan['dpi'], geometry=plan.get('geometry'))
    extra = args.scanimage_arg or []
    settings = runtime.run(capture.command(runtime, name, plan, extra)[1:-2] +
                           ['--all-options', '--dont-scan']).stdout
    film = film_info(args, roll_dir)
    interactive = sys.stdin.isatty() and not out.as_json

    records = []
    frame = 0
    while frame < args.frames:
        dest = roll.next_frame(roll_dir) if roll_dir else target
        if frame and interactive:
            if input('Position the next frame, then press Enter (q to stop): ').strip() == 'q':
                break
        out.say(f"Scanning {scanner['vendor']} {scanner['model']} at {plan['dpi']} dpi to {dest}")
        try:
            record = capture.capture(runtime, dest, name, plan, prof, scanner, film, settings,
                                     estimate, extra, progress=sys.stderr)
        except capture.CaptureError as e:
            out.say(f'\nCapture failed: {e}')
            records.append({'output': str(dest), 'status': 'failed_or_interrupted',
                            'error': str(e)})
            if interactive and roll_dir:
                choice = input('r to retry, s to skip, q to stop: ').strip()
                if choice == 'r':
                    continue
                if choice == 's':
                    frame += 1
                    continue
            break
        out.say(f"\nSaved and verified: {dest}\nSHA-256: {record['sha256']}")
        records.append({k: record[k] for k in ('output', 'status', 'sha256', 'bytes', 'tiff')})
        frame += 1
    if frame == 1 and records and records[0]['status'] == 'captured_verified':
        out.say('Check framing, focus and exposure before scanning the rest of the roll.')
    out.result({'scanner': scanner, 'plan': plan, 'warnings': warnings, 'captures': records})
    return 0 if records and all(r['status'] == 'captured_verified' for r in records) else 1


def parser():
    p = argparse.ArgumentParser(prog='openfilmkit', description='OpenFilmKit: free film digitization.')
    p.add_argument('--version', action='version', version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', help='machine-readable output on stdout')
    common.add_argument('--runtime', help='OpenFilmKit SANE runtime prefix (default: newest installed)')
    sub = p.add_subparsers(dest='command', required=True)

    s = sub.add_parser('doctor', parents=[common], help='check driver, scanner and identification')
    s.add_argument('--dest', help='also show free space where scans will go')
    s = sub.add_parser('scanners', parents=[common], help='scanners SANE supports and kit profiles')
    s.add_argument('--all', action='store_true', help='every SANE scanner, not only film scanners')
    s = sub.add_parser('options', parents=[common], help="the scanner's settings, from the driver")
    s.add_argument('--device')

    r = sub.add_parser('roll', help='roll folders').add_subparsers(dest='roll_command', required=True)
    s = r.add_parser('new', parents=[common], help='start a roll folder')
    s.add_argument('path')
    s.add_argument('--film', choices=roll.FILM_TYPES, default='unknown')
    s.add_argument('--stock', default='')
    s.add_argument('--year', type=int)
    s.add_argument('--note', default='')

    s = r.add_parser('set', parents=[common], help="choose a roll's stock and conversion profile")
    s.add_argument('path')
    s.add_argument('--stock', required=True)
    s.add_argument('--profile')
    s.add_argument('--made-in', choices=('Germany', 'Japan', 'Italy', 'USA', 'unknown'))
    s.add_argument('--year', type=int, help='identification clue only; never chooses an emulsion')
    s.add_argument('--edge-markings', default='')
    s.add_argument('--calibration', help='scene-shared density calibration JSON for this roll')
    s.add_argument('--recover-clipped-blue', action=argparse.BooleanOptionalAction, default=None)

    sub.add_parser('stocks', parents=[common], help='film stocks and their candidate profiles')
    sub.add_parser('profiles', parents=[common], help='numerical response profiles and sources')

    s = sub.add_parser('convert', parents=[common],
                       help='viewing copy of a color negative or slide from a linear RGB16 master')
    s.add_argument('source')
    s.add_argument('--output', help='JPEG/PNG; default: the roll\'s derived/<frame>-vN.jpg')
    s.add_argument('--stock', help='film ID from `openfilmkit stocks`; default: the roll or capture record')
    s.add_argument('--roll', help='roll folder whose film choice to use')
    s.add_argument('--profile', help='explicit numerical profile, e.g. agfa-hdc200 or generic')
    s.add_argument('--crop', type=int, nargs=4, metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'))
    s.add_argument('--calibration', help='scene-shared measured density calibration JSON')
    s.add_argument('--scene', help='scene name from the density calibration plan')
    s.add_argument('--recover-clipped-blue', action=argparse.BooleanOptionalAction, default=None,
                   help='estimate missing blue from red/green in viewing copies; raw detail stays lost')
    s.add_argument('--levels-from', help="share another conversion record's channel endpoints")
    s.add_argument('--max-side', type=int, default=1800, help='preview bound; 0 = full resolution')
    s.add_argument('--contrast', type=float, help='default 1.3 for negatives, 1.0 for slides')
    s.add_argument('--gamma', type=float, default=1.0, help='above 1 lifts midtones')
    s.add_argument('--warmth', type=float, default=0.0, help='small balance adjustment, -1 to +1')

    s = sub.add_parser('clean', parents=[common],
                       help='remove dust and holder edges from converted copies (new files + recipes)')
    s.add_argument('sources', nargs='+', help='JPEG/PNG conversions, or a roll folder (its uncleaned conversions)')
    s.add_argument('--output', help='output file (one source only); default <name>-clean-vN beside it')
    s.add_argument('--no-crop', dest='crop', action='store_false', help='keep film-holder edges')
    s.add_argument('--no-dust', dest='dust', action='store_false', help='skip dust and fibre removal')
    s.add_argument('--no-faces', dest='faces', action='store_false',
                   help="don't detect faces (they're otherwise protected from dust removal)")
    s.add_argument('--colour', action='store_true', help='also correct levels and colour cast')
    s.add_argument('--straighten', type=float, metavar='DEGREES', help='rotate, positive = counter-clockwise')
    s.add_argument('--lift-shadows', type=float, metavar='GAMMA', help='brighten shadows, e.g. 0.85')
    s.add_argument('--red-eye', action='store_true', help='darken flash-red pupils')

    s = sub.add_parser('scan', parents=[common], help='capture linear 48-bit TIFF masters')
    s.add_argument('target', help='a roll folder (next frame) or an explicit .tif path')
    s.add_argument('--frames', type=int, default=1, help='frames to scan into a roll, with prompts')
    s.add_argument('--dpi', type=int)
    s.add_argument('--full-area', action='store_true',
                   help="scan the driver's whole scan area, not its default selection")
    s.add_argument('--source', help="override the profile's source")
    s.add_argument('--device', help='device name from `openfilmkit doctor`, if several are connected')
    s.add_argument('--film', choices=roll.FILM_TYPES, help='metadata only; defaults to the roll')
    s.add_argument('--stock', help='metadata only; defaults to the roll')
    s.add_argument('--note', default='')
    s.add_argument('--scanimage-arg', action='append', metavar='ARG',
                   help='extra scanimage argument, recorded with the capture (repeatable)')
    return p


COMMANDS = {'doctor': cmd_doctor, 'scanners': cmd_scanners, 'options': cmd_options,
            'scan': cmd_scan, 'stocks': cmd_stocks, 'profiles': cmd_profiles,
            'convert': cmd_convert, 'clean': cmd_clean}


def _stdout_closed():
    """True when stdout's reader has gone; then point stdout at /dev/null so the
    interpreter's final flush doesn't raise the same error again."""
    try:
        sys.stdout.flush()
        return False
    except BrokenPipeError:
        pass
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass
    return True


def main(argv=None):
    args = parser().parse_args(argv)
    out = Out(getattr(args, 'json', False))
    try:
        if args.command == 'roll':
            return {'new': cmd_roll_new, 'set': cmd_roll_set}[args.roll_command](args, out)
        return COMMANDS[args.command](args, out)
    except KeyboardInterrupt:
        out.error('\nInterrupted.')
        return 130
    except Fail as e:
        out.error(f'openfilmkit: {e}')
        out.result({'error': str(e), 'detail': e.detail})
        return e.code
    except BrokenPipeError as e:
        if not _stdout_closed():
            out.error(f'openfilmkit: {e}')
            out.result({'error': str(e)})
            return 1
        # The reader went away (`openfilmkit scanners | head`): stop quietly, like a SIGPIPE.
        return 141
    except (RuntimeMissing, roll.RollError, capture.CaptureError, OSError) as e:
        out.error(f'openfilmkit: {e}')
        out.result({'error': str(e)})
        return 1


def run():
    """Entry point for the installed `openfilmkit` command."""
    sys.exit(main())


if __name__ == '__main__':
    run()
