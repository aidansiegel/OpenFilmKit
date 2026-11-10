"""The terminal UI: what it finds in a roll, how it describes a recipe, and the app driven by
Textual's test pilot (keys, the clean dialog, the queue's states, the preview)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np
    from PIL import Image
except ImportError:
    np = None
try:
    import textual  # noqa: F401
    import textual_image  # noqa: F401
    from openfilmkit.ui import app as ui_app
except ImportError:
    ui_app = None
from openfilmkit import roll
from openfilmkit.ui import model


def picture(path, value=120):
    Image.fromarray(np.full((40, 60, 3), value, np.uint8)).save(path)


def make_roll(root):
    """A roll with frame 01 converted and cleaned, 02 converted (as a JPEG and a TIFF, the TIFF
    cleaned), and 03 only scanned."""
    r = root / 'roll-35'
    roll.create(r, film='color-negative', stock='kodak-gold200')
    d = r / 'derived'
    d.mkdir()
    for n in ('01', '02', '03'):
        (r / 'masters' / f'frame-{n}.tif').write_bytes(b'master')
    picture(d / 'frame-01-v1.png')
    picture(d / 'frame-01-v1-clean-v1.png', 130)
    (d / 'frame-01-v1-clean-v1.png.json').write_text(json.dumps({
        'status': 'Cleaned copy — requires visual review',
        'steps': [{'step': 'dust', 'pixels_filled': 1672, 'faces_protected': 1},
                  {'step': 'scratches', 'pixels_filled': 3326, 'model_calls': 12},
                  {'step': 'crop', 'changed': True}],
        'filled_in_by_model': {'pixels': 3326, 'share': 0.0026},
        'job': {'id': 'ab' * 32, 'ran_on': 'portable'}}))
    picture(d / 'frame-02-v1.jpg')
    picture(d / 'frame-02-v1.png')
    picture(d / 'frame-02-v1-clean-v1.png', 140)
    return r


@unittest.skipIf(np is None, 'needs numpy and Pillow')
class Model(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.roll = make_roll(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_roll_shows_each_conversion_with_its_own_cleaned_copies(self):
        items = model.items(self.roll)
        self.assertEqual([(i.name, i.state) for i in items],
                         [('frame-01', 'cleaned'), ('frame-02-v1.jpg', 'to clean'),
                          ('frame-02-v1.png', 'cleaned'), ('frame-03', 'to convert')])
        self.assertEqual(items[2].latest.name, 'frame-02-v1-clean-v1.png')    # the PNG's, not the JPEG's
        self.assertTrue(all(i.master for i in items))

    def test_a_folder_of_rolls_or_of_photos_is_shown_too(self):
        self.assertEqual([label for label, _ in model.places(self.root)], ['roll-35'])
        loose = self.root / 'loose'
        loose.mkdir()
        picture(loose / 'beach.png')
        picture(loose / 'beach-clean-v1.png')
        picture(loose / 'beach-clean-v2.png')
        (only,) = model.items(loose)
        self.assertEqual((only.name, [p.name for p in only.cleaned]), ('beach', ['beach-clean-v1.png', 'beach-clean-v2.png']))

    def test_a_recipe_is_described_in_plain_words(self):
        text = '\n'.join(model.describe(model.items(self.roll)[0]))
        for words in ('1,672 pixels filled from their surroundings', '3,326 pixels filled by MI-GAN',
                      'holder edges trimmed', '0.26% of the picture', 'Made on portable'):
            self.assertIn(words, text)
        self.assertIn('Not converted yet', '\n'.join(model.describe(model.items(self.roll)[3])))

    def test_before_and_after_make_one_picture(self):
        item = model.items(self.roll)[0]
        both, n = model.side_by_side(item.source, item.latest, side=60)
        self.assertEqual((both.width, both.height, n), (60 + 8 + 60, 40, None))
        marked, n = model.side_by_side(item.source, item.latest, side=60, fixes=True)
        self.assertEqual(n, 40 * 60)                           # the test copy differs everywhere by 10 levels

    def test_changes_are_marked_even_where_the_preview_is_much_smaller(self):
        d = self.roll / 'derived'
        base = np.full((400, 600, 3), 120, np.uint8)
        Image.fromarray(base).save(d / 'big-v1.png')
        after = base.copy()
        after[200, 300] = 200                                  # one pixel changed
        Image.fromarray(after).save(d / 'big-v1-clean-v1.png')
        picture, n = model.changes(d / 'big-v1.png', d / 'big-v1-clean-v1.png', side=60)
        self.assertEqual((n, picture.size), (1, (60, 40)))
        red = np.all(np.asarray(picture) == (230, 30, 30), axis=2)
        self.assertTrue(red[20, 30] and red.sum() < 20)        # still there, at a tenth of the size

    def test_the_original_is_cropped_like_the_cleaned_copy(self):
        d = self.roll / 'derived'
        base = np.random.default_rng(1).integers(0, 255, (100, 160, 3), dtype=np.uint8)
        Image.fromarray(base).save(d / 'crop-v1.png')
        Image.fromarray(base[10:90, 20:140]).save(d / 'crop-v1-clean-v1.png')
        (d / 'crop-v1-clean-v1.png.json').write_text(json.dumps({'steps': [{'step': 'crop', 'box': [20, 10, 140, 90]}]}))
        self.assertEqual(model.changes(d / 'crop-v1.png', d / 'crop-v1-clean-v1.png')[1], 0)
        (d / 'crop-v1-clean-v1.png.json').write_text(json.dumps({'steps': [{'step': 'straighten', 'degrees': 2}]}))
        with self.assertRaises(model.NotComparable):
            model.changes(d / 'crop-v1.png', d / 'crop-v1-clean-v1.png')

    def test_a_viewer_isnt_opened_without_a_screen(self):
        with mock.patch.object(model, 'screen_here', lambda: False):
            with self.assertRaisesRegex(OSError, 'no screen'):
                model.open_in_viewer(self.roll / 'derived' / 'frame-01-v1.png')


@unittest.skipIf(ui_app is None or np is None, 'needs Textual and textual-image (the ui extra)')
class App(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.roll = make_roll(self.root)
        self.env = mock.patch.dict(os.environ, {'OPENFILMKIT_CONFIG': str(self.root / 'config')})
        self.env.start()
        (self.root / 'config').mkdir()
        (self.root / 'config' / 'hosts.toml').write_text('[hosts.portable]\nssh = "user@portable.local"\n')

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def cells(self, app, column):
        table = app.query_one('#frames')
        return [str(table.get_row_at(i)[column]) for i in range(table.row_count)]

    async def test_frames_recipe_and_preview(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(self.cells(app, 5), ['cleaned', 'to clean', 'cleaned', 'to convert'])
            self.assertIn('MI-GAN', str(app.query_one('#recipe').render()))
            await pilot.pause(0.4)                             # the load starts after SETTLE
            await app.workers.wait_for_complete()
            self.assertIsNotNone(app.query_one('#preview').image)
            await pilot.press('down', 'down', 'down')
            self.assertIn('Not converted yet', str(app.query_one('#recipe').render()))

    async def caption_after(self, app, pilot, *keys):
        await pilot.press(*keys)
        await pilot.pause(0.4)                                 # the load starts after SETTLE
        await app.workers.wait_for_complete()
        await pilot.pause()
        return str(app.query_one('#preview-caption').render())

    async def test_b_flips_in_place_shift_b_side_by_side_ctrl_shift_b_marks_fixes(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        async with app.run_test(size=(120, 40)) as pilot:
            self.assertTrue((await self.caption_after(app, pilot)).startswith('after:'))
            self.assertTrue((await self.caption_after(app, pilot, 'b')).startswith('before:'))
            self.assertTrue((await self.caption_after(app, pilot, 'b')).startswith('after:'))
            self.assertIn('|  after:', await self.caption_after(app, pilot, 'B'))
            self.assertTrue((await self.caption_after(app, pilot, 'B')).startswith('after:'))   # off again
            self.assertIn('pixels changed noticeably', await self.caption_after(app, pilot, 'ctrl+shift+b'))
            self.assertTrue((await self.caption_after(app, pilot, 'ctrl+b')).startswith('after:'))   # any terminal

    async def test_a_flurry_of_presses_works_out_one_preview(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        made = []
        async with app.run_test(size=(120, 40)) as pilot:
            await self.caption_after(app, pilot)
            real = app.make_preview
            app.make_preview = lambda item, view: made.append(view) or real(item, view)
            caption = await self.caption_after(app, pilot, 'b', 'b', 'b', 'b', 'b')   # ends on before
            self.assertLessEqual(len(made), 1)
            self.assertTrue(caption.startswith('before:'))

    async def test_marking_and_cleaning_on_a_host(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        ran = []
        async with app.run_test(size=(120, 40)) as pilot:
            app.run_openfilmkit = ran.append
            await pilot.press('a')                                   # every frame that needs cleaning
            self.assertEqual(self.cells(app, 0), [' ', '✓', ' ', ' '])
            await pilot.press('c')
            await pilot.click('#scratches')
            await pilot.click('#on-portable')
            await pilot.click('#go')
            await pilot.pause()
        self.assertEqual(ran, [['clean', str(self.roll / 'derived' / 'frame-02-v1.jpg'), '--scratches', '--on', 'portable']])

    async def test_queue_states_show_against_their_frames(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        async with app.run_test(size=(120, 40)) as pilot:
            frame2 = str(self.roll / 'derived' / 'frame-02-v1.jpg')
            app._queue_seen({frame2: {'state': 'running', 'worker': 'portable'}}, 'Queue home: 1 running')
            await pilot.pause()
            self.assertEqual(self.cells(app, 5)[1], 'running on portable')

    async def test_help_opens_and_closes(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press('question_mark')
            self.assertEqual(type(app.screen).__name__, 'HelpScreen')
            await pilot.press('escape')
            self.assertEqual(type(app.screen).__name__, 'Screen')


if __name__ == '__main__':
    unittest.main()
