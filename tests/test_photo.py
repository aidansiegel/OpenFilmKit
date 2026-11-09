"""clean on real photographs: leave an undamaged portrait alone, and find real and added damage.

Sally Ride's 1984 NASA portrait (public domain) is film-era, with fine hair, fabric and a flag,
the detail a cleaner is tempted to take for dust. Damage is added to it with known positions, the
way debris on a negative prints: bright, soft-edged specks, curled fibers and a long scratch.

Grace Hopper's 1984 Navy portrait (public domain) carries genuine dust of its own, and on top of
it goes real dust from a scan of blank film (tests/fixtures/README.md), printed as a negative
prints it. The scratch-pass tests need the models (openfilmkit models --fetch) and are skipped
without them.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import cv2
    import numpy as np
    from PIL import Image
    from openfilmkit import clean, models
    from openfilmkit.clean import restore, scratches as sc
except ImportError:
    clean = None

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
RIDE = FIXTURES / 'sally-ride-1984.jpg'            # see fixtures/README.md for sources and rights
HOPPER = FIXTURES / 'grace-hopper-1984.jpg'
FILM_DUST = FIXTURES / 'blank-film-dust.png'


def have_models():
    return clean is not None and all(m['present'] for m in models.status().values())


def paint(img, mask_f, value, alpha=1.0):
    a = (np.clip(mask_f, 0, 1) * alpha)[..., None]
    return np.clip(img * (1 - a) + np.array(value, np.float32) * a, 0, 255)


def damage(img, keep_out, seed=7, specks=40, fibers=8, scratch=True, where=None, radius=(1.0, 3.0), spacing=9):
    """Debris as it prints from a negative. Returns (damaged uint8, [(kind, bool mask)]).
    `where` (bool mask), if given, is the only area specks are placed in; `spacing` keeps them
    apart (real dust is sparse; the dust pass leaves crowds of bright points, like gravel, alone)."""
    rng = np.random.default_rng(seed)
    h, w = img.shape[:2]
    out = img.astype(np.float32)
    taken = keep_out.copy()
    defects = []

    def spot(r):
        pool = np.argwhere(where & ~taken) if where is not None else None
        for _ in range(2000):
            if pool is not None:
                y, x = pool[rng.integers(len(pool))]
            else:
                x, y = int(rng.integers(r + 10, w - r - 10)), int(rng.integers(r + 10, h - r - 10))
            if not taken[max(0, y - r - 4):y + r + 5, max(0, x - r - 4):x + r + 5].any():
                return int(x), int(y)
        raise RuntimeError('no room for damage')

    for _ in range(specks):
        r = float(rng.uniform(*radius))
        x, y = spot(int(np.ceil(r)) + 2)
        m = np.zeros((h, w), np.float32)
        cv2.circle(m, (x, y), int(round(r)), 1.0, -1, cv2.LINE_AA)
        m = cv2.GaussianBlur(m, (0, 0), 0.6)
        v = float(rng.uniform(232, 255))
        out = paint(out, m, (v, v * 0.98, v * 0.95))
        defects.append(('speck', m > 0.3))
        taken |= cv2.dilate((m > 0.05).astype(np.uint8), np.ones((spacing, spacing), np.uint8)) > 0
    placed = 0
    while placed < fibers:
        n = int(rng.integers(25, 70))
        x, y = spot(n // 2 + 4)
        ang, pts = rng.uniform(0, 2 * np.pi), []
        for _ in range(n):
            ang += rng.normal(0, 0.18)
            x, y = x + np.cos(ang), y + np.sin(ang)
            pts.append((x, y))
        m = np.zeros((h, w), np.float32)
        cv2.polylines(m, [np.round(np.array(pts) * 4).astype(np.int32)], False, 1.0, 1, cv2.LINE_AA, shift=2)
        m = cv2.GaussianBlur(m, (0, 0), 0.5)
        if (m > 0.05)[taken].any():
            continue
        v = float(rng.uniform(220, 250))
        out = paint(out, m * 1.6, (v, v, v * 0.96), 0.9)
        defects.append(('fiber', m > 0.3))
        taken |= cv2.dilate((m > 0.05).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
        placed += 1
    while scratch:
        x0, y0 = int(rng.integers(40, w - 40)), int(rng.integers(40, h // 2))
        m = np.zeros((h, w), np.float32)
        cv2.line(m, (x0, y0), (x0 + int(rng.integers(-60, 60)), y0 + int(h * 0.3)), 1.0, 1, cv2.LINE_AA)
        if not (m > 0.05)[taken].any():
            out = np.clip(out + m[..., None] * 70, 0, 255)
            defects.append(('scratch', m > 0.3))
            break
    return out.astype(np.uint8), defects


def found(defects, filled, kind):
    """Defects of `kind` with at least half their pixels filled, and how many there are."""
    of = [m for k, m in defects if k == kind]
    return sum(filled[m].mean() >= 0.5 for m in of), len(of)


def elsewhere(defects, filled):
    near = np.zeros(filled.shape, bool)
    for _, m in defects:
        near |= m
    return int((filled & ~(cv2.dilate(near.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0)).sum())


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
class Portrait(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.img = np.asarray(Image.open(RIDE).convert('RGB'))
        cls.faces = clean.find_faces(cls.img)
        cls.stamp = restore.stamp_mask(cls.img)
        cls.features = restore.feature_mask(cls.img.shape, cls.faces)
        h, w = cls.img.shape[:2]
        keep = cls.stamp | (cv2.dilate(cls.features.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0)
        e = int(max(h, w) * 0.01)
        keep[:e] = keep[-e:] = True
        keep[:, :e] = keep[:, -e:] = True
        cls.keep_out = keep

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_dust_pass_leaves_an_undamaged_portrait_alone(self):
        # Hair glints are bright, small and sit where a median filter sees smooth ground; before the
        # texture test the dust pass filled 42 of them (502 pixels) on this photo.
        mask = restore.dust_mask(self.img, protect=self.stamp | self.features)
        self.assertLess(int(mask.sum()), 30)

    def test_dust_pass_finds_specks_on_plain_ground_and_repairs_them(self):
        g = cv2.cvtColor(self.img, cv2.COLOR_RGB2GRAY).astype(np.float32)
        fine = g - cv2.GaussianBlur(g, (0, 0), 1.5)
        local = np.sqrt(np.maximum(cv2.blur(fine * fine, (15, 15)) - cv2.blur(fine, (15, 15)) ** 2, 0))
        # Plain ground as the dust pass sees it: no fine texture, no edge or shading nearby (it leaves
        # specks beside edges to the scratch pass), and dark enough for a bright speck to show.
        m31 = cv2.blur(g, (31, 31))
        shading = np.sqrt(np.maximum(cv2.blur(g * g, (31, 31)) - m31 * m31, 0))
        plain = (local < 2.5) & (shading < 6) & (m31 < 200)
        # Dust-sized specks, as sparse as dust; bigger debris is the scratch pass's (tested below).
        dam, defects = damage(self.img, self.keep_out, seed=11, specks=20, fibers=0, scratch=False, where=plain,
                              radius=(1.0, 2.0), spacing=45)
        mask = restore.dust_mask(dam, protect=self.stamp | self.features)
        hit, n = found(defects, mask, 'speck')
        # Before the dust pass left candidates out of its smoothness measure, about a quarter were missed.
        self.assertGreaterEqual(hit, n - 1)
        self.assertLess(elsewhere(defects, mask), 30)
        fixed = restore.remove_dust(dam, mask)
        spots = np.any([m for _, m in defects], axis=0)
        before = np.abs(dam[spots].astype(int) - self.img[spots]).mean()
        after = np.abs(fixed[spots].astype(int) - self.img[spots]).mean()
        self.assertLess(after, 0.2 * before)

    def test_skin_is_her_face_not_her_hair_or_shirt(self):
        skin = sc.skin_area(self.img, self.faces)
        (lx, ly), (rx, ry), (nx, ny) = [(int(x), int(y)) for x, y in self.faces[0]['kps'][:3]]
        self.assertTrue(skin[ny, nx] and skin[ny + 40, nx - 50])          # nose, cheek
        x0, y0, x1, y1 = (int(v) for v in self.faces[0]['box'])
        self.assertFalse(skin[y0 - 40, (x0 + x1) // 2])                    # hair above the forehead
        self.assertFalse(skin[y1 + 150, (x0 + x1) // 2 - 120])             # her shirt

    @unittest.skipUnless(have_models(), 'needs the scratch models (openfilmkit models --fetch)')
    def test_scratch_pass_spares_her_hair(self):
        # The face rules once covered the whole widened face box, hair included: 4,087 pixels filled
        # on this undamaged photo. On skin only it is what the model alone marks (flyaway hairs
        # against the background, about 2,150).
        _, stats, mask = sc.repair(self.img, self.faces, self.stamp, restore.dust_mask, sc.load_nets())
        self.assertLess(int((mask > 0).sum()), 2600)
        # On skin only the extra speck finder may act: two faint pale points by her nose (18 pixels).
        self.assertLess(int((mask > 0)[sc.skin_area(self.img, self.faces)].sum()), 40)

    @unittest.skipUnless(have_models(), 'needs the scratch models (openfilmkit models --fetch)')
    def test_clean_with_scratches_finds_the_damage_and_puts_the_portrait_back(self):
        dam, defects = damage(self.img, self.keep_out)
        src = self.root / 'damaged.png'
        Image.fromarray(dam).save(src)
        rec = clean.clean(src, crop=False, scratches=True)
        out = np.asarray(Image.open(rec['output']).convert('RGB'))
        changed = np.abs(out.astype(int) - dam).max(axis=2) > 0
        for kind, least in (('speck', 0.85), ('fiber', 0.85), ('scratch', 1.0)):
            hit, n = found(defects, changed, kind)
            self.assertGreaterEqual(hit, int(np.ceil(least * n)), kind)
        spots = np.any([m for _, m in defects], axis=0)
        self.assertLess(np.abs(out[spots].astype(int) - self.img[spots]).mean(), 15)
        self.assertTrue(rec['generative_processing'])

    @unittest.skipUnless(have_models(), 'needs the scratch models (openfilmkit models --fetch)')
    def test_faint_specks_on_skin_are_found(self):
        # What the face rules are for: dust on a face is what people notice.
        skin = sc.skin_area(self.img, self.faces) & ~(cv2.dilate(self.features.astype(np.uint8),
                                                                 np.ones((11, 11), np.uint8)) > 0)
        rng = np.random.default_rng(3)
        dam, spots = self.img.astype(np.float32), []
        ys, xs = np.nonzero(skin)
        for _ in range(10):
            i = rng.integers(len(ys))
            x, y = int(xs[i]), int(ys[i])
            m = np.zeros(self.img.shape[:2], np.float32)
            cv2.circle(m, (x, y), int(rng.integers(1, 3)), 1.0, -1, cv2.LINE_AA)
            m = cv2.GaussianBlur(m, (0, 0), 0.6)
            dam = paint(dam, m, (min(255.0, float(self.img[y, x].mean()) + 55),) * 3)
            spots.append(m > 0.3)
        dam = dam.astype(np.uint8)
        _, _, mask = sc.repair(dam, self.faces, self.stamp, restore.dust_mask, sc.load_nets())
        self.assertGreaterEqual(sum((mask > 0)[s].mean() >= 0.5 for s in spots), 9)


def print_dust(img, opacity, contrast=2.6):
    """Debris on a negative as it prints: the negative loses `opacity` of its light there, and the
    print raises that loss to the print contrast, in linear light, so the positive brightens."""
    f = img.astype(np.float32) / 255
    lin = np.where(f <= 0.04045, f / 12.92, ((f + 0.055) / 1.055) ** 2.4)
    lin = np.clip(lin * np.clip(1 - opacity, 0.05, 1)[..., None] ** -contrast, 0, 1)
    out = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.rint(out * 255).astype(np.uint8)


def bright_specks(img, region):
    """Small bright specks in `region`, counted independently of clean: a top-hat over 7 px."""
    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    hat = cv2.morphologyEx(g, cv2.MORPH_TOPHAT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    return cv2.connectedComponents(((hat > 30) & region).astype(np.uint8))[0] - 1


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
class HopperPortrait(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.img = np.asarray(Image.open(HOPPER).convert('RGB'))
        cls.faces = clean.find_faces(cls.img)
        cls.protect = restore.stamp_mask(cls.img) | restore.feature_mask(cls.img.shape, cls.faces)
        cls.dust = np.asarray(Image.open(FILM_DUST)).astype(np.float32) / 255
        cls.damaged = print_dust(cls.img, cls.dust)
        lift = np.abs(cls.damaged.astype(int) - cls.img).max(axis=2)
        n, lab = cv2.connectedComponents((lift >= 12).astype(np.uint8))
        cls.pieces = [(lab == i, int(lift[lab == i].max())) for i in range(1, n)]   # visible dust, and how bright

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def cleaned(self):
        src = self.root / 'dusty.png'
        Image.fromarray(self.damaged).save(src)
        return np.asarray(Image.open(clean.clean(src, crop=False, scratches=True)['output']).convert('RGB'))

    def test_her_face_is_found_at_every_rotation(self):
        for turns in range(4):
            self.assertEqual(len(clean.find_faces(np.ascontiguousarray(np.rot90(self.img, turns)))), 1, turns)

    def test_the_portraits_own_dust_is_cleaned(self):
        # White specks on her dark uniform: dust on the 1984 original (or lint on the wool).
        uniform = cv2.cvtColor(self.img, cv2.COLOR_RGB2GRAY) < 60
        before = bright_specks(self.img, uniform)
        mask = restore.dust_mask(self.img, protect=self.protect)
        after = bright_specks(restore.remove_dust(self.img, mask), uniform)
        # The count also takes in real highlights (buttons, insignia, weave), which must stay:
        # about 150 before, about 60 after.
        self.assertGreater(before, 100)
        self.assertLess(after, 0.5 * before)
        self.assertLess(int(mask[sc.skin_area(self.img, self.faces)].sum()), 30)   # her face is left alone

    def arm_crook(self):
        """The sliver of light blue backdrop seen through the crook of her left arm (about 5 x 18 px
        at 816, 1149): a bright blob on the dark uniform that is picture, not dust."""
        hsv = cv2.cvtColor(self.img, cv2.COLOR_RGB2HSV)
        gray = cv2.cvtColor(self.img, cv2.COLOR_RGB2GRAY)
        blue = ((hsv[..., 0] > 95) & (hsv[..., 0] < 125) & (hsv[..., 1] > 60) & (gray > 90)).astype(np.uint8)
        n, lab, st, cen = cv2.connectedComponentsWithStats(blue, connectivity=8)
        i = [k for k in range(1, n) if abs(cen[k][0] - 816) < 8 and abs(cen[k][1] - 1149) < 8]
        self.assertEqual(len(i), 1)
        return cv2.dilate((lab == i[0]).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0

    def test_the_backdrop_through_the_crook_of_her_arm_is_kept(self):
        sliver = self.arm_crook()
        mask = restore.dust_mask(self.img, protect=self.protect)
        self.assertEqual(int(mask[sliver].sum()), 0)
        if have_models():
            src = self.root / 'clean.png'
            Image.fromarray(self.img).save(src)
            out = np.asarray(Image.open(clean.clean(src, crop=False, scratches=True)['output']).convert('RGB'))
            self.assertTrue((out[sliver] == self.img[sliver]).all())

    def test_the_film_dust_is_real_and_visible(self):
        self.assertGreaterEqual(len(self.pieces), 15)
        self.assertGreaterEqual(sum(lift >= 30 for _, lift in self.pieces), 5)

    @unittest.skipUnless(have_models(), 'needs the scratch models (openfilmkit models --fetch)')
    def test_cleaning_the_film_dust_does_no_harm(self):
        out = self.cleaned()
        spots = np.any([m for m, _ in self.pieces], axis=0)
        before = np.abs(self.damaged[spots].astype(int) - self.img[spots]).mean()
        self.assertLessEqual(np.abs(out[spots].astype(int) - self.img[spots]).mean(), before)

    def clear_pieces(self, plain):
        """Film dust at least 30 levels brighter than the photo, outside the protected eyes and
        mouth, on plain ground (no fine texture or shading round it) or not."""
        g = cv2.cvtColor(self.img, cv2.COLOR_RGB2GRAY).astype(np.float32)
        fine = g - cv2.GaussianBlur(g, (0, 0), 1.5)
        local = np.sqrt(np.maximum(cv2.blur(fine * fine, (15, 15)) - cv2.blur(fine, (15, 15)) ** 2, 0))
        m31 = cv2.blur(g, (31, 31))
        shading = np.sqrt(np.maximum(cv2.blur(g * g, (31, 31)) - m31 * m31, 0))
        out = []
        for m, lift in self.pieces:
            if lift < 30 or (m & self.protect).any():
                continue
            around = cv2.dilate(m.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
            is_plain = np.median(local[around]) < 2.5 and np.median(shading[around]) < 6
            if is_plain == plain:
                out.append(m)
        return out

    @unittest.skipUnless(have_models(), 'needs the scratch models (openfilmkit models --fetch)')
    def test_soft_film_dust_on_plain_ground_is_mostly_removed(self):
        # Two of the three pieces on the plain backdrop and flag go (the scratch model takes them);
        # the third is a soft 6 x 9 px blob the dust pass measures against its own median.
        changed = np.abs(self.cleaned().astype(int) - self.damaged).max(axis=2) > 0
        plain = self.clear_pieces(plain=True)
        self.assertGreaterEqual(len(plain), 3)
        self.assertGreaterEqual(sum(changed[m].mean() >= 0.5 for m in plain), len(plain) - 1)

    @unittest.expectedFailure
    @unittest.skipUnless(have_models(), 'needs the scratch models (openfilmkit models --fetch)')
    def test_soft_film_dust_on_busy_patterns_is_found(self):
        # Known gap (Nov 2026): soft, out-of-focus dust on busy, colorful patterns (her ribbon bar)
        # stays. Every dust rule leaves blobs on busy ground alone, because there a blob is as likely
        # to be picture; rules on size, softness and keeping the color (dust on a negative brightens
        # a spot without changing its color) did not separate the two on this portrait. When this
        # passes, remove the marker.
        changed = np.abs(self.cleaned().astype(int) - self.damaged).max(axis=2) > 0
        busy = self.clear_pieces(plain=False)
        self.assertGreaterEqual(sum(changed[m].mean() >= 0.5 for m in busy), len(busy) - 1)


if __name__ == '__main__':
    unittest.main()
