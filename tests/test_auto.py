"""Automatic finishing (films/auto.py and convert's --auto) on made-up scans."""
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
    from PIL import Image
    from openfilmkit.films import auto, convert as conversion
    from openfilmkit import roll as rolls
except ImportError:
    np = None

BASE = np.array([40000., 18000., 11900.]) if np is not None else None   # orange film base, as scanned


def framed(h=300, w=450, seed=3):
    """A negative frame as the 8200i SE scans it: holder (no light) along the top and left, then a strip of clear
    film base, then the picture: a gray card scene (patches of several densities) under a warm cast."""
    rng = np.random.default_rng(seed)
    a = np.zeros((h, w, 3))
    yy, xx = np.mgrid[0:h, 0:w]
    density = 0.25 + 0.6 * ((xx // 40 + yy // 40) % 5) / 4          # a checkerboard of grays
    scene = BASE * (10 ** -density)[..., None] * np.array([0.8, 1.0, 1.15])   # the cast: less red, more blue
    a[:] = scene
    a[:, 25:40] = BASE                                              # clear film between frames
    a[:30, :] = 300                                                 # holder along the top
    a[:, :25] = 300                                                 # holder along the left
    a += rng.normal(0, 120, a.shape)
    return np.clip(np.rint(a), 0, 65535).astype(np.uint16)


def args(root, name, **kw):
    opts = dict(source=str(root / 'raw.tif'), output=str(root / name), stock='unknown', roll=None, profile=None,
                crop=None, levels_from=None, max_side=0, contrast=1.3, gamma=1., warmth=0., saturation=1.)
    opts.update(kw)
    return argparse.Namespace(**opts)


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class PictureBox(unittest.TestCase):
    def test_finds_the_picture_inside_holder_and_film_base(self):
        x0, y0, x1, y1 = auto.picture_box(framed())
        self.assertTrue(40 <= x0 <= 50, x0)          # past the holder and the film base strip
        self.assertTrue(30 <= y0 <= 40, y0)          # past the holder
        self.assertEqual((x1, y1), (450, 300))       # no edge on the right or bottom: left alone

    def test_a_frame_with_no_holder_is_left_whole(self):
        rng = np.random.default_rng(1)
        plain = np.clip(rng.normal(20000, 4000, (200, 300, 3)), 0, 65535).astype(np.uint16)
        self.assertEqual(auto.picture_box(plain), [0, 0, 300, 200])


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class Balance(unittest.TestCase):
    def test_pulls_a_cast_gray_scene_back_to_gray(self):
        rng = np.random.default_rng(2)
        gray = rng.uniform(.05, .6, (200, 200, 1)) * np.ones(3)
        cast = gray * np.array([1.25, 1.0, 0.7])                  # warm
        g = auto.balance(cast)
        fixed = cast * g
        spread = lambda a: np.abs(np.log(a[..., 0] / a[..., 2])).mean()
        self.assertLess(spread(fixed), spread(cast) / 10)
        self.assertAlmostEqual(float(g @ auto.W), 1.0, places=6)   # brightness kept

    def test_exposure_brightens_a_dark_frame_and_is_capped(self):
        dark = np.full((100, 100, 3), .02)
        dark[::7, ::7] = .2
        k = auto.exposure(dark)
        self.assertGreater(k, 1.5)
        self.assertLessEqual(auto.exposure(np.full((50, 50, 3), 1e-5)), 4.0)


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class RollCheck(unittest.TestCase):
    ROLL = [[1.06, .95, 1.18], [1.18, .93, 1.22], [1.12, .94, 1.19], [1.24, .91, 1.15], [1.05, .98, 1.15]]

    def axes(self, g):
        lg = np.log(g)
        return float(lg @ auto.GREEN_MAGENTA), float(lg @ auto.AMBER_BLUE)

    def test_a_frame_greener_than_its_roll_is_brought_back(self):
        frame = np.array([.86, 1.045, .965]); frame = frame / (frame @ auto.W)     # roll 35 frame L
        g, check = auto.settle(frame, self.ROLL + [frame])
        typical = np.median([self.axes(r)[0] for r in self.ROLL + [frame]])
        self.assertAlmostEqual(self.axes(g)[0], typical - .1, places=6)          # now 0.1 from the roll
        self.assertAlmostEqual(self.axes(g)[1], self.axes(frame)[1], places=6)   # the light's own color kept
        self.assertAlmostEqual(float(g @ auto.W), 1., places=6)
        self.assertGreater(check['moved'], .15)

    def test_warmer_light_is_left_alone(self):
        tungsten = np.array([.9, .95, 1.9]); tungsten = tungsten / (tungsten @ auto.W)   # far off on amber-blue only
        lg = np.log(tungsten); m = np.median([self.axes(r)[0] for r in self.ROLL])
        lg = lg - (lg @ auto.GREEN_MAGENTA - m) * auto.GREEN_MAGENTA                    # on the roll's green-magenta
        tungsten = np.exp(lg)
        g, check = auto.settle(tungsten, self.ROLL + [tungsten])
        np.testing.assert_allclose(g, tungsten / (tungsten @ auto.W), rtol=1e-6)
        self.assertEqual(check['moved'], 0)

    def test_fluorescent_light_keeps_its_extra_magenta(self):
        tube = np.array([1.02, .89, 2.0]); tube = tube / (tube @ auto.W)    # roll 35 frame D: 0.23 more magenta
        g, check = auto.settle(tube, self.ROLL + [tube])
        self.assertEqual(check['moved'], 0)
        np.testing.assert_allclose(g, tube / (tube @ auto.W), rtol=1e-6)

    def test_too_few_frames_to_compare(self):
        frame = np.array([.86, 1.045, .965])
        self.assertEqual(auto.settle(frame, self.ROLL[:2] + [frame]), (frame, None))


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class Tone(unittest.TestCase):
    def test_highlights_roll_off_instead_of_clipping(self):
        x = np.array([0., .5, .8, 1., 1.5, 4.])
        y = auto.shoulder(x)
        np.testing.assert_allclose(y[:3], x[:3])                 # below the knee: untouched
        self.assertTrue((np.diff(y) > 0).all() and y[-1] < 1)    # still rising, never white

    def test_the_curve_keeps_its_middle_and_ends(self):
        v = np.array([0., .1, .45, 1.])
        t = auto.curve(v, black=.1, pivot=.45, contrast=1.5, middle=.5)
        self.assertAlmostEqual(float(t[1]), 0, places=6)          # the black point is black
        self.assertAlmostEqual(float(t[2]), .5, places=6)         # the middle lands where asked
        self.assertAlmostEqual(float(t[3]), 1, places=6)
        old = auto.curve(np.linspace(0, 1, 11))
        v = np.linspace(0, 1, 11); up = v ** 1.3
        np.testing.assert_allclose(old, up / (up + (1 - v) ** 1.3), atol=1e-9)   # the defaults: the old curve

    def test_a_flat_frame_gets_more_contrast_than_a_punchy_one(self):
        rng = np.random.default_rng(4)
        flat = np.clip(rng.normal(.18, .03, (200, 200, 1)), .01, 1) * np.ones(3)
        punchy = np.clip(rng.lognormal(np.log(.18), 1.2, (200, 200, 1)), .001, 1) * np.ones(3)
        self.assertGreater(auto.tone(flat)['contrast'], auto.tone(punchy)['contrast'])
        self.assertLessEqual(auto.tone(flat)['contrast'], 2.)


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class Orientation(unittest.TestCase):
    def test_sky_on_the_side_is_turned_to_the_top(self):
        rgb = np.zeros((300, 450, 3), np.uint8)
        rgb[:] = (70, 90, 50)                     # grass
        rgb[:, 300:] = (170, 200, 245)            # sky on the right: the camera was turned
        turn, why = auto.orientation(rgb)
        self.assertEqual((turn, why), (270, 'sky'))

    def test_no_cue_keeps_it_as_scanned(self):
        rgb = np.full((300, 450, 3), 128, np.uint8)
        self.assertEqual(auto.orientation(rgb), (0, 'no clear cue'))


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class Convert(unittest.TestCase):
    def test_auto_crops_balances_and_records_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tifffile.imwrite(root / 'raw.tif', framed(), photometric='rgb')
            rec = conversion.convert(args(root, 'auto.png'))
            plain = conversion.convert(args(root, 'plain.png', auto=False))
            a = rec['automatic']
            self.assertIsNotNone(a['crop_found'])
            self.assertEqual(rec['raw_crop_pixels'], a['crop_found'])
            self.assertEqual(len(a['balance_rgb']), 3)
            self.assertIsNone(plain['automatic'])
            self.assertEqual(plain['raw_crop_pixels'], [0, 0, 450, 300])
            out = np.asarray(Image.open(root / 'auto.png')).astype(float)
            self.assertLess(out.shape[1], 450)
            self.assertEqual(json.loads((root / 'auto.png.json').read_text())['automatic']['rotation_cw'], 0)

    def test_an_approved_curve_is_not_rebalanced(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tifffile.imwrite(root / 'raw.tif', framed(), photometric='rgb')
            ramp = (65535 - np.arange(65536)).astype(np.uint16)
            np.save(root / 'lut.npy', np.stack([ramp] * 3, 1))
            rec = conversion.convert(args(root, 'v.png', lut=str(root / 'lut.npy'), contrast=1.0))
            self.assertIsNone(rec['automatic']['balance_rgb'])
            self.assertEqual(rec['automatic']['exposure'], 1.0)

    def test_a_roll_is_measured_once_and_checked(self):
        with tempfile.TemporaryDirectory() as d:
            roll = Path(d) / 'roll-01'
            rolls.create(roll, film='color-negative')
            for n in range(1, 6):
                tifffile.imwrite(roll / 'masters' / f'frame-{n:02d}.tif', framed(seed=n), photometric='rgb')
            master = roll / 'masters' / 'frame-01.tif'
            rec = conversion.convert(args(Path(d), 'one.png', source=str(master)))
            check = rec['automatic']['roll_check']
            self.assertEqual(check['frames'], 5)
            self.assertEqual(check['moved'], 0)             # the same scene five times: nothing is off
            stored = json.loads((roll / conversion.ROLL_BALANCE).read_text())
            self.assertEqual(sorted(stored), [f'frame-{n:02d}.tif' for n in range(1, 6)])
            stored['frame-02.tif']['gains'] = [9, 9, 9]     # kept, not measured again
            (roll / conversion.ROLL_BALANCE).write_text(json.dumps(stored))
            chosen = conversion.film_stocks.selection('unknown', {}, {}, None)
            table = conversion.roll_balance(roll, chosen, conversion.film_stocks.profile(chosen['profile_id']))
            self.assertEqual(table['frame-02.tif'], [9, 9, 9])

    def test_without_a_roll_there_is_no_check(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tifffile.imwrite(root / 'raw.tif', framed(), photometric='rgb')
            self.assertIsNone(conversion.convert(args(root, 'a.png'))['automatic']['roll_check'])


if __name__ == '__main__':
    unittest.main()
