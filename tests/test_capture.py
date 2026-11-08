import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openfilmkit import capture, cli, options as opts, roll, scanners  # noqa: E402
from openfilmkit.runtime import Runtime, RuntimeMissing  # noqa: E402

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
        with self.assertRaisesRegex(scanners.PlanError, '--source'):
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
