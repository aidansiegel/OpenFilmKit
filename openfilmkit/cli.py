"""Command line. Every command takes --json for scripts and models.

Exit status: 0 ok, 1 error, 2 no scanner found, 3 scanner found but not
safely identified, 130 interrupted.
"""
import argparse
import errno
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, capture, identify, options as opts, roll, scanners
from .runtime import Runtime, RuntimeMissing, ScanimageError, tiffinfo, udev_status

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
    try:
        info['tiffinfo'] = tiffinfo()
    except RuntimeMissing as e:
        info['tiffinfo'] = None
        out.say(f'\n! {e}: no scan can be saved without it. It comes with libtiff '
                '(brew install libtiff, or apt install libtiff-tools).')
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
    if not all(r['status'] in identify.SCANNABLE for r in results):
        return 3
    return 0 if info['tiffinfo'] else 1


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


def unknown_stock(name):
    """What to say when `name` isn't a stock the kit knows, or None. Such a name is kept as a label,
    and a slip of the keys (kodak-gold20O) would otherwise be met only as generic conversions."""
    from .films import stocks
    if not name or stocks.stock(name)['id'] != 'custom':
        return None
    import difflib
    near = difflib.get_close_matches(name.lower(), [s['id'] for s in stocks.stocks()], n=3, cutoff=0.6)
    meant = f" Did you mean {' or '.join(near)}?" if near else ''
    return (f'! No film stock called {name!r} in this kit.{meant} It is kept as a label, and conversions will use '
            'the generic profile. `openfilmkit stocks` lists the stocks; `roll set --stock` chooses one later.')


def cmd_roll_new(args, out):
    record = roll.create(args.path, args.film, args.stock, args.year, args.note)
    if unknown_stock(args.stock):
        out.say(unknown_stock(args.stock))
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
        args.output = str(roll.next_derived(roll_dir, source.stem, '.tif' if args.tiff else '.jpg'))
    try:
        record = convert.convert(args)
    except ValueError as e:
        raise Fail(str(e))
    model = stocks.profile(record['film_selection']['profile_id'])
    out.say(record['film_selection']['stock'] + ' → ' + model['name'])
    out.say(model['calibration'])
    out.say(f"Saved starting conversion: {record['output']}")
    out.say(f"Processing record: {record['output']}.json")
    for w in record['channel_health']['warnings']:
        out.say(f'! In the scan: {w}')
    out.result(record)
    return 0


def cmd_check(args, out):
    from . import health
    from .films.convert import raw_open
    reports, warned = {}, False
    for name in args.scans:
        try:
            report = health.check(raw_open(Path(name).expanduser()))
        except (ValueError, OSError) as e:
            raise Fail(f'{name}: {e}')
        out.say(f'{name}:')
        for line in health.describe(report):
            out.say(line)
        warned |= bool(report['warnings'])
        reports[name] = report
    out.result(reports)
    return 1 if warned else 0


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
                args.crop, args.dust, args.faces, args.color, args.straighten,
                args.lift_shadows, args.red_eye, args.scratches, args.spare_long_lines))
        else:
            sources.append(path)
    if args.output and len(sources) != 1:
        raise Fail('--output needs exactly one source')
    if args.spot and (len(sources) != 1 or Path(args.sources[0]).expanduser().is_dir()):
        raise Fail('--spot is for one photo at a time')
    if not sources:
        out.say('Nothing to clean: no conversions without a copy cleaned with these settings.')
    options = dict(crop=args.crop, dust=args.dust, protect_faces=args.faces, color=args.color,
                   straighten=args.straighten, lift_shadows=args.lift_shadows, red_eye=args.red_eye,
                   scratches=args.scratches, spare_long_lines=args.spare_long_lines, spots=args.spot)
    if args.pack or args.on or args.queue:
        return pack_clean(sources, args, options, out)
    from . import jobs
    records = []
    for source in sources:
        try:
            # Stage checkpoints, so stopping partway (Ctrl-C, sleep) keeps the slow work for a rerun.
            ck = jobs.local_checkpoints(jobs.clean_job(source, args.output, **options)[0])
            record = clean.clean(source, args.output, say=out.error, checkpoints=ck, **options)
            shutil.rmtree(ck, ignore_errors=True)
        except (clean.CleanError, jobs.JobError) as e:
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


def pack_clean(sources, args, options, out):
    """clean --pack, --on HOST (pack, then run there) or --queue NAME (pack, then queue)."""
    from . import jobs, queues, remote
    if sum(bool(x) for x in (args.pack, args.on, args.queue)) > 1:
        raise Fail('choose one of --pack, --on and --queue')
    try:
        q = queues.queue_settings(args.queue) if args.queue else None
    except queues.QueueError as e:
        raise Fail(str(e))
    into = args.pack or (queues.outbox(q) if q else remote.outbox(args.on))
    packed = []
    for source in sources:
        try:
            package = jobs.pack_clean(source, into, output=args.output, **options)
        except (jobs.JobError, OSError) as e:
            out.error(f'openfilmkit: {source}: {e}')
            packed.append({'source': str(source), 'error': str(e)})
            continue
        if args.pack:
            out.say(f'{source.name} → job {package.name[:12]} in {package.parent}')
        packed.append({'source': str(source), 'package': str(package), 'id': package.name})
    if q and any('error' not in p for p in packed):
        try:
            queues.submit(q, list(dict.fromkeys(p['package'] for p in packed if 'error' not in p)), say=out.say)
        except queues.QueueError as e:
            raise Fail(str(e))
    if args.on and any('error' not in p for p in packed):
        ready = [p['package'] for p in packed if 'error' not in p]
        to = Path(args.output).expanduser().resolve().parent if args.output else None
        try:
            results = remote.run_on(args.on, ready, to=to, keep=args.keep_remote, say=out.say)
        except remote.RemoteError as e:
            raise Fail(str(e))

        def mine(p):   # this photo's own destination among the job's
            got = results[p['package']]
            if isinstance(got, dict):
                return dict(got, source=p['source'])
            return next((r for r in got if r['source'] == p['source']), got[0])
        packed = [p if 'error' in p else mine(p) for p in packed]
        for rec in packed:
            if 'error' in rec:
                out.error(f"openfilmkit: {rec.get('source') or rec.get('package')}: {rec['error']}")
            else:
                out.say(f"{Path(rec['source']).name} → {Path(rec['output']).name} (on {args.on})")
    out.result(packed)
    return 0 if all('error' not in p for p in packed) else 1


def cmd_job(args, out):
    from . import jobs
    try:
        if args.job_command == 'show':
            info = jobs.show(args.package)
            out.say(f"{info['id'][:12]}: {info['state']} — {info['job']['command']} of {info['about']['source']}"
                    f" → {info['about']['output_name']}")
            out.result(info)
            return 0
        if args.job_command == 'remove':
            for package in args.packages:
                jobs.remove(package)
            out.result({'removed': args.packages})
            return 0
        if args.job_command == 'collect':
            records = jobs.collect(args.package, args.to)
            for record in records:
                out.say(f"Collected {record['output']}")
            out.result(records)
            return 0
        if args.job_command == 'build':
            out.say(jobs.build())
            out.result({'version': __version__, 'build': jobs.build()})
            return 0
    except jobs.JobError as e:
        raise Fail(str(e))
    records = []
    for package in args.packages:   # run
        try:
            already = jobs.done(package)
            record = already or jobs.run(package, say=out.error)
        except jobs.JobError as e:
            out.error(f'openfilmkit: {package}: {e}')
            records.append({'package': str(package), 'error': str(e), 'always_fails': isinstance(e, jobs.AlwaysFails)})
            continue
        out.say(f"{Path(package).name[:12]}: {'already done' if already else 'done'} → {Path(record['output']).name}")
        records.append(record)
    out.result(records)
    return 0 if all('error' not in r for r in records) else 1


def cmd_queue(args, out):
    from . import queues
    try:
        q = queues.queue_settings(args.name)
        if args.queue_command == 'collect':
            records = queues.collect(q, args.to, say=out.say)
            if not records:
                out.say('Nothing finished to collect.')
            out.result(records)
            return 0 if all('error' not in r for r in records) else 1
        if args.queue_command == 'retry':
            retried = queues.retry(q, args.job, say=out.say)
            if not retried:
                out.say('No failed jobs.')
            out.result({'retried': retried})
            return 0
        if args.queue_command == 'remove':
            out.result({'removed': queues.remove(q, args.job, say=out.say)})
            return 0
        info = queues.status(q)
    except queues.QueueError as e:
        raise Fail(str(e))
    out.say(f"Queue {info['queue']}: {len(info['jobs'])} job{'s' if len(info['jobs']) != 1 else ''}")
    for row in info['jobs']:
        where = f" on {row['worker']}" if row.get('worker') else ''
        extra = f": {row['error']}" if row.get('error') else ''
        if row['state'] == 'failed' and not row['final']:
            extra += f" (try {row['tries']} of {queues.TRIES}; it will try again)"
        if row.get('why_waiting'):
            extra += f": no worker can take it now ({row['why_waiting']})"
        what = f"{Path(row['photo'] or '').name} → {row['output']}" if row['output'] else '(no description)'
        out.say(f"  {row['id'][:12]}  {row['state']}{where}  {what}{extra}")
    if any(row['state'] == 'failed' and row['final'] for row in info['jobs']):
        out.say('  Failed jobs stay until `queue retry` (try them again) or `queue remove --job ID`.')
    for w in info['workers']:
        busy = f", running {w['running'][:12]}" if w.get('running') else ''
        seen = (f"not seen for {queues.ago(w['seen_ago_s'])} (stopped, frozen or cut off)" if w['silent']
                else f"seen {queues.ago(w['seen_ago_s'])} ago")
        out.say(f"  worker {w['name']}: {w.get('version')}, up to {w.get('memory_gb')} GB, "
                f"hours {w.get('hours') or 'any'}, {seen}{busy}")
    out.result(info)
    return 0


def cmd_ui(args, out):
    try:
        from .ui import app
    except ImportError as e:
        raise Fail(f'the UI needs Textual and textual-image ({e.name} is missing): '
                   'pip install "openfilmkit[ui]"')
    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        raise Fail(f'{folder} is not a folder')
    app.run(folder)
    return 0


def cmd_worker(args, out):
    from . import queues
    import signal
    try:
        s = queues.worker_settings(queue=args.queue, drop=args.drop, name=args.name, memory_gb=args.memory,
                                   hours=args.hours, ssh_command=args.ssh_command, busy_when=args.busy_when)
    except queues.QueueError as e:
        raise Fail(str(e))

    def stop(signum, frame):   # systemd's stop: like Ctrl-C, so the slow work is pushed and the claim let go
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    w = queues.Worker(s, say=out.say)
    out.say(f"Worker {s['name']}: {s['queue']}, up to {s['memory_gb']} GB, hours {s['hours'] or 'any'}")
    try:
        if args.once:
            jid = w.once()
            out.result({'job': jid})
            return 0
        w.forever()
    except queues.QueueError as e:
        raise Fail(str(e))


def spot(text):
    """X,Y or X,Y,R (whole pixels) for clean --spot."""
    try:
        values = [int(v) for v in text.split(',')]
    except ValueError:
        values = []
    if len(values) not in (2, 3) or min(values) < 0:
        raise argparse.ArgumentTypeError(f'expected X,Y or X,Y,R in pixels, not {text!r}')
    return tuple(values)


def film_info(args, roll_dir):
    base = roll.load(roll_dir) if roll_dir else {}
    film = args.film or base.get('film') or 'unknown'
    return {'film': film, 'stock': args.stock or base.get('stock', ''),
            'note': args.note or '', 'roll': base or None}


# scanimage options the kit sets itself, from the scanner's profile and its own options: given again
# with --scanimage-arg, the later one would win, and the record would go on stating the plan.
OWNED = ('--mode', '--depth', '--resolution', '--source', '--format', '--device-name', '-d', '--output-file', '-o',
         '--batch', '-b', '--dont-scan', '-n', '--all-options', '-A', '--list-devices', '-L',
         '--formatted-device-list', '-f', '--test', '-T', '--help', '-h', '--version', '-V')


def _ask(prompt):
    """An answer typed at the terminal; "q" when there is none to be had (Ctrl-D, a closed terminal)."""
    try:
        return input(prompt).strip()
    except EOFError:
        return 'q'


def cmd_scan(args, out):
    import signal
    target = Path(args.target).expanduser().absolute()
    roll_dir = None
    if target.is_dir():
        if not roll.is_roll(target):
            raise Fail(f'{target} is a folder but not a roll; run `openfilmkit roll new {args.target}`')
        roll_dir = target
    elif args.frames != 1:
        raise Fail('--frames needs a roll folder, not a file name')
    if args.frames < 1:
        raise Fail('--frames is the number of frames to scan: 1 or more')
    if args.dpi is not None and args.dpi <= 0:
        raise Fail('--dpi is a resolution: a number above 0')
    extra = args.scanimage_arg or []
    for arg in extra:
        if arg.split('=')[0] in OWNED or arg.startswith('--batch'):
            raise Fail(f'--scanimage-arg {arg}: the kit sets that itself (see --dpi, --source and --device), and '
                       'given twice the record would say one thing and the scan be another')
    if unknown_stock(args.stock):
        out.say(unknown_stock(args.stock))
    interactive = sys.stdin.isatty() and not out.as_json
    if args.frames > 1 and not interactive:
        # Nothing would wait for the film to be moved: the same frame, scanned again and again.
        raise Fail('--frames above 1 waits at a prompt for you to move the film on, so it needs a terminal '
                   'and no --json; scan one frame per command instead')

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
    settings = runtime.run(capture.command(runtime, name, plan, extra)[1:-2] +
                           ['--all-options', '--dont-scan']).stdout
    film = film_info(args, roll_dir)

    def stop(signum, frame):   # `kill`, or the terminal closed: like Ctrl-C, so the scanner is stopped and the record says so
        raise KeyboardInterrupt
    before = {s: signal.signal(s, stop) for s in (getattr(signal, n, None) for n in ('SIGTERM', 'SIGHUP')) if s}
    records = []
    frame = 0
    try:
        while frame < args.frames:
            dest = roll.next_frame(roll_dir) if roll_dir else target
            if frame and interactive:
                if _ask('Position the next frame, then press Enter (q to stop): ') == 'q':
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
                    choice = _ask('r to retry, s to skip, q to stop: ')
                    if choice == 'r':
                        records.pop()   # the frame is being scanned again: this try isn't how it ended
                        continue
                    if choice == 's':
                        frame += 1
                        continue
                break
            out.say(f"\nSaved and verified: {dest}\nSHA-256: {record['sha256']}")
            if record['channel_health'].get('error'):
                out.say(f"! What its channels hold was not checked: {record['channel_health']['error']}")
            for w in record['channel_health']['warnings']:
                out.say(f'! {w}')
            records.append({k: record[k] for k in ('output', 'status', 'sha256', 'bytes', 'tiff', 'channel_health')})
            frame += 1
    finally:
        for s, handler in before.items():
            signal.signal(s, handler)
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
    s = sub.add_parser('models', parents=[common], help='the trained models clean --scratches uses')
    s.add_argument('--fetch', action='store_true', help='download any that are missing, and check them')

    s = sub.add_parser('convert', parents=[common],
                       help='viewing copy of a color negative or slide from a linear RGB16 master')
    s.add_argument('source')
    s.add_argument('--output', help='JPEG/PNG (8-bit) or TIFF (16-bit); default: the roll\'s derived/<frame>-vN.jpg')
    s.add_argument('--tiff', action='store_true',
                   help='16-bit TIFF at full resolution instead of a JPEG (derived/<frame>-vN.tif)')
    s.add_argument('--stock', help='film ID from `openfilmkit stocks`; default: the roll or capture record')
    s.add_argument('--roll', help='roll folder whose film choice to use')
    s.add_argument('--profile', help='explicit numerical profile, e.g. agfa-hdc200 or generic')
    s.add_argument('--crop', type=int, nargs=4, metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'))
    s.add_argument('--calibration', help='scene-shared measured density calibration JSON')
    s.add_argument('--scene', help='scene name from the density calibration plan')
    s.add_argument('--recover-clipped-blue', action=argparse.BooleanOptionalAction, default=None,
                   help='estimate missing blue from red/green in viewing copies; raw detail stays lost')
    s.add_argument('--levels-from', help="share another conversion record's channel endpoints")
    s.add_argument('--max-side', type=int,
                   help='longest side in pixels; 0 = full resolution (default: 1800 for JPEG/PNG, full for TIFF)')
    s.add_argument('--contrast', type=float, help='default 1.3 for negatives, 1.0 for slides')
    s.add_argument('--gamma', type=float, default=1.0, help='above 1 lifts midtones')
    s.add_argument('--warmth', type=float, default=0.0, help='small balance adjustment, -1 to +1')

    s = sub.add_parser('check', parents=[common],
                       help="what each channel of a scan holds: clipping at 0 or full scale, and codes a digital "
                            'stretch left unused (exit status 1 if anything is wrong)')
    s.add_argument('scans', nargs='+', help='linear RGB16 scans: masters, SilverFast or VueScan raw files')

    s = sub.add_parser('clean', parents=[common],
                       help='remove dust and holder edges from converted copies (new files + recipes)')
    s.add_argument('sources', nargs='+', help='JPEG/PNG conversions, or a roll folder (its uncleaned conversions)')
    s.add_argument('--output', help='output file (one source only); default <name>-clean-vN beside it')
    s.add_argument('--no-crop', dest='crop', action='store_false', help='keep film-holder edges')
    s.add_argument('--no-dust', dest='dust', action='store_false', help='skip dust and fiber removal')
    s.add_argument('--no-faces', dest='faces', action='store_false',
                   help="don't detect faces (they're otherwise protected from dust removal)")
    s.add_argument('--color', action='store_true', help='also correct levels and color cast')
    s.add_argument('--colour', dest='color', action='store_true', help=argparse.SUPPRESS)
    s.add_argument('--straighten', type=float, metavar='DEGREES', help='rotate, positive = counter-clockwise')
    s.add_argument('--lift-shadows', type=float, metavar='GAMMA', help='brighten shadows, e.g. 0.85')
    s.add_argument('--red-eye', action='store_true', help='darken flash-red pupils')
    s.add_argument('--scratches', action='store_true',
                   help='also find scratches and debris with a trained model and fill them in with another '
                        'model (MI-GAN); downloads about 100 MB of models on first use')
    s.add_argument('--spare-long-lines', action='store_true',
                   help='with --scratches: leave long straight lines outside faces (tile grout, wires), '
                        'which the model can take for scratches')
    s.add_argument('--spot', type=spot, action='append', metavar='X,Y[,R]',
                   help='also fill a spot you point at: X,Y in pixels of a copy cleaned with the same options, '
                        'radius R (default 0.4%% of the short side); repeat for more spots')
    s.add_argument('--pack', metavar='DIR',
                   help="don't clean now: write a job package per photo into DIR, for `openfilmkit job run` "
                        'here or on another machine')
    s.add_argument('--on', metavar='HOST',
                   help='clean on another machine over SSH (same OpenFilmKit version there) and bring the '
                        'results back; running it again resumes a stopped run')
    s.add_argument('--queue', metavar='NAME',
                   help='put the work in a queue for workers to do (a queue named in hosts.toml); '
                        '`openfilmkit queue collect` brings the results back')
    s.add_argument('--keep-remote', action='store_true',
                   help="with --on: keep the job packages afterwards (here and on the host)")

    j = sub.add_parser('job', help='run and collect job packages (made with clean --pack)').add_subparsers(
        dest='job_command', required=True)
    s = j.add_parser('run', parents=[common], help='run job packages; finished ones are left as they are')
    s.add_argument('packages', nargs='+')
    s = j.add_parser('show', parents=[common], help="a package's ID, state and job")
    s.add_argument('package')
    s = j.add_parser('collect', parents=[common],
                     help="copy a finished job's result and recipe out, beside the original by default")
    s.add_argument('package')
    s.add_argument('--to', metavar='DIR', help='folder to copy into')
    qp = sub.add_parser('queue', help='a job queue several machines work from').add_subparsers(
        dest='queue_command', required=True)
    s = qp.add_parser('status', parents=[common], help='jobs in the queue and the workers taking them')
    s.add_argument('name', nargs='?', help='the queue (default: the only one in hosts.toml)')
    s = qp.add_parser('collect', parents=[common],
                      help='bring finished jobs back beside their originals and clear them from the queue')
    s.add_argument('name', nargs='?', help='the queue (default: the only one in hosts.toml)')
    s.add_argument('--to', metavar='DIR', help='folder to copy into instead')
    s = qp.add_parser('retry', parents=[common], help='have the workers try failed jobs again')
    s.add_argument('name', nargs='?', help='the queue (default: the only one in hosts.toml)')
    s.add_argument('--job', action='append', metavar='ID',
                   help='a failed job, by its ID or the start of it (repeatable; default: every failed job)')
    s = qp.add_parser('remove', parents=[common], help='take jobs out of the queue, whatever their state')
    s.add_argument('name', nargs='?', help='the queue (default: the only one in hosts.toml)')
    s.add_argument('--job', action='append', metavar='ID', required=True,
                   help='the job, by its ID or the start of it (repeatable)')
    s = sub.add_parser('ui', help='the terminal UI: rolls, frames, previews, the queue')
    s.add_argument('folder', nargs='?', default='.', help='a roll, a folder of rolls, or a folder of photos')
    s = sub.add_parser('worker', parents=[common], help='take jobs from a queue and do them, one at a time')
    s.add_argument('--queue', metavar='HOST:PATH', help='the queue folder (default: [worker] in hosts.toml)')
    s.add_argument('--drop', metavar='HOST:PATH', help="this worker's drop folder (default: QUEUE/drop/NAME)")
    s.add_argument('--name', help="this worker's name (default: the machine's)")
    s.add_argument('--memory', type=float, metavar='GB', help='largest job to take, in GB (default 2.5)')
    s.add_argument('--hours', metavar='START-END', help='only start jobs in these hours, e.g. 22-7 or 10pm-7am')
    s.add_argument('--ssh-command', help='ssh command for rsync, e.g. "ssh -i ~/.ssh/queue_key"')
    s.add_argument('--busy-when', action='append', metavar='COMMAND',
                   help="a shell command that succeeds while this machine shouldn't run a job (repeatable)")
    s.add_argument('--once', action='store_true', help='do at most one job, then stop')
    s = j.add_parser('build', parents=[common], help="this OpenFilmKit's build: a fingerprint of its code")
    s = j.add_parser('remove', parents=[common], help='delete job packages (checked to be packages first)')
    s.add_argument('packages', nargs='+')

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


def cmd_models(args, out):
    from . import models
    if args.fetch:
        try:
            models.fetch(say=out.error)
        except models.ModelError as e:
            raise Fail(str(e))
    listing = models.status()
    out.say(f'Models for clean --scratches, in {models.folder()}:')
    for name, m in listing.items():
        out.say(f"  {name}: {'present' if m['present'] else 'not downloaded'} "
                f"({m['bytes'] / 1e6:.0f} MB) — {m['source']}")
    if not all(m['present'] for m in listing.values()):
        out.say('Run `openfilmkit models --fetch` to download them now (otherwise on first use).')
    out.result(listing)
    return 0


COMMANDS = {'doctor': cmd_doctor, 'scanners': cmd_scanners, 'options': cmd_options,
            'scan': cmd_scan, 'stocks': cmd_stocks, 'profiles': cmd_profiles,
            'convert': cmd_convert, 'check': cmd_check, 'clean': cmd_clean, 'models': cmd_models, 'job': cmd_job,
            'queue': cmd_queue, 'worker': cmd_worker, 'ui': cmd_ui}


def _pipe_error(e):
    """A write to a reader that has gone: BrokenPipeError, which Windows reports as EINVAL."""
    return isinstance(e, BrokenPipeError) or (sys.platform == 'win32' and e.errno == errno.EINVAL)


def _stdout_closed():
    """True when stdout's reader has gone; then point stdout at /dev/null so the
    interpreter's final flush doesn't raise the same error again."""
    try:
        sys.stdout.flush()
        return False
    except OSError as e:
        if not _pipe_error(e):
            return False
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
    except (RuntimeMissing, ScanimageError, roll.RollError, capture.CaptureError, OSError) as e:
        if isinstance(e, OSError) and _pipe_error(e) and _stdout_closed():
            # The reader went away (`openfilmkit scanners | head`): stop quietly, like a SIGPIPE.
            return 141
        out.error(f'openfilmkit: {e}')
        out.result({'error': str(e)})
        return 1


def run():
    """Entry point for the installed `openfilmkit` command."""
    sys.exit(main())


if __name__ == '__main__':
    run()
