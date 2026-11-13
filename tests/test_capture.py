import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openfilmkit import capture, cli, identify, options as opts, roll, scanners  # noqa: E402
from openfilmkit.runtime import Runtime, RuntimeMissing, ScanimageError  # noqa: E402

FIXTURES = Path(__file__).parent / 'fixtures'
SE = opts.parse((FIXTURES / 'options-8200i-se.txt').read_text())
TEST = opts.parse((FIXTURES / 'options-test-backend.txt').read_text())
SE_PROFILE = scanners.profile('genesys/plustek-opticfilm-8200i-se')
PATCH = '0001-gl128-afe-port-replay.diff'


class Options(unittest.TestCase):
    def test_8200i_se_listing(self):
        self.assertEqual(SE['source']['choices'], ['Transparency Adapter'])
        self.assertEqual(SE['depth']['choices'], [16])
        self.assertEqual(opts.resolutions(SE)[-1], 7200)
        self.assertEqual((SE['x']['max'], SE['y']['max'], SE['x']['unit']), (36.58, 25, 'mm'))
        self.assertEqual(SE['preview']['kind'], 'bool')
        self.assertEqual(SE['clear-calibration']['kind'], 'button')
        self.assertEqual(SE['calibration-file']['flags'], ['advanced'])
        self.assertEqual(SE['gamma-table']['kind'], 'array')
        self.assertTrue(SE['gamma-table']['inactive'])
        self.assertEqual(opts.film_sources(SE), ['Transparency Adapter'])

    def test_test_backend_listing(self):
        r = TEST['resolution']
        self.assertEqual((r['kind'], r['min'], r['max'], r['step']), ('range', 1, 1200, 1))
        self.assertTrue(opts.allows_resolution(TEST, 75))
        self.assertFalse(opts.allows_resolution(TEST, 2400))
        self.assertEqual(TEST['source']['choices'], ['Flatbed', 'Automatic Document Feeder'])
        self.assertEqual(opts.film_sources(TEST), [])


class Plan(unittest.TestCase):
    def test_se_defaults(self):
        plan, warnings = scanners.plan(SE, SE_PROFILE, runtime_patches=[PATCH])
        self.assertEqual(plan, {'source': 'Transparency Adapter', 'mode': 'Color', 'depth': 16,
                                'dpi': 7200})
        self.assertEqual(warnings, [])

    def test_untested_but_allowed_dpi_warns(self):
        plan, warnings = scanners.plan(SE, SE_PROFILE, dpi=2400, runtime_patches=[PATCH])
        self.assertEqual(plan['dpi'], 2400)
        self.assertIn('untested', warnings[0])

    def test_driver_refusal_wins_over_profile(self):
        with self.assertRaises(scanners.PlanError):
            scanners.plan(SE, SE_PROFILE, dpi=5000)

    def test_runtime_without_patch_warns(self):
        _, warnings = scanners.plan(SE, SE_PROFILE, runtime_patches=[])
        self.assertTrue(any(PATCH in w for w in warnings))

    def test_unprofiled_scanner_needs_choices(self):
        # Both named at once: it used to take a run to learn of --source and another for --dpi.
        with self.assertRaisesRegex(scanners.PlanError, r'--source from: .*Flatbed.*; and --dpi'):
            scanners.plan(TEST)
        with self.assertRaisesRegex(scanners.PlanError, '--dpi'):
            scanners.plan(TEST, source='Flatbed')
        plan, warnings = scanners.plan(TEST, source='Flatbed', dpi=75)
        self.assertEqual(plan['dpi'], 75)
        self.assertIn('no kit profile', warnings[-1])

    def test_8_bit_only_driver_is_refused(self):
        eight = dict(SE, depth={'kind': 'list', 'choices': [8]})
        with self.assertRaisesRegex(scanners.PlanError, '16-bit'):
            scanners.plan(eight, SE_PROFILE)

    def test_full_area_uses_the_drivers_whole_range(self):
        plan, _ = scanners.plan(SE, SE_PROFILE, runtime_patches=[PATCH], whole_area=True)
        self.assertEqual(plan['geometry'], {'l': 0, 't': 0, 'x': 36.58, 'y': 25})
        plan, _ = scanners.plan(TEST, source='Flatbed', dpi=75, whole_area=True)
        self.assertEqual(plan['geometry'], {'l': 0, 't': 0, 'x': 200, 'y': 200})
        cmd = capture.command(mock.Mock(scanimage='scanimage'), 'test:0', plan)
        self.assertEqual(cmd[cmd.index('-x') + 1], '200')
        self.assertNotIn('--ignore-internal-offsets', ' '.join(cmd))
        self.assertNotIn('geometry', scanners.plan(SE, SE_PROFILE, runtime_patches=[PATCH])[0])

    def test_full_area_without_geometry_is_refused(self):
        no_geometry = {k: v for k, v in SE.items() if k not in ('l', 't', 'x', 'y')}
        with self.assertRaisesRegex(scanners.PlanError, 'full area'):
            scanners.plan(no_geometry, SE_PROFILE, whole_area=True)

    def test_full_frame_estimate(self):
        # 10369 x 7086 px x 6 bytes, the ~440 MB film-scan documents.
        self.assertEqual(scanners.estimate_bytes(SE, SE_PROFILE, 7200), 10369 * 7086 * 6)


class Rolls(unittest.TestCase):
    def test_create_and_number_frames(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'roll-07')
            roll.create(path, 'color-negative', 'Kodak Gold 200', 1999)
            self.assertTrue(roll.is_roll(path))
            with self.assertRaises(roll.RollError):
                roll.create(path)
            self.assertEqual(roll.next_frame(path).name, 'frame-01.tif')
            (path / 'masters' / 'frame-01.tif').write_bytes(b'')
            (path / 'masters' / 'frame-02.tif.partial').write_bytes(b'')  # failed attempt
            self.assertEqual(roll.next_frame(path).name, 'frame-03.tif')

    def test_bad_film_type(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(roll.RollError):
            roll.create(Path(d, 'r'), film='positive')

    def test_a_stock_the_kit_doesnt_know_is_said_when_the_roll_is_started(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = kit('roll', 'new', str(Path(d, 'typo')), '--stock', 'kodak-gold20O')   # O for 0
            self.assertEqual(code, 0)
            self.assertIn('kodak-gold200', out)                   # what was probably meant
            self.assertIn('generic', out)                         # and what conversions will use otherwise
            self.assertEqual(roll.load(Path(d, 'typo'))['stock'], 'kodak-gold20O')   # kept as a label all the same
            for n, known in enumerate(('kodak-gold200', 'Kodak Gold 200', '')):
                code, out, _ = kit('roll', 'new', str(Path(d, f'known-{n}')), '--stock', known)
                self.assertEqual((code, '!' in out), (0, False), known)

    def test_a_frame_named_in_capitals_keeps_its_number(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'roll-09')
            roll.create(path)
            (path / 'masters' / 'frame-01.tif').write_bytes(b'')
            (path / 'masters' / 'FRAME-02.TIF').write_bytes(b'')         # as another program, or a camera card, names it
            self.assertEqual(roll.next_frame(path).name, 'frame-03.tif')
            (path / 'roll.json').write_text('[1, 2]')                    # JSON, but not a roll's record
            with self.assertRaisesRegex(roll.RollError, 'damaged'):
                roll.load(path)

    def test_a_damaged_roll_file_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'roll-07')
            roll.create(path)
            (path / 'roll.json').write_text('{"format": 1, "name": ')      # cut short
            with self.assertRaisesRegex(roll.RollError, 'damaged'):
                roll.load(path)
            code, _, err = kit('roll', 'set', str(path), '--stock', 'kodak-gold200')
            self.assertEqual(code, 1)
            self.assertIn(str(path / 'roll.json'), err)


class Destination(unittest.TestCase):
    def test_refuses_existing_and_wrong_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(capture.CaptureError, '.tif'):
                capture.check_destination(capture.paths(Path(d, 'x.png')), 0)
            Path(d, 'x.tif.json').write_text('{}')
            with self.assertRaisesRegex(capture.CaptureError, 'refusing'):
                capture.check_destination(capture.paths(Path(d, 'x.tif')), 0)

    def test_refuses_without_space(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch('shutil.disk_usage', return_value=mock.Mock(free=10 * 1024 ** 2)):
            with self.assertRaisesRegex(capture.CaptureError, 'GiB free'):
                capture.check_destination(capture.paths(Path(d, 'x.tif')), 440 * 1024 ** 2)

    def test_tiff_checks(self):
        plan = {'dpi': 7200}
        full = {'width': 10369, 'height': 7086, 'dpi_x': 7200.0, 'dpi_y': 7200.0}
        capture.check_tiff(full, plan, SE_PROFILE)
        with self.assertRaisesRegex(capture.CaptureError, 'resolution'):
            capture.check_tiff(dict(full, dpi_x=3600.0), plan, SE_PROFILE)
        with self.assertRaisesRegex(capture.CaptureError, 'full frame'):
            capture.check_tiff(dict(full, height=5000), plan, SE_PROFILE)


def kit(*args):
    """Run the command line: (exit status, what it printed, what it said on stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


def fake_runtime(folder):
    """A folder that passes for a kit runtime; the tests stand in for its programs."""
    Path(folder, 'build-provenance.json').write_text(json.dumps(
        {'commit': 'abcdef1234', 'patches': [], 'backends': ['genesys']}))
    return str(folder)


class ScannerProgram(unittest.TestCase):
    """When scanimage fails or doesn't answer, the command says what it said: no traceback."""
    def test_what_scanimage_said_is_shown(self):
        busy = subprocess.CalledProcessError(
            1, ['scanimage'], stderr='scanimage: open of device genesys failed: Device busy\n')
        with tempfile.TemporaryDirectory() as d, mock.patch('subprocess.run', side_effect=busy):
            for command in (('doctor',), ('options',), ('scan', str(Path(d, 'frame.tif')))):
                code, out, err = kit(*command, '--runtime', fake_runtime(d), '--json')
                self.assertEqual(code, 1, command)
                self.assertIn('Device busy', err)
                self.assertIn('Device busy', json.loads(out)['error'])
            self.assertEqual([p.name for p in Path(d).iterdir()], ['build-provenance.json'])

    def test_a_scanner_that_doesnt_answer_is_said_too(self):
        slow = subprocess.TimeoutExpired(['scanimage'], 60)
        with tempfile.TemporaryDirectory() as d, mock.patch('subprocess.run', side_effect=slow):
            code, _, err = kit('doctor', '--runtime', fake_runtime(d))
            self.assertEqual(code, 1)
            self.assertIn('60 seconds', err)
            with self.assertRaisesRegex(ScanimageError, '60 seconds'):
                identify.list_scanners()


def terminal(there):
    """Run as at a terminal, or as without one (a script, a pipe)."""
    return mock.patch('sys.stdin', mock.Mock(isatty=lambda: there))


class Frames(unittest.TestCase):
    def test_several_frames_need_someone_there_to_move_the_film(self):
        """Without a terminal to pause at, --frames 3 scanned the same frame three times."""
        untouched = mock.patch.object(Runtime, 'find', side_effect=RuntimeMissing('no runtime in this test'))
        with tempfile.TemporaryDirectory() as d, untouched as find:
            path = Path(d, 'roll-01')
            roll.create(path)
            with terminal(True):
                code, _, err = kit('scan', str(path), '--frames', '3', '--json')
            self.assertEqual(code, 1)
            self.assertIn('--frames', err)
            with terminal(False):
                code, _, err = kit('scan', str(path), '--frames', '3')
            self.assertEqual(code, 1)
            self.assertIn('--frames', err)
            find.assert_not_called()                 # refused before the scanner is looked for
            with terminal(True):
                code, _, err = kit('scan', str(path), '--frames', '3')    # at a terminal it goes ahead
            self.assertIn('no runtime in this test', err)
            for args in (['--json'], []):            # and one frame never needed a prompt
                with terminal(False):
                    self.assertIn('no runtime in this test', kit('scan', str(path), *args)[2])


class ScanOptions(unittest.TestCase):
    def test_what_the_kit_sets_itself_cant_be_set_again_behind_its_back(self):
        """scanimage takes the later of two options: --scanimage-arg=--source=... scanned from another
        source while the record went on stating the plan."""
        untouched = mock.patch.object(Runtime, 'find', side_effect=RuntimeMissing('no runtime in this test'))
        with tempfile.TemporaryDirectory() as d, untouched as find:
            for extra in ('--resolution=1200', '--source=Flatbed', '--mode', '-d', '--output-file=/tmp/x.tif',
                          '--batch=x%d.tif', '--format=png', '--device-name=other:0'):
                code, _, err = kit('scan', str(Path(d, 'frame.tif')), f'--scanimage-arg={extra}')
                self.assertEqual(code, 1, extra)
                self.assertIn('the kit sets that itself', err, extra)
            for n, wrong in enumerate((['--dpi', '0'], ['--dpi', '-300'], ['--frames', '0'], ['--frames', '-1'])):
                roll.create(Path(d, f'roll-{n}'))
                self.assertEqual(kit('scan', str(Path(d, f'roll-{n}')), *wrong)[0], 1, wrong)
            find.assert_not_called()
            code, _, err = kit('scan', str(Path(d, 'frame.tif')), '--scanimage-arg=-x', '--scanimage-arg=20')
            self.assertIn('no runtime in this test', err)                # the scan area may be given this way

    def test_a_usb_listing_that_isnt_well_formed_falls_back_to_the_name(self):
        bad = (b'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><array><dict><key>USB Product Name</key>'
               b'<string>bad \x01 char</string></dict></array></plist>')
        with mock.patch('subprocess.check_output', return_value=bad), mock.patch.object(sys, 'platform', 'darwin'):
            self.assertIsNone(identify.usb_devices())


STUB = '''#!{python}
# A stand-in for scanimage: what it does is chosen by STUB_MODE, and it notes what happened to it in STUB_MARKS.
import os, signal, sys, time
mode, marks = os.environ.get('STUB_MODE'), os.environ.get('STUB_MARKS')
if '--version' in sys.argv:
    print('stub')
    sys.exit(0)
def mark(word):
    with open(marks, 'a') as f:
        f.write(word + chr(10))
mark(f'pid={{os.getpid()}}')
if mode == 'cancel':      # as scanimage does: the first signal cancels the scan and parks, a second exits at once
    got = []
    def told(signum, frame):
        got.append(signum)
        if len(got) > 1:
            mark('aborted')
            os._exit(3)
        mark('stopping')
    signal.signal(signal.SIGINT, told)
    signal.signal(signal.SIGTERM, told)
    sys.stderr.write('Progress: 1%' + chr(13))
    sys.stderr.flush()
    mark('scanning')
    while not got:
        time.sleep(0.05)
    time.sleep(0.5)       # the carriage going home
    mark('parked')
    sys.exit(2)
if mode == 'silent':
    sys.stderr.write('warming up' + chr(10))
    sys.stderr.flush()
    mark('scanning')
    time.sleep(120)
if mode == 'odd':
    sys.stderr.buffer.write(bytes([99, 97, 102, 233, 32, 255, 254, 10]))   # not UTF-8
for n in range(50):
    sys.stderr.write(f'Progress: {{n}}%' + chr(13))
    sys.stderr.flush()
sys.stdout.buffer.write(bytes(2000))
'''
PLAN = {'source': None, 'mode': 'Color', 'depth': 16, 'dpi': 75}


@unittest.skipIf(os.name != 'posix', 'the stand-in for scanimage is a script')
class StandIn(unittest.TestCase):
    """capture() driving a stand-in for scanimage: what a real scanner can do that SANE's test one doesn't."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.program = self.root / 'scanimage'
        self.program.write_text(STUB.format(python=sys.executable))
        self.program.chmod(0o755)
        self.marks, self.out = self.root / 'marks', self.root / 'frame.tif'
        for patch in (mock.patch.object(capture, 'MIN_FREE', 0), mock.patch.object(capture, 'MARGIN', 0),
                      mock.patch.object(capture, 'tiffinfo', lambda: 'tiffinfo')):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.temp.cleanup)

    def runtime(self, mode):
        return types.SimpleNamespace(scanimage=self.program, summary=lambda: {}, version=lambda: 'stub',
                                     env=lambda: dict(os.environ, STUB_MODE=mode, STUB_MARKS=str(self.marks)))

    def scan(self, mode, **kw):
        return capture.capture(self.runtime(mode), self.out, 'stub', PLAN, None, {}, {}, '', **kw)

    def checked(self):
        """The checks of the finished file passed, whatever is in it: these tests are about getting there."""
        info = {'width': 10, 'height': 10, 'dpi_x': 75.0, 'dpi_y': 75.0, 'bits_per_channel': 16, 'channels': 3}
        return mock.patch.multiple(capture, inspect_tiff=lambda path: info, check_tiff=lambda *a: None,
                                   channel_health=lambda path: {'channels': {}, 'warnings': []})

    def record(self):
        return json.loads(Path(str(self.out) + '.json').read_text())

    def gone(self):
        pid = int(self.marks.read_text().split()[0].partition('=')[2])
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    def test_ctrl_c_lets_scanimage_stop_the_scanner_itself(self):
        """Ctrl-C at a terminal goes to every program in the foreground. scanimage got it, began to
        cancel, and at once got a second signal from the kit, on which it exits in the middle."""
        helper = textwrap.dedent(f'''
            import os, sys, types
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
            from openfilmkit import capture
            capture.MIN_FREE = capture.MARGIN = 0
            capture.tiffinfo = lambda: 'tiffinfo'
            rt = types.SimpleNamespace(scanimage={str(self.program)!r}, summary=lambda: {{}}, version=lambda: 'stub',
                                       env=lambda: dict(os.environ, STUB_MODE='cancel', STUB_MARKS={str(self.marks)!r}))
            try:
                capture.capture(rt, {str(self.out)!r}, 'stub', {PLAN!r}, None, {{}}, {{}}, '', progress=open(os.devnull, 'w'))
            except KeyboardInterrupt:
                sys.exit(130)
        ''')
        at_terminal = subprocess.Popen([sys.executable, '-c', helper], start_new_session=True)
        try:
            for _ in range(200):
                if self.marks.exists() and 'scanning' in self.marks.read_text():
                    break
                time.sleep(0.05)
            os.killpg(at_terminal.pid, signal.SIGINT)
            self.assertEqual(at_terminal.wait(timeout=60), 130)
        finally:
            if at_terminal.poll() is None:
                at_terminal.kill()
        self.assertEqual(self.marks.read_text().split()[1:], ['scanning', 'stopping', 'parked'])
        self.assertEqual(self.record()['status'], 'failed_or_interrupted')

    def test_a_scan_that_goes_silent_is_given_up_on(self):
        with mock.patch.object(capture, 'STALL', 1), mock.patch.object(capture, 'LISTEN', 0.2):
            with self.assertRaisesRegex(capture.CaptureError, 'stuck'):
                self.scan('silent', progress=io.StringIO())
        self.assertTrue(self.gone())
        self.assertEqual(self.record()['status'], 'failed_or_interrupted')
        self.assertIn('warming up', Path(str(self.out) + '.scan.log').read_text())

    def test_a_display_nobody_reads_and_bytes_that_arent_text_dont_stop_a_scan(self):
        class Closed:
            def write(self, text):
                raise BrokenPipeError(32, 'Broken pipe')
        with self.checked():
            self.assertEqual(self.scan('odd', progress=Closed())['status'], 'captured_verified')
        log = Path(str(self.out) + '.scan.log').read_text()
        self.assertIn('caf', log)
        self.assertIn('Progress: 49%', log)                              # all of it is in the log all the same

    def test_the_record_is_right_even_if_writing_it_fails_at_the_end(self):
        write, calls = capture._write_record, []

        def fails_once(path, record):
            calls.append(record['status'])
            if len(calls) == 1:
                raise OSError(5, 'Input/output error')
            write(path, record)
        with self.checked(), mock.patch.object(capture, '_write_record', fails_once), self.assertRaises(OSError):
            self.scan('fine', progress=io.StringIO())
        self.assertEqual((self.record()['status'], self.out.exists(), Path(str(self.out) + '.partial').exists()),
                         ('captured_verified', True, False))

    def test_a_file_cut_short_is_caught_even_if_tiffinfo_lets_it_pass(self):
        """scanimage doesn't check its writes: a disk that fills mid-scan still ends with status 0."""
        lenient = self.root / 'tiffinfo'
        lenient.write_text('#!/bin/sh\necho "  Image Width: 100 Image Length: 100"\necho "  Resolution: 75, 75 pixels/inch"\n'
                           'echo "  Bits/Sample: 16"\necho "  Samples/Pixel: 3"\n[ -n "$SAYS" ] && echo "$SAYS" >&2\nexit 0\n')
        lenient.chmod(0o755)
        short = self.root / 'short.tif'
        short.write_bytes(bytes(10_000))
        whole = self.root / 'whole.tif'
        whole.write_bytes(bytes(60_000 + 200))
        with mock.patch.object(capture, 'tiffinfo', lambda: str(lenient)):
            with self.assertRaisesRegex(capture.CaptureError, 'cut short'):
                capture.inspect_tiff(short)
            self.assertEqual(capture.inspect_tiff(whole)['width'], 100)
            with mock.patch.dict(os.environ, {'SAYS': 'TIFFFillStrip: Read error on strip 0'}):
                with self.assertRaisesRegex(capture.CaptureError, 'Read error'):
                    capture.inspect_tiff(whole)
            slow = subprocess.TimeoutExpired(['tiffinfo'], 600)
            with mock.patch('subprocess.run', side_effect=slow), self.assertRaisesRegex(capture.CaptureError, '10 minutes'):
                capture.inspect_tiff(whole)

    def test_a_scan_whose_pixels_cant_be_read_back_has_failed(self):
        with mock.patch.object(capture, 'inspect_tiff', lambda path: {'width': 10, 'height': 10, 'dpi_x': 75.0, 'dpi_y': 75.0}), \
                mock.patch.object(capture, 'check_tiff', lambda *a: None):
            with self.assertRaisesRegex(capture.CaptureError, 'read back'):
                self.scan('fine', progress=io.StringIO())                # 2,000 zero bytes: not a TIFF at all
        self.assertEqual((self.record()['status'], self.out.exists()), ('failed_or_interrupted', False))


class Tiffinfo(unittest.TestCase):
    """A capture is verified with tiffinfo, so its absence is said before the scan, not after."""
    missing = RuntimeMissing('tiffinfo (libtiff) is required to verify captures')

    def test_a_scan_isnt_started_without_it(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(capture, 'tiffinfo', side_effect=self.missing):
            with self.assertRaisesRegex(RuntimeMissing, 'tiffinfo'):
                capture.capture(None, Path(d, 'frame.tif'), 'test:0', {}, None, {}, {}, '')
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_doctor_says_whether_it_is_there(self):
        scanner = {'device': 'genesys:libusb:001:004', 'backend': 'genesys', 'vendor': 'PLUSTEK',
                   'model': 'OpticFilm 8200i', 'id': 'genesys/plustek-opticfilm-8200i-se',
                   'status': identify.EXACT, 'profile': None, 'usb': None, 'problems': []}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(Runtime, 'version', lambda self: 'scanimage (sane-backends) 1.4.0'), \
                mock.patch.object(cli, 'detect', lambda runtime: ([scanner], [])):
            with mock.patch.object(cli, 'tiffinfo', return_value='/usr/bin/tiffinfo'):
                code, out, _ = kit('doctor', '--runtime', fake_runtime(d), '--json')
            self.assertEqual((code, json.loads(out)['tiffinfo']), (0, '/usr/bin/tiffinfo'))
            with mock.patch.object(cli, 'tiffinfo', side_effect=self.missing):
                code, out, err = kit('doctor', '--runtime', fake_runtime(d), '--json')
            self.assertEqual((code, json.loads(out)['tiffinfo']), (1, None))
            self.assertIn('tiffinfo', err)


def runtime_or_none():
    try:
        return Runtime.find()
    except RuntimeMissing:
        return None


@unittest.skipUnless(runtime_or_none() and 'test' in runtime_or_none().provenance['backends'],
                     'needs a kit runtime with SANE test backend')
class EndToEnd(unittest.TestCase):
    """Real scanimage, SANE's test backend, the whole capture path."""

    def setUp(self):
        # SANE's fake scanner is enabled only on request (etc/sane.d-test).
        patcher = mock.patch.dict('os.environ', {'OPENFILMKIT_TEST_BACKEND': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def kit(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_scan_roll_frames(self):
        small = ['--device', 'test:0', '--source', 'Flatbed', '--dpi', '75',
                 '--scanimage-arg=-x', '--scanimage-arg=20', '--scanimage-arg=-y',
                 '--scanimage-arg=20']
        with tempfile.TemporaryDirectory() as d, mock.patch.object(capture, 'MIN_FREE', 0), \
                mock.patch.object(capture, 'MARGIN', 0):
            path = Path(d, 'roll-01')
            self.assertEqual(self.kit('roll', 'new', str(path), '--film', 'slide')[0], 0)
            for expected in ('frame-01.tif', 'frame-02.tif'):
                code, out, _ = self.kit('scan', str(path), '--json', *small)
                self.assertEqual(code, 0)
                (c,) = json.loads(out)['captures']
                self.assertEqual((Path(c['output']).name, c['status']),
                                 (expected, 'captured_verified'))
            record = json.loads((path / 'masters' / 'frame-01.tif.json').read_text())
            self.assertEqual(record['film']['film'], 'slide')
            self.assertEqual(record['scanner']['status'], 'no-table')
            self.assertIn('--resolution=75', record['command'])
            self.assertEqual(record['tiff']['dpi_x'], 75.0)

    def test_a_scan_refused_for_room_doesnt_say_it_started(self):
        """It said "Scanning ... to frame-01.tif" and then "Capture failed: only 0.86 GiB free"."""
        small = ['--device', 'test:0', '--source', 'Flatbed', '--dpi', '75']
        with tempfile.TemporaryDirectory() as d, mock.patch.object(capture, 'MIN_FREE', 1 << 60):
            path = Path(d, 'roll-01')
            self.assertEqual(self.kit('roll', 'new', str(path))[0], 0)
            code, out, err = self.kit('scan', str(path), *small)
            self.assertEqual(code, 1)
            self.assertNotIn('Scanning', out + err)
            self.assertIn('Not scanned: only', err)
            self.assertEqual(list((path / 'masters').glob('*')), [])

    def test_full_area_scan_covers_the_whole_range(self):
        # The test backend selects 80 x 80 mm by default out of 200 x 200 mm.
        with tempfile.TemporaryDirectory() as d, mock.patch.object(capture, 'MIN_FREE', 0), \
                mock.patch.object(capture, 'MARGIN', 0):
            out = Path(d, 'full.tif')
            code, stdout, _ = self.kit('scan', str(out), '--json', '--device', 'test:0',
                                       '--source', 'Flatbed', '--dpi', '10', '--full-area')
            self.assertEqual(code, 0)
            record = json.loads(Path(str(out) + '.json').read_text())
            self.assertEqual(record['plan']['geometry'], {'l': 0, 't': 0, 'x': 200, 'y': 200})
            self.assertEqual((record['tiff']['width'], record['tiff']['height']), (78, 78))

    def test_no_answer_at_the_prompt_stops_the_roll_there(self):
        small = ['--device', 'test:0', '--source', 'Flatbed', '--dpi', '75',
                 '--scanimage-arg=-x', '--scanimage-arg=20', '--scanimage-arg=-y', '--scanimage-arg=20']
        with tempfile.TemporaryDirectory() as d, mock.patch.object(capture, 'MIN_FREE', 0), \
                mock.patch.object(capture, 'MARGIN', 0), terminal(True), mock.patch('builtins.input', side_effect=EOFError):
            path = Path(d, 'roll-01')
            roll.create(path)
            code, out, err = self.kit('scan', str(path), '--frames', '3', *small)     # Ctrl-D at "Position the next frame"
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted(p.name for p in (path / 'masters').glob('*.tif')), ['frame-01.tif'])

    def test_doctor_json(self):
        code, out, _ = self.kit('doctor', '--json')
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(all(s['backend'] == 'test' for s in data['scanners']))

    def test_two_devices_need_a_choice(self):
        code, _, err = self.kit('options')
        self.assertEqual(code, 2)
        self.assertIn('--device', err + _)


if __name__ == '__main__':
    unittest.main()
