"""The scratch pass's decisions (ported with the archive pipeline's tests) and the model download.

The models themselves are not needed: the mask and fill logic take a probability map and a
`run` function, and fill_window is checked with a stand-in network.
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import cv2
    import numpy as np
    from openfilmkit.clean import scratches as sc
except ImportError:
    sc = None
from openfilmkit import models

SKIN = (200, 150, 125)


def skin(h=1200, w=1200, color=SKIN, noise=2, seed=2):
    rng = np.random.default_rng(seed)
    img = np.ones((h, w, 3), np.float32) * np.array(color, np.float32)
    return np.clip(img + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)


def face(x0=300, y0=300, size=600):
    """A face box with eyes at the usual landmark positions."""
    return {'box': [x0, y0, x0 + size, y0 + size],
            'kps': [[x0 + 0.33 * size, y0 + 0.4 * size], [x0 + 0.67 * size, y0 + 0.4 * size],
                    [x0 + 0.5 * size, y0 + 0.55 * size], [x0 + 0.38 * size, y0 + 0.75 * size],
                    [x0 + 0.62 * size, y0 + 0.75 * size]]}


@unittest.skipIf(sc is None, 'needs OpenCV, numpy and Pillow')
class Mask(unittest.TestCase):
    def test_catchlight_is_the_spot_in_the_iris_not_the_white_of_the_eye(self):
        img = skin()
        f = face()
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        ex, ey = map(int, f['kps'][0])
        cv2.ellipse(gray, (ex, ey), (45, 18), 0, 0, 360, 245, -1)   # white of the eye, brightest
        cv2.circle(gray, (ex, ey), 16, 40, -1)                       # dark iris
        cv2.circle(gray, (ex + 5, ey - 4), 3, 210, -1)               # catchlight
        catch, irises = sc.eye_masks(gray, [f])
        self.assertTrue(catch[ey - 4, ex + 5] and irises[ey, ex])
        self.assertFalse(catch[ey, ex + 40])                         # the white is not protected

    def test_isolated_fleck_on_skin_is_found_but_whiskers_and_hair_are_not(self):
        f = face()
        img = skin()
        cv2.circle(img, (450, 640), 6, (255, 225, 205), -1)          # one pale fleck on the cheek
        none = np.zeros(img.shape[:2], bool)
        self.assertTrue(sc.face_flecks(img, [f], none)[640, 450])
        stubble = skin()
        rng = np.random.default_rng(5)
        for _ in range(80):                                          # a crowd of pale whiskers
            x, y = rng.integers(500, 700), rng.integers(700, 820)
            cv2.circle(stubble, (int(x), int(y)), 3, (240, 220, 210), -1)
        self.assertEqual(sc.face_flecks(stubble, [f], none)[700:820, 500:700].sum(), 0)
        hair = skin(color=(60, 45, 40))                              # the same fleck on dark hair
        cv2.circle(hair, (450, 640), 6, (200, 190, 180), -1)
        self.assertFalse(sc.face_flecks(hair, [f], none)[640, 450])

    def test_thick_dark_detection_is_scene_but_bright_one_is_damage(self):
        gray = np.full((400, 400), 120, np.float32)
        mask = np.zeros((400, 400), np.uint8)
        cv2.circle(gray, (100, 100), 20, 40, -1)
        cv2.circle(mask, (100, 100), 20, 1, -1)       # dark trench
        cv2.circle(gray, (300, 300), 20, 230, -1)
        cv2.circle(mask, (300, 300), 20, 1, -1)       # bright blotch
        self.assertEqual(sc.drop_dark_marks(mask, gray, grow=1), 1)
        self.assertTrue(mask[100, 100] == 0 and mask[300, 300] == 1)

    def test_faces_use_a_lower_threshold_and_the_stamp_and_edge_are_never_touched(self):
        img = skin(2000, 2000)
        f = face(800, 800, 400)
        prob = np.zeros((2000, 2000), np.float32)
        prob[900:903, 850:950] = 0.2          # weak line inside the face
        prob[200:203, 200:300] = 0.2          # the same weak line in the background
        prob[1500:1503, 200:300] = 0.9        # strong line in the background, but under the stamp
        prob[0:3, 500:600] = 0.9              # along the scan's edge
        protect = np.zeros((2000, 2000), bool)
        protect[1490:1515, 190:310] = True
        mask, _ = sc.build_mask(img, prob, protect, [f])
        self.assertTrue(mask[901, 900] and not mask[201, 250])
        self.assertTrue(not mask[1501, 250] and not mask[1, 550])

    def test_long_lines_outside_faces_can_be_spared_while_specks_and_faces_are_still_cleaned(self):
        mask = np.zeros((1000, 1000), np.uint8)
        mask[100:103, 50:900] = 1             # grout line across the floor
        mask[500:504, 480:510] = 1            # short scratch: below the length limit
        mask[300:303, 600:950] = 1            # long line, but it reaches into a face
        faces_area = np.zeros((1000, 1000), bool)
        faces_area[250:400, 850:1000] = True
        self.assertEqual(sc.drop_long_lines(mask, faces_area, mask.shape), 1)
        self.assertTrue(mask[101, 400] == 0 and mask[502, 495] == 1 and mask[301, 700] == 1)

    def test_a_partly_detected_white_blob_is_covered_whole(self):
        gray = np.full((600, 600), 40, np.uint8)
        cv2.ellipse(gray, (300, 300), (60, 25), 30, 0, 360, 255, -1)    # debris, printed pure white
        mask = np.zeros((600, 600), np.uint8)
        mask[290:310, 300:340] = 1                                       # the model marked part of it
        self.assertEqual(sc.complete_bright_blobs(mask, gray, max_area_share=0.05), 1)
        self.assertTrue(mask[gray == 255].all() and mask[100, 100] == 0)

    def test_white_blob_on_busy_or_colored_ground_is_not_completed(self):
        mask = np.zeros((600, 600), np.uint8)
        mask[290:310, 300:340] = 1
        for ground, blob, expected in (('busy', (255, 255, 255), 0), ('plain', (230, 150, 120), 0),
                                       ('plain', (250, 250, 250), 1)):
            rgb = np.full((600, 600, 3), 40, np.uint8)
            if ground == 'busy':
                rgb[:, ::6] = 120
            cv2.ellipse(rgb, (300, 300), (60, 25), 30, 0, 360, blob, -1)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            self.assertEqual(sc.complete_bright_blobs(mask.copy(), gray, max_area_share=0.05, rgb=rgb), expected,
                             (ground, blob))


@unittest.skipIf(sc is None, 'needs OpenCV, numpy and Pillow')
class Fill(unittest.TestCase):
    def test_fill_finishes_damage_longer_than_one_window_and_fills_every_hole(self):
        img = skin(800, 2400)
        mask = np.zeros((800, 2400), np.uint8)
        mask[400:404, 100:2300] = 1           # a scratch four windows long
        mask[100:110, 100:110] = 1

        def run(crop, keep):
            self.assertEqual(crop.shape[:2], keep.shape)
            return np.zeros_like(crop)

        res, calls = sc.fill_damage(img, mask, run)
        self.assertTrue((res[mask > 0] == 0).all() and (res[mask == 0] == img[mask == 0]).all())
        self.assertTrue(4 <= calls <= 20)

    def test_spot_invented_by_the_fill_is_evened_out(self):
        img = skin(400, 400)
        mask = np.zeros((400, 400), np.uint8)
        mask[180:220, 180:220] = 1
        modeled = img.copy()
        modeled[195:201, 195:201] = 20         # a dark dot the model made up
        res, stats = sc.settle_fill(modeled, img.copy(), mask, grow=1)
        self.assertEqual(stats['invented_replaced'], 1)
        self.assertLess(abs(int(res[198, 198].mean()) - int(img[198, 198].mean())), 10)

    def test_only_too_smooth_fills_get_grain_and_no_more_than_their_surroundings(self):
        rng = np.random.default_rng(3)
        img = np.clip(120 + rng.normal(0, 4, (600, 600, 3)), 0, 255).astype(np.uint8)
        mask = np.zeros((600, 600), np.uint8)
        mask[100:160, 100:200] = 1          # fill left flat (too smooth)
        mask[400:460, 400:500] = 1          # fill that already has grain
        img[100:160, 100:200] = 120
        out, n = sc.match_grain(img, mask)
        self.assertEqual(n, 1)
        self.assertTrue(2.0 < out[105:155, 105:195].astype(float).std() < 5.0)
        self.assertTrue((out[400:460, 400:500] == img[400:460, 400:500]).all())

    def test_window_keeps_known_pixels_and_takes_the_model_in_holes(self):
        class Net:   # stands in for MI-GAN: paints everything mid-gray (0 in its -1..1 range)
            def setInput(self, x):
                self.shape = x.shape

            def forward(self):
                assert self.shape == (1, 4, 512, 512)
                return np.zeros((1, 3, 512, 512), np.float32)

        crop = np.full((300, 400, 3), 200, np.uint8)       # a window smaller than 512 (photo edge)
        keep = np.full((300, 400), 255, np.uint8)
        keep[100:140, 150:250] = 0
        out = sc.fill_window(Net())(crop, keep)
        self.assertEqual(out.shape, crop.shape)
        self.assertTrue(abs(int(out[120, 200, 0]) - 128) <= 1)          # hole: the model's gray
        self.assertTrue((out[:80] == 200).all() and (out[160:] == 200).all())   # away from it: untouched


class Download(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.server, self.dest = root / 'release', root / 'models'
        self.server.mkdir()
        (self.server / 'tiny.onnx').write_bytes(b'not really a model')
        for lic in models.LICENSES:
            (self.server / lic).write_text('MIT')
        self.good = hashlib.sha256(b'not really a model').hexdigest()
        self.env = mock.patch.dict(os.environ, {'OPENFILMKIT_MODELS': str(self.dest)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def patched(self, sha):
        table = {'tiny': {'file': 'tiny.onnx', 'bytes': 18, 'sha256': sha, 'source': 'test'}}
        return (mock.patch.object(models, 'MODELS', table),
                mock.patch.object(models, 'RELEASE', self.server.as_uri() + '/'))

    def test_model_is_fetched_once_and_checked(self):
        a, b = self.patched(self.good)
        with a, b:
            self.assertFalse(models.status()['tiny']['present'])
            path = models.path('tiny')
            self.assertEqual(path.read_bytes(), b'not really a model')
            self.assertTrue((self.dest / models.LICENSES[0]).is_file())
            (self.server / 'tiny.onnx').unlink()        # already here: no second download
            self.assertEqual(models.path('tiny'), path)

    def test_a_file_that_fails_its_checksum_is_not_kept(self):
        a, b = self.patched('0' * 64)
        with a, b:
            with self.assertRaises(models.ModelError):
                models.path('tiny')
        self.assertEqual([p.name for p in self.dest.glob('tiny*')], [])


if __name__ == '__main__':
    unittest.main()
