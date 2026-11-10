"""What a scan's channels hold (clamped at 0, at full scale, digitally stretched), and smaller
conversions that average the scan instead of thinning it."""
import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np
    import tifffile
    from openfilmkit import capture, health
    from openfilmkit.films import convert as conversion
except ImportError:
    np = None
from openfilmkit import cli


def negative(h=400, w=600, seed=1):
    """A smooth, grainy linear scan like a color negative's: every code in each channel's range used."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    base = 0.3 + 0.5 * (x / w) * (0.6 + 0.4 * np.sin(y / 37))
    scale = np.array([40000, 18000, 10000])
    a = base[..., None] * scale + rng.normal(0, 250, (h, w, 3))
    return np.clip(np.rint(a), 0, 65535).astype(np.uint16)


@unittest.skipIf(np is None, 'needs numpy and tifffile')
class Health(unittest.TestCase):
    def test_a_sound_scan_has_nothing_to_report(self):
        report = health.check(negative())
        self.assertEqual(report['warnings'], [])
        self.assertTrue(all(ch['missing_codes'] < 0.01 for ch in report['channels'].values()))

    def test_a_channel_clamped_at_0_is_reported(self):
        a = negative()
        a[..., 2] = np.clip(a[..., 2].astype(int) - 4000, 0, None)      # a lowered blue offset
        report = health.check(a)
        (warning,) = report['warnings']
        self.assertIn('blue is at exactly 0', warning)
        self.assertGreater(report['channels']['blue']['at_zero'], 0.01)

    def test_a_channel_at_full_scale_is_reported(self):
        a = negative()
        a[:50, :, 0] = 65535
        self.assertIn('red is at full scale', ' '.join(health.check(a)['warnings']))

    def test_a_digitally_stretched_channel_is_reported(self):
        a = negative()
        a[..., 1] = np.clip(a[..., 1].astype(int) // 3 * 3, 0, 65535)    # two codes in three never occur
        report = health.check(a)
        (warning,) = report['warnings']
        self.assertIn('green never uses', warning)
        self.assertAlmostEqual(report['channels']['green']['missing_codes'], 2 / 3, delta=0.05)

    def test_capture_records_it_and_check_reports_it(self):
        with tempfile.TemporaryDirectory() as d:
            a = negative()
            a[..., 2] = 0
            path = Path(d, 'scan.tif')
            tifffile.imwrite(path, a, photometric='rgb')
            self.assertEqual([w.split(' ')[:4] for w in capture.channel_health(path)['warnings']],
                             [['blue', 'is', 'at', 'exactly']])                # a stuck channel isn't also "stretched"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(['check', str(path), '--json'])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out.getvalue())[str(path)]['channels']['blue']['max'], 0)
            tifffile.imwrite(Path(d, 'sound.tif'), negative(), photometric='rgb')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['check', str(Path(d, 'sound.tif'))]), 0)


@unittest.skipIf(np is None, 'needs numpy and tifffile')
class Shrink(unittest.TestCase):
    def test_blocks_are_averaged_including_the_short_ones_at_the_edges(self):
        a = np.arange(7 * 5 * 3, dtype=np.uint16).reshape(7, 5, 3) * 100
        small = conversion.shrink(a, 3)
        self.assertEqual(small.shape, (3, 2, 3))
        self.assertEqual(small[0, 0, 0], np.rint(a[:3, :3, 0].mean()))
        self.assertEqual(small[2, 1, 2], np.rint(a[6:, 3:, 2].mean()))       # a 1 x 2 block
        self.assertIs(conversion.shrink(a, 1), a)

    def test_a_speck_between_kept_pixels_still_shows(self):
        a = np.full((60, 60, 3), 30000, np.uint16)
        a[20:22, 20:22] = 2000                                              # a 2 px dark speck
        self.assertEqual(a[::6, ::6].min(), 30000)                           # thinning loses it
        self.assertLess(conversion.shrink(a, 6).min(), 28000)                # averaging keeps it

    def test_a_smaller_conversion_averages_and_records_how(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a = negative(240, 360)
            tifffile.imwrite(root / 'raw.tif', a, photometric='rgb')
            args = argparse.Namespace(source=str(root / 'raw.tif'), output=str(root / 'view.png'), stock='kirkland-200',
                                      roll=None, profile='agfa-hdc200', crop=None, levels_from=None, max_side=60,
                                      contrast=1.3, gamma=1., warmth=0.)
            record = conversion.convert(args)
            self.assertEqual((record['output_dimensions'], record['downscale']),
                             ([60, 40], {'method': 'block average', 'block': 6}))
            self.assertEqual(record['channel_health']['warnings'], [])
            full = conversion.convert(argparse.Namespace(**{**vars(args), 'output': str(root / 'full.png'), 'max_side': 0}))
            self.assertIsNone(full['downscale'])


if __name__ == '__main__':
    unittest.main()
