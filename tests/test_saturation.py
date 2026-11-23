"""convert --saturation: color strength around each pixel's own gray, at constant luminance."""
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np
    import tifffile
    from openfilmkit.films import convert as conversion
except ImportError:
    np = None


def lut():
    """An identity-like LUT: code -> the same fraction of full scale, per channel."""
    ramp = np.arange(65536, dtype=np.uint16)
    return np.stack([ramp, ramp, ramp], 1)


def patch(rgb, size=8):
    return np.full((size, size, 3), rgb, np.uint16)


@unittest.skipIf(np is None, 'needs numpy and tifffile')
class Saturation(unittest.TestCase):
    def test_one_leaves_the_pixels_as_they_were(self):
        raw = np.random.default_rng(3).integers(0, 65535, (16, 16, 3)).astype(np.uint16)
        before = conversion.render(raw, lut(), 1.3, 1.0, 0.0, 255)
        self.assertTrue(np.array_equal(before, conversion.render(raw, lut(), 1.3, 1.0, 0.0, 255, 1.0)))

    def test_more_moves_color_away_from_gray_and_keeps_luminance(self):
        raw = patch((30000, 20000, 12000))
        plain = conversion.render(raw, lut(), 1.0, 1.0, 0.0, 65535).astype(float)
        strong = conversion.render(raw, lut(), 1.0, 1.0, 0.0, 65535, 1.5).astype(float)
        spread = lambda a: a[0, 0].max() - a[0, 0].min()
        self.assertGreater(spread(strong), spread(plain) * 1.3)
        w = np.array([.2126, .7152, .0722])
        # luminance of the linear values before the tone curve is kept, so the output's
        # luminance-weighted sum stays close (within rounding and clipping)
        self.assertLess(abs(float(strong[0, 0] @ w) - float(plain[0, 0] @ w)), 0.02 * 65535)

    def test_zero_is_gray_and_gray_stays_gray(self):
        gray0 = conversion.render(patch((30000, 20000, 12000)), lut(), 1.0, 1.0, 0.0, 255, 0.0)
        self.assertLessEqual(int(gray0[0, 0].max()) - int(gray0[0, 0].min()), 1)
        neutral = conversion.render(patch((20000, 20000, 20000)), lut(), 1.0, 1.0, 0.0, 255, 2.5)
        self.assertLessEqual(int(neutral[0, 0].max()) - int(neutral[0, 0].min()), 1)

    def test_out_of_range_is_refused(self):
        with self.assertRaises(ValueError):
            conversion.render(patch((1, 2, 3)), lut(), 1.3, 1.0, 0.0, 255, 3.5)

    def test_convert_records_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rng = np.random.default_rng(1)
            y, x = np.mgrid[0:120, 0:180]
            base = 0.3 + 0.5 * (x / 180)
            a = base[..., None] * np.array([40000, 18000, 10000]) + rng.normal(0, 250, (120, 180, 3))
            tifffile.imwrite(root / 'raw.tif', np.clip(np.rint(a), 0, 65535).astype(np.uint16), photometric='rgb')
            args = argparse.Namespace(source=str(root / 'raw.tif'), output=str(root / 'v.png'), stock='kirkland-200',
                                      roll=None, profile='agfa-hdc200', crop=None, levels_from=None, max_side=0,
                                      contrast=1.3, gamma=1., warmth=0., saturation=1.4, auto=False)
            record = conversion.convert(args)
            self.assertEqual(record['saturation'], 1.4)
            self.assertEqual(json.loads(Path(str(root / 'v.png') + '.json').read_text())['saturation'], 1.4)
            # a Namespace without the attribute (older callers) still works, as 1
            del args.saturation
            args.output = str(root / 'w.png')
            self.assertEqual(conversion.convert(args)['saturation'], 1.0)

    def test_an_approved_lut_is_applied_as_given(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            raw = np.random.default_rng(2).integers(1000, 60000, (40, 60, 3)).astype(np.uint16)
            tifffile.imwrite(root / 'raw.tif', raw, photometric='rgb')
            ramp = (65535 - np.arange(65536)).astype(np.uint16)       # an inverting curve
            given = np.stack([ramp, ramp, ramp], 1)
            np.save(root / 'keeper-lut.npy', given)
            args = argparse.Namespace(source=str(root / 'raw.tif'), output=str(root / 'v.tif'), stock='kirkland-200',
                                      roll=None, profile='agfa-hdc200', crop=None, levels_from=None, max_side=0,
                                      contrast=1.0, gamma=1., warmth=0., saturation=1.0,
                                      lut=str(root / 'keeper-lut.npy'), tiff=True, auto=False)
            record = conversion.convert(args)
            self.assertEqual(record['lut_source']['path'], str((root / 'keeper-lut.npy').resolve()))
            out = tifffile.imread(root / 'v.tif')
            expected = 65535 - raw.astype(int)
            self.assertLessEqual(int(np.abs(out.astype(int) - expected).max()), 2)
            np.save(root / 'bad.npy', np.zeros((10, 3), np.uint16))
            args.lut, args.output = str(root / 'bad.npy'), str(root / 'w.tif')
            with self.assertRaisesRegex(ValueError, '65536 x 3'):
                conversion.convert(args)


if __name__ == '__main__':
    unittest.main()
