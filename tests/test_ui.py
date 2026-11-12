"""The terminal UI: what it finds in a roll, how it describes a recipe, and the app driven by
Textual's test pilot (keys, the clean dialog, the queue's states, the preview)."""
import json
import os
import sys
import tempfile
import time
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

    def test_an_original_is_shown_the_way_clean_reads_it(self):
        """Turned by its orientation tag and in sRGB, so it lines up with its cleaned copy."""
        import imagecodecs
        d = self.roll / 'derived'
        x = np.linspace(0, 1, 1024)
        curve = np.rint(np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4) * 65535).astype(np.uint16)
        p3 = imagecodecs.cms_profile('rgb', whitepoint=(0.3127, 0.3290, 1.0), transferfunction=curve,
                                     primaries=(0.680, 0.320, 1.0, 0.265, 0.690, 1.0, 0.150, 0.060, 1.0))

        def squares(colors):
            return np.repeat(np.repeat(np.array([colors], np.uint8), 40, 0), 40, 1)
        exif = Image.Exif()
        exif[0x0112] = 6                                       # orientation: show it a quarter turn clockwise
        Image.fromarray(squares([(200, 100, 50), (40, 180, 90), (10, 20, 200)])).save(d / 'phone-v1.png', exif=exif, icc_profile=p3)
        upright = np.rot90(squares([(215, 93, 31), (0, 183, 78), (7, 20, 208)]), -1)   # the same colors in sRGB
        Image.fromarray(upright).save(d / 'phone-v1-clean-v1.png')
        (d / 'phone-v1-clean-v1.png.json').write_text(json.dumps({'steps': [
            {'step': 'to_srgb', 'from': 'Display P3'}, {'step': 'upright', 'orientation': 6}]}))
        shown = model.preview_image(d / 'phone-v1.png')
        self.assertEqual(shown.size, (40, 120))
        self.assertLessEqual(np.abs(np.asarray(shown).astype(int) - upright).max(), 1)
        self.assertEqual(model.changes(d / 'phone-v1.png', d / 'phone-v1-clean-v1.png')[1], 0)
        self.assertEqual(model.before_image(d / 'phone-v1.png', d / 'phone-v1-clean-v1.png').size, (40, 120))
        text = '\n'.join(model.describe(model.Item('phone', d / 'phone-v1.png', cleaned=[d / 'phone-v1-clean-v1.png'])))
        for words in ('Turned upright', 'converted from Display P3 to sRGB'):
            self.assertIn(words, text)
        import tifffile
        tifffile.imwrite(d / 'deep-v1.tif', squares([(200, 100, 50), (40, 180, 90), (10, 20, 200)]).astype(np.uint16) * 257,
                         photometric='rgb', iccprofile=p3, extratags=[(274, 'H', 1, 6, True)])    # a TIFF's own orientation tag
        deep = model.preview_image(d / 'deep-v1.tif')
        self.assertEqual(deep.size, (40, 120))
        self.assertLessEqual(np.abs(np.asarray(deep).astype(int) - upright).max(), 1)
        Image.fromarray(upright).save(d / 'odd-v1.png', icc_profile=b'not a profile at all' * 20)
        self.assertEqual(model.preview_image(d / 'odd-v1.png').size, (40, 120))    # clean refuses it; it can still be looked at

    def test_a_zoomed_preview_shows_that_part_at_full_size(self):
        d = self.roll / 'derived'
        base = np.zeros((400, 600, 3), np.uint8)
        base[300:, 450:] = 255                                 # the bottom-right corner is white
        Image.fromarray(base).save(d / 'zoom-v1.png')
        after = base.copy()
        after[350, 520] = 0                                    # one speck filled, in that corner
        Image.fromarray(after).save(d / 'zoom-v1-clean-v1.png')
        self.assertIsNone(model.zoomed(1, (0.9, 0.9)))
        region = model.zoomed(4, (0.99, 0.99))                 # kept inside the picture
        self.assertEqual(region, (0.75, 0.75, 1.0, 1.0))
        part = model.preview_image(d / 'zoom-v1.png', region=region)
        self.assertEqual(part.size, (150, 100))
        self.assertTrue(np.all(np.asarray(part) == 255))
        marked, n = model.changes(d / 'zoom-v1.png', d / 'zoom-v1-clean-v1.png', region=region)
        self.assertEqual((marked.size, n), ((150, 100), 1))
        both, _ = model.side_by_side(d / 'zoom-v1.png', d / 'zoom-v1-clean-v1.png', region=region)
        (l0, r0, *_), (l1, r1, *_) = both.info['panels']
        self.assertEqual((l0, round(r0, 3), round(l1, 3), r1), (0.0, round(150 / 308, 3), round(158 / 308, 3), 1.0))

    def test_spots_are_drawn_where_they_are_and_clean_repeats_a_recipe(self):
        im = model.preview_image(self.roll / 'derived' / 'frame-01-v1.png')   # 60x40
        ringed = np.asarray(model.draw_spots(im, [(30, 20, 5)], (60, 40)))
        self.assertTrue(np.all(ringed[20, 25] == (255, 210, 0)) and not np.all(ringed[20, 30] == (255, 210, 0)))
        outside = np.asarray(model.draw_spots(im, [(5, 5, 2)], (60, 40), region=(0.5, 0.5, 1, 1)))
        self.assertTrue(np.array_equal(outside, np.asarray(im)))
        self.assertEqual(model.clean_args({'crop': False, 'dust': True, 'protect_faces': True, 'color': True,
                                           'straighten': 1.5, 'lift_shadows': None, 'red_eye': False,
                                           'scratches': True, 'spare_long_lines': False}),
                         ['--no-crop', '--color', '--scratches', '--straighten', '1.5'])

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
            self.assertIn('pixels changed, in red', await self.caption_after(app, pilot, 'ctrl+shift+b'))
            self.assertTrue((await self.caption_after(app, pilot, 'ctrl+b')).startswith('after:'))   # any terminal

    async def test_a_flurry_of_presses_works_out_one_preview(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        made = []
        async with app.run_test(size=(120, 40)) as pilot:
            await self.caption_after(app, pilot)
            real = app.make_preview
            app.make_preview = lambda item, view, region: made.append(view) or real(item, view, region)
            caption = await self.caption_after(app, pilot, 'b', 'b', 'b', 'b', 'b')   # ends on before
            self.assertLessEqual(len(made), 1)
            self.assertTrue(caption.startswith('before:'))

    async def test_marking_and_cleaning_on_a_host(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        ran = []
        async with app.run_test(size=(120, 40)) as pilot:
            app.run_command = lambda argv, about, doing: ran.append(argv)
            await pilot.press('a')                                   # every frame that needs cleaning
            self.assertEqual(self.cells(app, 0), [' ', '✓', ' ', ' '])
            await pilot.press('c')
            await pilot.click('#scratches')
            await pilot.click('#on-portable')
            await pilot.click('#go')
            await pilot.pause()
        self.assertEqual(ran, [['clean', str(self.roll / 'derived' / 'frame-02-v1.jpg'), '--scratches', '--on', 'portable']])

    async def test_a_click_zooms_in_there_and_escape_shows_it_all(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.caption_after(app, pilot)
            await pilot.click('#preview', offset=(0, 0))       # the top-left corner
            await pilot.pause(0.4)
            await app.workers.wait_for_complete()
            self.assertEqual(app.zoom, 2)
            self.assertEqual(app.region(), (0, 0, 0.5, 0.5))
            self.assertIn('×4', await self.caption_after(app, pilot, 'z'))
            self.assertEqual(app.zoom, 4)
            await pilot.press('l', 'l')                        # a quarter of what's shown, twice
            self.assertAlmostEqual(app.center[0], 0.25 + 2 * 0.0625)    # from the corner's center at ×2
            self.assertNotIn('×', await self.caption_after(app, pilot, 'escape'))
            self.assertEqual(app.zoom, 1)

    async def test_spots_are_marked_by_clicking_and_cleaned_with_the_same_options(self):
        recipe = self.roll / 'derived' / 'frame-01-v1-clean-v1.png.json'
        rec = json.loads(recipe.read_text())
        rec['options'] = {'crop': True, 'dust': True, 'protect_faces': True, 'color': False, 'straighten': None,
                          'lift_shadows': None, 'red_eye': False, 'scratches': True, 'spare_long_lines': False}
        recipe.write_text(json.dumps(rec))
        app = ui_app.OpenFilmKitApp(self.roll)
        ran = []
        async with app.run_test(size=(120, 40)) as pilot:
            app.run_command = lambda argv, about, doing: ran.append(argv)
            await self.caption_after(app, pilot, 's')
            app.preview_clicked(0.5, 0.5)                      # the middle of the 60x40 cleaned copy
            app.preview_clicked(0.1, 0.1)
            app.preview_clicked(0.1, 0.1)                      # again: unmarked
            self.assertEqual(app.spots, {str(self.roll / 'derived' / 'frame-01-v1-clean-v1.png'): [[30, 20, 2]]})
            self.assertEqual(self.cells(app, 5)[0], 'cleaned · 1 spot')
            await pilot.press('c')
            self.assertIn('same places', str(app.screen.query_one('#fixed').render()))
            await pilot.click('#go')
            await pilot.pause()
        self.assertEqual(ran, [['clean', str(self.roll / 'derived' / 'frame-01-v1.png'), '--scratches',
                                '--spot', '30,20,2']])

    async def test_converting_a_frame_from_its_master(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        ran = []
        async with app.run_test(size=(120, 40)) as pilot:
            app.run_command = lambda argv, about, doing: ran.append((argv, doing))
            await pilot.press('down', 'down', 'down', 'v')
            await pilot.click('#tiff')
            await pilot.click('#go')
            await pilot.pause()
            self.assertEqual(self.cells(app, 5)[3], 'waiting')
        self.assertEqual(ran, [(['convert', str(self.roll / 'masters' / 'frame-03.tif'), '--tiff'], 'converting…')])

    async def test_commands_run_one_at_a_time_in_one_thread(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        async with app.run_test(size=(120, 40)) as pilot:
            started, active = [], []

            class Proc:
                def __init__(self, argv, **kw):
                    active.append(1)
                    started.append((argv[-1], len(active)))
                    self.stdout, self.returncode = iter(['working']), 0

                def wait(self):
                    time.sleep(0.1)
                    active.pop()

            with mock.patch.object(ui_app.subprocess, 'Popen', Proc):
                for n in range(20):                            # more than the threads previews share
                    app.start_command(['models', str(n)], [], 'x')
                self.assertEqual(len([w for w in app.workers if w.group == 'commands']), 1)
                await pilot.press('b')                         # previews still load meanwhile
                for _ in range(20):
                    await pilot.pause(0.1)
                    if str(app.query_one('#preview-caption').render()).startswith('before:'):
                        break
                self.assertTrue(str(app.query_one('#preview-caption').render()).startswith('before:'))
                self.assertLess(len(started), 20)              # while commands were still running
                while len(started) < 20 or active:
                    await pilot.pause(0.1)
            self.assertEqual(started, [(str(n), 1) for n in range(20)])

    async def test_quitting_runs_no_more_waiting_commands(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        ran = []
        async with app.run_test(size=(120, 40)) as pilot:
            app.run_command = lambda argv, about, doing: ran.append(argv) or time.sleep(0.3)
            app.start_command(['models', 'one'], [], 'x')
            app.start_command(['models', 'two'], [], 'x')
            await pilot.pause(0.1)
        time.sleep(0.5)
        self.assertEqual(ran, [['models', 'one']])

    async def test_panning_shows_the_new_part(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        regions = []
        async with app.run_test(size=(120, 40)) as pilot:
            await self.caption_after(app, pilot, 'z')
            real = app.make_preview
            app.make_preview = lambda item, view, region: regions.append(region) or real(item, view, region)
            await self.caption_after(app, pilot, 'l')
            await self.caption_after(app, pilot, 'l', 'l', 'l', 'l')   # past the edge: stays at it
            await self.caption_after(app, pilot, 'h')
            self.assertEqual(app.region(), (0.375, 0.25, 0.875, 0.75))   # one step back, from the edge
        self.assertEqual(regions, [(0.375, 0.25, 0.875, 0.75), (0.5, 0.25, 1.0, 0.75)])   # then from the cache

    async def test_a_master_converted_twice_is_converted_once_more(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        ran = []
        async with app.run_test(size=(120, 40)) as pilot:
            app.run_command = lambda argv, about, doing: ran.append(argv)
            await pilot.press('down', 'm', 'down', 'm', 'v')  # frame-02's JPEG and its PNG
            await pilot.click('#go')
            await pilot.pause()
        self.assertEqual(ran, [['convert', str(self.roll / 'masters' / 'frame-02.tif')]])

    async def test_a_blocky_preview_says_so_once(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        with mock.patch.object(ui_app, 'BLOCKY', True), mock.patch.object(app, 'notify') as notify:
            async with app.run_test(size=(120, 40)) as pilot:
                await self.caption_after(app, pilot)
                await self.caption_after(app, pilot, 'down')
        said = [c for c in notify.call_args_list if c.kwargs.get('title') == 'Low-resolution preview']
        self.assertEqual(len(said), 1)
        self.assertIn('low-resolution guide', said[0].args[0])

    async def test_queue_states_show_against_their_frames(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        async with app.run_test(size=(120, 40)) as pilot:
            frame2 = str(self.roll / 'derived' / 'frame-02-v1.jpg')
            app._queue_seen({frame2: {'state': 'running', 'worker': 'portable'}}, 'Queue home: 1 running')
            await pilot.pause()
            self.assertEqual(self.cells(app, 5)[1], 'running on portable')

    async def test_each_preview_replaces_the_last_in_the_terminal_and_is_freed_on_quitting(self):
        """Ghostty, Kitty and WezTerm keep a picture until it's replaced (sent again under its i= ID) or
        freed (d=I); Ghostty 1.3.1 keeps one freed while another is on screen, so each preview replaces
        the last."""
        sent = []

        def send(payload=None, **kw):
            sent.append(kw)
        app = ui_app.OpenFilmKitApp(self.roll)
        with mock.patch.object(ui_app.Preview, '_Renderable', ui_app.InPlaceTGP), \
                mock.patch.object(ui_app, '_send_tgp_message', send), \
                mock.patch('textual_image.renderable.tgp._send_tgp_message', send):
            async with app.run_test(size=(120, 40)) as pilot:
                await self.caption_after(app, pilot)
                await self.caption_after(app, pilot, 'b')
                await self.caption_after(app, pilot, 'b')
                one_id = app.query_one('#preview').terminal_id
                pictures = [k for k in sent if k.get('f')]
                self.assertGreaterEqual(len(pictures), 3)
                self.assertEqual({k['i'] for k in pictures}, {one_id})
                self.assertEqual([k for k in sent if k.get('a') == 'd' and k.get('d') != 'i'], [])
        freed = [k for k in sent if k.get('a') == 'd' and k.get('d') == 'I']
        self.assertEqual([k['i'] for k in freed], [one_id])

    async def test_help_opens_and_closes(self):
        app = ui_app.OpenFilmKitApp(self.roll)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press('question_mark')
            self.assertEqual(type(app.screen).__name__, 'HelpScreen')
            await pilot.press('escape')
            self.assertEqual(type(app.screen).__name__, 'Screen')


if __name__ == '__main__':
    unittest.main()
