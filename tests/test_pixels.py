"""A photo's pixels the way its file says to show them: turned by its orientation tag, in sRGB.

Turning is checked against Pillow's own (ImageOps.exif_transpose), color against the published
Display P3 to sRGB matrix, so neither is checked against itself.
"""
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import imagecodecs
import numpy as np
import tifffile
from PIL import Image, ImageOps

from openfilmkit import pixels
from openfilmkit.films.convert import srgb_icc

D65 = (0.3127, 0.3290, 1.0)
REC709 = (0.64, 0.33, 1.0, 0.30, 0.60, 1.0, 0.15, 0.06, 1.0)     # sRGB's primaries
P3 = (0.680, 0.320, 1.0, 0.265, 0.690, 1.0, 0.150, 0.060, 1.0)
APPLE = Path('/System/Library/ColorSync/Profiles')
# Display P3 to sRGB in linear light (both have a D65 white), as published.
P3_TO_SRGB = np.array([[1.2249, -0.2247, 0.0], [-0.0420, 1.0419, 0.0], [-0.0197, -0.0786, 1.0979]])


def srgb_curve():
    x = np.linspace(0, 1, 1024)
    return np.rint(np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4) * 65535).astype(np.uint16)


def display_p3():
    """A profile like Display P3: its primaries, with sRGB's white point and curve."""
    return imagecodecs.cms_profile('rgb', whitepoint=D65, primaries=P3, transferfunction=srgb_curve())


def encoded(linear, top=255):
    linear = np.clip(linear, 0, 1)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055) * top


def by_matrix(p3, top=255):
    """Display P3 values in sRGB (as floats, on the same scale), colors outside sRGB clipped."""
    v = p3 / top
    return encoded(np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4) @ P3_TO_SRGB.T, top)


def noise(shape=(50, 40, 3), dtype=np.uint8, seed=1):
    return np.random.default_rng(seed).integers(0, np.iinfo(dtype).max + 1, shape, dtype=dtype)


class Upright(unittest.TestCase):
    def test_each_orientation_turns_as_pillow_turns_it(self):
        a = noise((4, 6, 3))
        for orientation in range(1, 9):
            exif = Image.Exif()
            exif[pixels.ORIENTATION] = orientation
            held = io.BytesIO()
            Image.fromarray(a).save(held, 'PNG', exif=exif)
            with Image.open(held) as im:
                self.assertEqual(pixels.tags(im), (orientation, None))
                theirs = np.asarray(ImageOps.exif_transpose(im))
            self.assertTrue(np.array_equal(pixels.upright(a, orientation), theirs), orientation)

    def test_an_upright_photo_or_a_tag_that_says_nothing_is_left_as_it_is(self):
        a = noise((4, 6, 3))
        for orientation in (1, 0, 9, None, 'sideways'):
            self.assertIs(pixels.upright(a, orientation), a)

    def test_a_tiff_says_it_with_its_own_tags(self):
        held = io.BytesIO()
        tifffile.imwrite(held, noise((4, 6, 3), np.uint16), photometric='rgb', iccprofile=display_p3(),
                         extratags=[(pixels.ORIENTATION, 'H', 1, 8, True)])
        held.seek(0)
        with tifffile.TiffFile(held) as tif:
            self.assertEqual(pixels.tiff_tags(tif.pages[0]), (8, display_p3()))
        held = io.BytesIO()
        tifffile.imwrite(held, noise((4, 6, 3)), photometric='rgb')
        held.seek(0)
        with tifffile.TiffFile(held) as tif:
            self.assertEqual(pixels.tiff_tags(tif.pages[0]), (1, None))


class ToSRGB(unittest.TestCase):
    def test_display_p3_colors_come_out_as_the_published_matrix_gives_them(self):
        colors = np.array([[[200, 100, 50], [40, 180, 90], [128, 128, 128], [255, 0, 0], [10, 20, 200], [250, 240, 30]]],
                          np.uint8)
        out, name = pixels.to_srgb(colors.copy(), display_p3())
        self.assertEqual(name, 'RGB built-in')                    # what LittleCMS calls a profile it made
        self.assertLess(np.abs(out - by_matrix(colors)).max(), 1)
        every = noise()
        self.assertLess(np.abs(pixels.to_srgb(every.copy(), display_p3())[0] - by_matrix(every)).max(), 1)

    def test_16_bits_are_computed_pixel_by_pixel(self):
        """LittleCMS's quicker way looks colors up in a table it fills first: at 16 bits that was up
        to 19 8-bit levels off where colors clip at the edge of sRGB."""
        a = noise((64, 64, 3), np.uint16)
        out, _ = pixels.to_srgb(a.copy(), display_p3())
        self.assertEqual(out.dtype, np.uint16)
        self.assertLess(np.abs(out - by_matrix(a, 65535)).max(), 257)    # within one 8-bit level

    def test_the_quick_way_is_within_a_level_at_8_bits(self):
        a = noise()
        exact, _ = pixels.to_srgb(a.copy(), display_p3())
        quick, _ = pixels.to_srgb(a.copy(), display_p3(), exact=False)
        self.assertLessEqual(np.abs(exact.astype(int) - quick).max(), 1)

    def test_a_photo_is_converted_in_place_a_strip_at_a_time(self):
        a = noise()
        whole, _ = pixels.to_srgb(a.copy(), display_p3())
        b = a.copy()
        with mock.patch.object(pixels, 'STRIP', 7 * a.shape[1]):         # seven rows at a time
            out, _ = pixels.to_srgb(b, display_p3())
        self.assertIs(out, b)
        self.assertTrue(np.array_equal(out, whole))
        frozen = a.copy()                                                # as Pillow hands pixels out
        frozen.flags.writeable = False
        out, _ = pixels.to_srgb(frozen, display_p3())
        self.assertTrue(np.array_equal(out, whole) and np.array_equal(frozen, a))

    def test_srgb_under_another_name_is_left_exactly_alone(self):
        a = noise()
        alike = imagecodecs.cms_profile('rgb', whitepoint=D65, primaries=REC709, transferfunction=srgb_curve())
        self.assertNotEqual(alike, srgb_icc())
        for icc in (None, b'', srgb_icc(), alike):
            out, name = pixels.to_srgb(a, icc)
            self.assertIs(out, a)
            self.assertIsNone(name)
        # The same primaries with a plain 2.2 gamma are not sRGB: the shadows sit levels apart.
        out, name = pixels.to_srgb(a.copy(), imagecodecs.cms_profile('rgb', whitepoint=D65, primaries=REC709, gamma=2.2))
        self.assertIsNotNone(name)
        self.assertGreater(np.abs(out.astype(int) - a).max(), 3)

    @unittest.skipUnless(APPLE.is_dir(), "needs the color profiles macOS comes with")
    def test_the_profiles_macos_comes_with(self):
        a = noise()
        self.assertEqual(pixels.profile_name((APPLE / 'sRGB Profile.icc').read_bytes()), 'sRGB IEC61966-2.1')
        out, name = pixels.to_srgb(a, (APPLE / 'sRGB Profile.icc').read_bytes())
        self.assertIs(out, a)
        self.assertIsNone(name)
        out, name = pixels.to_srgb(a.copy(), (APPLE / 'Display P3.icc').read_bytes())
        self.assertEqual(name, 'Display P3')
        self.assertLess(np.abs(out - by_matrix(a)).max(), 1)

    def test_gray_comes_back_as_rgb(self):
        ramp = np.arange(256, dtype=np.uint8).reshape(16, 16)
        out, name = pixels.to_srgb(ramp, None)
        self.assertEqual((out.shape, name), ((16, 16, 3), None))
        self.assertTrue((out == ramp[..., None]).all())
        out, name = pixels.to_srgb(ramp, imagecodecs.cms_profile('gray', gamma=1.8))     # by its gray profile
        self.assertEqual((out.shape, name is None), ((16, 16, 3), False))
        self.assertLess(np.abs(out - encoded((ramp / 255) ** 1.8)[..., None]).max(), 1)
        self.assertEqual(int(out[4, 6, 0]), 88)                                          # 70, lighter in sRGB
        out, name = pixels.to_srgb(ramp, display_p3())                # an RGB profile: gray is R = G = B in it
        self.assertEqual(name, 'RGB built-in')
        self.assertLessEqual(np.abs(out.astype(int) - ramp[..., None]).max(), 1)         # P3's grays are sRGB's

    def test_a_profile_that_cant_be_used_is_refused(self):
        a = noise((4, 4, 3))
        for icc in (b'not a profile at all' * 20, imagecodecs.cms_profile('lab4'), imagecodecs.cms_profile('gray', gamma=1.8)):
            with self.assertRaisesRegex(pixels.ProfileError, 'color profile'):
                pixels.to_srgb(a.copy(), icc)


if __name__ == '__main__':
    unittest.main()
