import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np
    from PIL import Image
    from openfilmkit import clean
    from openfilmkit.clean import redeye, restore
except ImportError:
    clean = None
from openfilmkit import cli, roll

# Sally K. Ride, NASA photo S84-37256 (June 1984), public domain: https://images.nasa.gov/details/S84-37256
RIDE = Path(__file__).resolve().parent / 'fixtures' / 'sally-ride-1984.jpg'


def scene(w=900, h=600, seed=3):
    """A smooth, slightly noisy photo-like gradient."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[:h, :w]
    base = np.stack([90 + 60 * x / w, 100 + 40 * y / h, 120 - 30 * x / w], 2)
    return np.clip(base + rng.normal(0, 3, base.shape), 0, 255).astype(np.uint8)


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
class Clean(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, img, name='frame-v1.jpg'):
        path = self.root / name
        Image.fromarray(img).save(path, quality=98, subsampling=0)
        return path

    def test_dust_is_removed_and_recipe_written(self):
        img = scene()
        specks = [(150, 200), (420, 330), (700, 120), (610, 480)]
        for x, y in specks:
            img[y:y + 3, x:x + 3] = 255
        src = self.write(img)
        rec = clean.clean(src, protect_faces=False)
        out = np.asarray(Image.open(rec['output']).convert('RGB')).astype(int)
        for x, y in specks:
            self.assertLess(out[y + 1, x + 1].max(), 200, (x, y))
        dust = [s for s in rec['steps'] if s['step'] == 'dust'][0]
        self.assertIsNone(dust['face_detector'])
        self.assertGreater(dust['pixels_filled'], 0)
        recipe = json.loads(Path(rec['output'] + '.json').read_text())
        self.assertEqual(recipe['source_sha256'], clean.sha(src))
        self.assertEqual(recipe['output_sha256'], clean.sha(rec['output']))
        self.assertIsNone(recipe['filled_in_by_model'])   # no model drew anything into this copy
        self.assertEqual(Path(rec['output']).name, 'frame-v1-clean-v1.jpg')

    def test_date_stamp_is_protected(self):
        img = scene()
        img[540:546, 760:790] = (235, 90, 40)   # orange LED digits in the bottom-right band
        img[542:544, 770:772] = (255, 255, 255)  # a bright spot inside the stamp
        rec = clean.clean(self.write(img), protect_faces=False, crop=False)
        out = np.asarray(Image.open(rec['output']).convert('RGB')).astype(int)
        self.assertGreater(out[543, 771].min(), 200)
        self.assertGreater([s for s in rec['steps'] if s['step'] == 'dust'][0]['stamp_pixels_protected'], 0)

    def test_recipe_names_the_face_detector(self):
        dust = [s for s in clean.clean(self.write(scene()), crop=False)['steps'] if s['step'] == 'dust'][0]
        self.assertIn('YuNet', dust['face_detector'])

    def test_face_is_found_at_every_rotation(self):
        # Film frames are often scanned sideways. RIDE is 960 x 1200, her face in the upper middle.
        img = np.asarray(Image.open(RIDE).convert('RGB'))
        for turns in range(4):
            faces = clean.find_faces(np.ascontiguousarray(np.rot90(img, turns)))
            self.assertEqual(len(faces), 1, turns)
            # Back in the upright photo's coordinates, every rotation agrees on where the face is.
            back = clean._unrotate([faces[0]['box'][:2], faces[0]['box'][2:]], turns, np.rot90(img, turns).shape)
            x0, y0 = back.min(0)
            x1, y1 = back.max(0)
            self.assertTrue(364 < x0 < 384 and 250 < y0 < 270 and 602 < x1 < 622 and 558 < y1 < 578, (turns, back))

    def test_only_eyes_and_mouth_are_protected(self):
        img = np.asarray(Image.open(RIDE).convert('RGB'))
        face = clean.find_faces(img)[0]
        m = restore.feature_mask(img.shape, [face])
        for x, y in face['kps'][:2] + face['kps'][3:]:   # eyes and mouth corners
            self.assertTrue(m[int(y), int(x)])
        x0, y0, x1, y1 = face['box']
        self.assertLess(m[int(y0):int(y1), int(x0):int(x1)].mean(), 0.5)   # cheeks and forehead stay open

    def test_whole_face_box_is_protected_without_landmarks(self):
        m = restore.feature_mask((100, 100, 3), [{'box': [20, 30, 60, 80]}])
        self.assertTrue(m[30:80, 20:60].all())
        self.assertFalse(m[:30].any() or m[80:].any())

    def test_holder_strip_is_cropped(self):
        img = scene()
        img[:, :30] = 255                       # light past the holder along the left edge
        rec = clean.clean(self.write(img), protect_faces=False, dust=False)
        crop = [s for s in rec['steps'] if s['step'] == 'crop'][0]
        self.assertTrue(crop['changed'])
        self.assertGreaterEqual(crop['box'][0], 30)
        self.assertEqual(rec['output_dimensions'][1], 600)

    def test_red_pupil_turns_dark_and_neutral(self):
        img = np.full((200, 200, 3), (200, 160, 140), np.uint8)   # skin
        yy, xx = np.mgrid[:200, :200]
        img[np.hypot(yy - 100, xx - 100) <= 5] = (220, 40, 40)    # flash-red pupil
        out, fixed = redeye.correct(img, [{'box': [0, 0, 200, 200], 'kps': [[100, 100], [150, 150]]}])
        self.assertEqual(fixed, [(0, 0)])
        r, g, b = out[100, 100].astype(int)
        self.assertLess(r, 80)
        self.assertLess(abs(r - g), 25)
        self.assertTrue(np.array_equal(out[10, 10], img[10, 10]))

    def test_reproducible_and_never_overwrites(self):
        src = self.write(scene())
        a = clean.clean(src, self.root / 'a.jpg', protect_faces=False)
        b = clean.clean(src, self.root / 'b.jpg', protect_faces=False)
        self.assertEqual(Path(a['output']).read_bytes(), Path(b['output']).read_bytes())
        with self.assertRaises(clean.CleanError):
            clean.clean(src, self.root / 'a.jpg')
        with self.assertRaises(clean.CleanError):
            clean.clean(src, src)

    def test_dust_can_be_left_for_later(self):
        r = self.root / 'roll-02'
        roll.create(r)
        (r / 'derived').mkdir()
        img = scene()
        img[300:303, 450:453] = 255
        Image.fromarray(img).save(r / 'derived' / 'frame-01-v1.jpg', quality=98, subsampling=0)

        def run(*flags):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(['clean', str(r), '--json', '--no-faces', *flags]), 0)
            return json.loads(out.getvalue())

        first = run('--no-dust')
        self.assertEqual([s['step'] for s in first[0]['steps']], ['crop'])
        self.assertEqual(run('--no-dust'), [])          # same settings: already done
        later = run()                                    # dust now, from the original conversion
        self.assertEqual(later[0]['source'], first[0]['source'])
        self.assertEqual(Path(later[0]['output']).name, 'frame-01-v1-clean-v2.jpg')
        self.assertGreater(later[0]['steps'][0]['pixels_filled'], 0)
        self.assertEqual(run(), [])

    def test_unreadable_file_fails_alone(self):
        bad = self.root / 'broken.jpg'
        bad.write_bytes(b'not an image')
        with self.assertRaises(clean.CleanError):
            clean.clean(bad)

    def test_sixteen_bit_master_is_refused(self):
        p = self.root / 'master.png'
        Image.fromarray(np.full((10, 10), 30000, np.uint16)).save(p)
        with self.assertRaises(clean.CleanError):
            clean.clean(p)

    def test_roll_cleans_each_conversion_once(self):
        r = self.root / 'roll-01'
        roll.create(r)
        (r / 'derived').mkdir()
        for name in ('frame-01-v1.jpg', 'frame-02-v1.jpg'):
            Image.fromarray(scene()).save(r / 'derived' / name, quality=95)
        (r / 'derived' / '._frame-01-v1.jpg').write_bytes(b'\0\5\26\7')   # exFAT sidecar
        for expected in (2, 0):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(['clean', str(r), '--json', '--no-faces'])
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out.getvalue())), expected)
        self.assertEqual(sorted(p.name for p in (r / 'derived').glob('*-clean-v1.jpg')),
                         ['frame-01-v1-clean-v1.jpg', 'frame-02-v1-clean-v1.jpg'])

    def test_a_spot_pointed_at_in_the_cleaned_copy_is_filled(self):
        img = scene()
        img[:, :30] = 255                                         # holder light: the crop moves the picture
        first = clean.clean(self.write(img), protect_faces=False)
        l, t = first['steps'][-1]['box'][:2]
        img[400:410, 500:510] = 20                                # a dark blot the dust pass leaves alone
        src = self.write(img, 'frame-v2.jpg')
        plain = np.asarray(Image.open(clean.clean(src, self.root / 'plain.png', protect_faces=False)['output'])).astype(int)
        x, y = 505 - l, 405 - t                                   # where it is in the cleaned copy
        self.assertLess(plain[y, x].max(), 60)
        rec = clean.clean(src, self.root / 'spotted.png', protect_faces=False, spots=[(x, y, 9)])
        out = np.asarray(Image.open(rec['output'])).astype(int)
        self.assertGreater(out[y, x].min(), 80)
        far = np.ones(out.shape[:2], bool)
        far[y - 12:y + 13, x - 12:x + 13] = False
        self.assertTrue((out[far] == plain[far]).all())          # nothing else moved
        step = rec['steps'][-1]
        self.assertEqual((step['step'], step['spots']), ('spots', [[x, y, 9]]))
        self.assertGreater(step['pixels_filled'], 200)

    def test_spots_must_be_inside_the_copy_and_for_one_photo(self):
        src = self.write(scene())
        with self.assertRaisesRegex(clean.CleanError, 'inside'):
            clean.clean(src, protect_faces=False, crop=False, spots=[(5000, 10)])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.main(['clean', str(src), '--spot', '10'])
        r = self.root / 'roll-03'
        roll.create(r)
        with contextlib.redirect_stderr(err):
            self.assertNotEqual(cli.main(['clean', str(r), '--spot', '10,10']), 0)


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
class ThinImages(unittest.TestCase):
    """Nothing is enlarged without limit on a strip a few pixels wide or high."""
    def test_a_thin_strip_isnt_enlarged_to_look_for_a_date_stamp(self):
        """A 200 x 12 image became a 43-megapixel copy for the search: 700 MB with the work on it."""
        with mock.patch.object(restore, 'faint_stamp_at') as search:
            for shape in ((12, 200, 3), (500, 1, 3)):
                mask = restore.faint_stamp(np.full(shape, 120, np.uint8))
                self.assertEqual((mask.shape, mask.dtype, bool(mask.any())), (shape[:2], np.dtype(bool), False))
        search.assert_not_called()
        with mock.patch.object(restore, 'faint_stamp_at', wraps=restore.faint_stamp_at) as search:
            restore.faint_stamp(scene(w=640, h=480))              # an ordinary small photo is still searched
            restore.faint_stamp(scene(w=720, h=240))              # and a panorama
        self.assertEqual([c.args[0].shape[:2] for c in search.call_args_list], [(1600, 2133), (1600, 4800)])

    def test_a_thin_strip_is_cleaned_like_any_photo(self):
        with tempfile.TemporaryDirectory() as d:
            for name, (h, w) in (('wide.png', (12, 200)), ('tall.png', (500, 1))):
                Image.fromarray(scene(w=w, h=h)).save(Path(d, name))
                rec = clean.clean(Path(d, name), protect_faces=False, color=True)
                self.assertEqual(rec['source_dimensions'], [w, h])
                self.assertTrue(Path(rec['output']).exists())

    def test_the_color_cast_is_measured_on_a_copy_of_bounded_size(self):
        with mock.patch.object(restore.cv2, 'resize', wraps=restore.cv2.resize) as resize:
            for shape in ((12, 5000, 3), (500, 1, 3), (600, 900, 3), (900, 600, 3)):   # the last two: as ever
                a, b = restore.color_cast(np.full(shape, 120, np.uint8))
                self.assertTrue(abs(a) < 3 and abs(b) < 3, shape)
        self.assertEqual([c.args[1] for c in resize.call_args_list], [(400, 1), (400, 3200), (400, 266), (400, 600)])


def rgb_profile(primaries):
    """A color profile with these primaries (x, y, Y of red, green, blue) and sRGB's white point and curve."""
    import imagecodecs
    x = np.linspace(0, 1, 1024)
    curve = np.rint(np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4) * 65535).astype(np.uint16)
    return imagecodecs.cms_profile('rgb', whitepoint=(0.3127, 0.3290, 1.0), primaries=primaries, transferfunction=curve)


def display_p3():
    return rgb_profile((0.680, 0.320, 1.0, 0.265, 0.690, 1.0, 0.150, 0.060, 1.0))


# Three Display P3 colors and what they are in sRGB (test_pixels checks the conversion itself).
IN_P3, IN_SRGB = [(200, 100, 50), (40, 180, 90), (10, 20, 200)], [(215, 93, 31), (0, 183, 78), (7, 20, 208)]


def patches(colors, side=100):
    """The colors as squares side by side."""
    return np.repeat(np.repeat(np.array([colors], np.uint8), side, 0), side, 1)


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
class AsItsFileSays(unittest.TestCase):
    """A photo is cleaned the way its file says to show it, turned by its orientation tag and in
    sRGB: a phone's photo, say (the kit's own conversions are upright sRGB already)."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def clean(self, src):
        """Only read and written, so the copy shows how the photo was read."""
        return clean.clean(src, dust=False, crop=False, protect_faces=False)

    def test_a_photo_stored_on_its_side_is_cleaned_upright(self):
        img = scene(w=300, h=200)
        exif = Image.Exif()
        exif[0x0112] = 6                                          # orientation: show it a quarter turn clockwise
        for name in ('phone.png', 'phone.jpg'):
            src = self.root / name
            Image.fromarray(img).save(src, exif=exif, quality=98, subsampling=0)
            rec = self.clean(src)
            with Image.open(rec['output']) as im:
                self.assertIsNone(im.getexif().get(0x0112))       # no tag left to turn it again
                out = np.asarray(im).astype(int)
            self.assertEqual(out.shape, (300, 200, 3))
            self.assertLess(np.abs(out - np.rot90(img, -1)).mean(), 0.01 if name.endswith('.png') else 5)   # (JPEG noise)
            self.assertEqual((rec['source_dimensions'], rec['steps']), ([200, 300], [{'step': 'upright', 'orientation': 6}]))

    def test_a_photo_in_another_color_space_is_converted_to_srgb(self):
        from openfilmkit.films.convert import srgb_icc
        src = self.root / 'phone.png'
        Image.fromarray(patches(IN_P3)).save(src, icc_profile=display_p3())
        rec = self.clean(src)
        with Image.open(rec['output']) as im:
            self.assertEqual(im.info['icc_profile'], srgb_icc())  # the copy says sRGB, and now it is
            out = np.asarray(im).astype(int)
        self.assertLessEqual(np.abs(out - patches(IN_SRGB)).max(), 1)
        (step,) = rec['steps']
        self.assertEqual((step['step'], step['from']), ('to_srgb', 'RGB built-in'))   # LittleCMS's name for a profile it made
        self.assertIn('LittleCMS', step['method'])

    def test_an_srgb_photo_keeps_its_pixels_whatever_its_profile_is_called(self):
        from openfilmkit.films.convert import srgb_icc
        img = scene(w=300, h=200)
        alike = rgb_profile((0.64, 0.33, 1.0, 0.30, 0.60, 1.0, 0.15, 0.06, 1.0))      # sRGB, written out another way
        self.assertNotEqual(alike, srgb_icc())
        for n, icc in enumerate((None, srgb_icc(), alike)):
            src = self.root / f'photo-{n}.png'
            Image.fromarray(img).save(src, icc_profile=icc)
            rec = self.clean(src)
            self.assertEqual(rec['steps'], [])
            with Image.open(rec['output']) as im:
                self.assertTrue(np.array_equal(np.asarray(im), img))

    def test_a_gray_photo_is_converted_by_its_gray_profile(self):
        import imagecodecs
        src = self.root / 'gray.png'
        Image.fromarray(np.full((200, 300), 70, np.uint8)).save(src, icc_profile=imagecodecs.cms_profile('gray', gamma=1.8))
        rec = self.clean(src)
        with Image.open(rec['output']) as im:
            out = np.asarray(im)
        self.assertEqual(out.shape, (200, 300, 3))
        self.assertTrue((out == 88).all())                        # 70 at gamma 1.8 is lighter in sRGB
        self.assertEqual([s['step'] for s in rec['steps']], ['to_srgb'])

    def test_a_profile_that_cant_be_used_is_refused(self):
        import imagecodecs
        for n, icc in enumerate((b'not a profile at all' * 20, imagecodecs.cms_profile('lab4'))):
            src = self.root / f'odd-{n}.png'
            Image.fromarray(scene(w=300, h=200)).save(src, icc_profile=icc)
            with self.assertRaisesRegex(clean.CleanError, 'color profile'):
                self.clean(src)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['odd-0.png', 'odd-1.png'])


def scene16(w=900, h=600, seed=3):
    """scene() at 16 bits, with the smooth gradient 8 bits can't hold."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[:h, :w]
    base = np.stack([90 + 60 * x / w, 100 + 40 * y / h, 120 - 30 * x / w], 2) * 257
    return np.clip(base + rng.normal(0, 3 * 257, base.shape), 0, 65535).astype(np.uint16)


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
class Deep(unittest.TestCase):
    """16-bit TIFFs (convert --tiff) are cleaned at full depth."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, img, name='frame-v1.tif', icc=True):
        import tifffile
        from openfilmkit.films.convert import srgb_icc
        path = self.root / name
        tifffile.imwrite(path, img, photometric='rgb', iccprofile=srgb_icc() if icc else None)
        return path

    def test_dust_is_filled_at_16_bits_and_the_rest_left_exactly_alone(self):
        import tifffile
        img = scene16()
        for x, y in [(150, 200), (420, 330), (700, 120)]:
            img[y:y + 3, x:x + 3] = 65535
        rec = clean.clean(self.write(img), protect_faces=False, crop=False)
        out = tifffile.imread(rec['output'])
        self.assertEqual(out.dtype, np.uint16)
        self.assertEqual(rec['bits_per_channel'], {'source': 16, 'output': 16})
        for x, y in [(150, 200), (420, 330), (700, 120)]:
            self.assertLess(int(out[y + 1, x + 1].max()), 200 * 257)
        far = np.ones(img.shape[:2], bool)
        for x, y in [(150, 200), (420, 330), (700, 120)]:
            far[y - 15:y + 18, x - 15:x + 18] = False
        self.assertTrue((out[far] == img[far]).all())
        self.assertGreater(len(np.unique(out[..., 0])), 2000)    # still 16-bit detail, not 8-bit steps

    def test_every_step_decides_as_it_would_at_8_bits(self):
        import tifffile
        img = scene16()
        img[:, :30] = 65535                                       # holder light to crop
        opts = dict(protect_faces=False, color=True, straighten=2.0, lift_shadows=0.85)
        deep = clean.clean(self.write(img), **opts)
        flat = clean.clean(self.write(restore.to8(img), name='frame-v1.png', icc=False), **opts)
        a = restore.to8(tifffile.imread(deep['output'])).astype(int)
        b = np.asarray(Image.open(flat['output'])).astype(int)
        self.assertEqual(a.shape, b.shape)                        # same crop and straightening
        self.assertLess(np.abs(a - b).mean(), 1.0)
        self.assertEqual([s['step'] for s in deep['steps']], [s['step'] for s in flat['steps']])

    def test_a_16_bit_file_without_a_profile_is_a_master_and_is_refused(self):
        with self.assertRaisesRegex(clean.CleanError, 'master'):
            clean.clean(self.write(scene16(), icc=False))

    def test_a_16_bit_tiff_is_turned_and_converted_to_srgb_at_16_bits(self):
        """Another program's TIFF (SilverFast's, say) comes with whatever profile it was given."""
        import tifffile
        from openfilmkit import pixels
        img = scene16(w=300, h=200)
        src = self.root / 'other.tif'
        tifffile.imwrite(src, img, photometric='rgb', iccprofile=display_p3(),
                         extratags=[(274, 'H', 1, 8, True)])      # orientation: show it a quarter turn back
        rec = clean.clean(src, dust=False, crop=False, protect_faces=False)
        out = tifffile.imread(rec['output'])
        turned = np.ascontiguousarray(np.rot90(img))
        self.assertEqual((out.dtype, out.shape), (np.uint16, (300, 200, 3)))
        self.assertTrue(np.array_equal(out, pixels.to_srgb(turned.copy(), display_p3())[0]))
        self.assertFalse(np.array_equal(out, turned))
        self.assertGreater(len(np.unique(out[..., 0])), 2000)     # still 16-bit detail, not 8-bit steps
        self.assertEqual([s['step'] for s in rec['steps']], ['to_srgb', 'upright'])
        self.assertEqual(rec['bits_per_channel'], {'source': 16, 'output': 16})

    def test_a_16_bit_tiff_can_also_give_a_jpeg(self):
        rec = clean.clean(self.write(scene16()), self.root / 'web.jpg', protect_faces=False)
        with Image.open(rec['output']) as im:
            self.assertEqual(im.mode, 'RGB')
        self.assertEqual(rec['bits_per_channel'], {'source': 16, 'output': 8})

    def test_roll_includes_tiff_conversions(self):
        r = self.root / 'roll-02'
        roll.create(r)
        (r / 'derived').mkdir()
        self.root = r / 'derived'
        self.write(scene16(), 'frame-01-v1.tif')
        self.assertEqual([p.name for p in clean.roll_sources(r)], ['frame-01-v1.tif'])


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
class InPieces(unittest.TestCase):
    """Big scans are worked on in strips and windows; the pixels must come out as on the whole image."""
    def holes(self, shape, seed=4):
        rng = np.random.default_rng(seed)
        m = np.zeros(shape[:2], np.uint8)
        for _ in range(40):                                       # specks, some close together
            x, y = rng.integers(0, shape[1] - 4), rng.integers(0, shape[0] - 4)
            m[y:y + rng.integers(1, 5), x:x + rng.integers(1, 5)] = 1
        m[300:302, 50:800] = 1                                    # a long scratch
        m[0:3, 10:40] = 1                                         # on the edge
        m[-2:, -30:] = 1
        return m

    def test_inpainting_window_by_window_equals_the_whole_image(self):
        import cv2
        for img in (scene(), scene16()):
            mask = self.holes(img.shape)
            for radius in (3, 9):
                if img.dtype == np.uint8:
                    whole = cv2.inpaint(img, mask * 255, radius, cv2.INPAINT_TELEA)
                else:
                    whole = np.dstack([cv2.inpaint(np.ascontiguousarray(img[..., c]), mask * 255, radius,
                                                   cv2.INPAINT_TELEA) for c in range(3)])
                self.assertTrue((restore.inpaint(img, mask, radius) == whole).all(), (img.dtype, radius))

    def test_strips_equal_the_whole_image(self):
        from unittest import mock
        img = scene16()
        with mock.patch.object(restore, 'BAND', 10 ** 6):
            whole = [restore.fix_color(img)[0], restore.lift_shadows(img, 0.8), restore.fix_color(scene())[0]]
        with mock.patch.object(restore, 'BAND', 64):
            strips = [restore.fix_color(img)[0], restore.lift_shadows(img, 0.8), restore.fix_color(scene())[0]]
            self.assertTrue((restore.to8(img) == np.rint(img / 257.0)).all())
        for a, b in zip(whole, strips):
            self.assertTrue((a == b).all())

if __name__ == '__main__':
    unittest.main()
