"""The desktop app's back end (openfilmkit.gui_service) and convert --rotate, on made-up scans."""
import argparse
import base64
import io
import json
import mmap
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import numpy as np
    import tifffile
    from PIL import Image
    from openfilmkit import gui_service, roll
    from openfilmkit.films import convert as conversion
except ImportError:
    np = None


def negative(path, h=120, w=180, seed=1):
    """A made-up linear negative scan: a left-to-right ramp, orange like film base, with noise."""
    rng = np.random.default_rng(seed)
    x = np.mgrid[0:h, 0:w][1]
    base = 0.25 + 0.55 * (x / w)
    a = base[..., None] * np.array([42000, 20000, 11000]) + rng.normal(0, 250, (h, w, 3))
    a[: h // 3, : w // 4] *= 0.4        # a dense corner, so the picture isn't symmetric
    tifffile.imwrite(path, np.clip(np.rint(a), 0, 65535).astype(np.uint16), photometric='rgb')


def roll_with_frame(root):
    r = root / 'Roll A'
    roll.create(r, film='color-negative')
    negative(r / 'masters' / 'frame-01.tif')
    return r


def talk(*requests):
    """Run the service over in-memory pipes and return its replies, parsed (progress events left out)."""
    stdin = io.StringIO(''.join(json.dumps(q) + '\n' for q in requests))
    stdout = io.StringIO()
    gui_service.serve(stdin, stdout)
    return [m for m in map(json.loads, stdout.getvalue().splitlines()) if 'event' not in m]


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class Service(unittest.TestCase):
    def test_hello_and_unknown(self):
        hello, unknown, bad = talk({'id': 1, 'method': 'hello'}, {'id': 2, 'method': 'rm_rf'},
                                   {'id': 3, 'method': 'open', 'params': {'path': '/no/such/folder'}})
        self.assertEqual(hello['id'], 1)
        self.assertIn('version', hello['result'])
        self.assertIn('Unknown request', unknown['error'])
        self.assertIn("isn't a folder", bad['error'])

    def test_open_lists_the_roll_and_its_frame(self):
        with tempfile.TemporaryDirectory() as d:
            r = roll_with_frame(Path(d))
            [reply] = talk({'id': 1, 'method': 'open', 'params': {'path': str(r)}})
            places = reply['result']['places']
            self.assertEqual(len(places), 1)
            self.assertTrue(places[0]['is_roll'])
            [frame] = places[0]['frames']
            self.assertEqual(frame['name'], 'frame-01')
            self.assertEqual(frame['state'], 'to convert')
            self.assertTrue(frame['master'].endswith('frame-01.tif'))

    def test_preview_changes_with_settings_and_turns(self):
        with tempfile.TemporaryDirectory() as d:
            master = str(roll_with_frame(Path(d)) / 'masters' / 'frame-01.tif')
            plain, warm, turned = talk(
                {'id': 1, 'method': 'preview', 'params': {'master': master, 'settings': {'auto': False, 'rotate': 0}}},
                {'id': 2, 'method': 'preview', 'params': {'master': master, 'settings': {'auto': False, 'rotate': 0,
                                                                                         'warmth': 0.8}}},
                {'id': 3, 'method': 'preview', 'params': {'master': master, 'settings': {'auto': False, 'rotate': 90}}})

            def img(reply):
                data = reply['result']['image'].split(',', 1)[1]
                import base64
                return np.asarray(Image.open(io.BytesIO(base64.b64decode(data))).convert('RGB')).astype(float)
            a, b, c = img(plain), img(warm), img(turned)
            self.assertEqual(a.shape[:2], (120, 180))
            self.assertEqual(c.shape[:2], (180, 120))          # turned a quarter
            self.assertGreater((b[..., 0] - b[..., 2]).mean(), (a[..., 0] - a[..., 2]).mean())   # warmer
            self.assertEqual(plain['result']['film'], 'color-negative')
            self.assertIn(plain['result']['auto_rotate'], (0, 90, 270))      # what it would turn by itself
            self.assertEqual(turned['result']['rotate'], 90)                 # a turn chosen by hand wins

    def test_save_writes_a_conversion_with_its_recipe(self):
        with tempfile.TemporaryDirectory() as d:
            r = roll_with_frame(Path(d))
            master = str(r / 'masters' / 'frame-01.tif')
            [saved] = talk({'id': 1, 'method': 'save',
                            'params': {'master': master, 'settings': {'saturation': 1.3, 'rotate': 270}}})
            out = Path(saved['result']['output'])
            self.assertEqual(out.parent, (r / 'derived').resolve())
            recipe = json.loads(Path(str(out) + '.json').read_text())
            self.assertEqual(recipe['saturation'], 1.3)
            self.assertEqual(recipe['rotation_cw'], 270)
            self.assertEqual(recipe['automatic']['rotation_reason'], 'given')
            w, h = Image.open(out).size
            self.assertGreater(h, w)                 # turned a quarter: portrait
            [again] = talk({'id': 1, 'method': 'open', 'params': {'path': str(r)}})
            self.assertEqual(again['result']['places'][0]['frames'][0]['state'], 'to clean')

    def test_dust_is_an_adjustment_shown_in_the_preview_and_saved_in_one_picture(self):
        with tempfile.TemporaryDirectory() as d:
            r = roll_with_frame(Path(d))
            master = r / 'masters' / 'frame-01.tif'
            raw = tifffile.imread(master)
            for y, x in ((40, 60), (70, 120), (90, 40)):
                raw[y:y + 2, x:x + 2] = 200                 # specks: they block the light
            tifffile.imwrite(master, raw, photometric='rgb')
            service = gui_service.Service(lambda ev: None)
            plain = service.preview(str(master), {'auto': True})['image']
            dusted = service.preview(str(master), {'dust': True})['image']
            self.assertNotEqual(plain, dusted)
            saved = service.save(str(master), {'dust': True})
            recipe = json.loads(Path(saved['output'] + '.json').read_text())
            self.assertEqual(recipe['dust']['step'], 'dust')
            self.assertGreater(recipe['dust']['pixels_filled'], 0)
            self.assertEqual(sorted(p.name for p in (r / 'derived').iterdir()),
                             ['frame-01-v1.jpg', 'frame-01-v1.jpg.json', 'frame-01-v1.jpg.lut.npy'])
            undusted = service.save(str(master), {})
            self.assertIsNone(json.loads(Path(undusted['output'] + '.json').read_text())['dust'])

    def export(self, service, events, items, **kw):
        service.export(items, **kw)
        got = []
        while True:
            ev = events.get(timeout=120)
            got.append(ev)
            if ev['event'] == 'export-done':
                return got

    def test_export_saves_a_roll_and_skips_what_is_already_saved(self):
        import queue
        with tempfile.TemporaryDirectory() as d:
            r = roll_with_frame(Path(d))
            for n in (2, 3):
                negative(r / 'masters' / f'frame-0{n}.tif', seed=n)
            masters = [str(r / 'masters' / f'frame-0{n}.tif') for n in (1, 2, 3)]
            events = queue.Queue()
            service = gui_service.Service(events.put)
            first = self.export(service, events, [{'master': m, 'settings': {}} for m in masters])
            log = service.work()['export']
            self.assertEqual([i['status'] for i in log['items']], ['saved'] * 3)
            self.assertFalse(log['running'])
            self.assertEqual(log['done']['saved'], 3)
            self.assertEqual([e['status'] for e in first[:-1]], ['saved'] * 3)
            self.assertEqual([e['index'] for e in first[:-1]], [1, 2, 3])
            self.assertIsNotNone(first[0]['seconds_left'])
            self.assertEqual(first[2]['seconds_left'], 0)
            self.assertEqual({k: first[-1][k] for k in ('saved', 'skipped', 'failed', 'stopped')},
                             dict(saved=3, skipped=0, failed=0, stopped=False))
            items = [{'master': m, 'settings': {'warmth': .2} if m.endswith('02.tif') else {}} for m in masters]
            again = self.export(service, events, items)
            self.assertEqual([e['status'] for e in again[:-1]], ['skipped', 'saved', 'skipped'])
            self.assertTrue(again[1]['output'].endswith('frame-02-v2.jpg'))
            self.assertEqual(again[0]['output'], first[0]['output'])

    def test_export_to_a_folder_leaves_plain_copies_there(self):
        """Exporting to a folder (a USB stick, say) puts plain-named copies there and nothing else; doing it
        again copies nothing new, and a changed frame arrives beside the old one instead of over it."""
        import queue
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as stick:
            r = roll_with_frame(Path(d))
            negative(r / 'masters' / 'frame-02.tif', seed=2)
            masters = [str(r / 'masters' / f'frame-0{n}.tif') for n in (1, 2)]
            name = json.loads((r / 'roll.json').read_text())['name']
            events = queue.Queue()
            service = gui_service.Service(events.put)
            first = self.export(service, events, [{'master': m, 'settings': {}} for m in masters], folder=stick)
            self.assertEqual(sorted(os.listdir(stick)), [f'{name} 01.jpg', f'{name} 02.jpg'])
            self.assertEqual(first[0]['copy'], str(Path(stick, f'{name} 01.jpg')))
            self.assertEqual(first[-1]['folder'], stick)
            self.assertIn('removable', first[-1])
            again = self.export(service, events, [{'master': m, 'settings': {}} for m in masters], folder=stick)
            self.assertEqual([e['status'] for e in again[:-1]], ['skipped', 'skipped'])
            self.assertEqual(len(os.listdir(stick)), 2)
            changed = [{'master': m, 'settings': {'warmth': .2} if m.endswith('02.tif') else {}} for m in masters]
            self.export(service, events, changed, folder=stick)
            self.assertEqual(sorted(os.listdir(stick)), [f'{name} 01.jpg', f'{name} 02 (2).jpg', f'{name} 02.jpg'])
            with self.assertRaises(gui_service.Problem):
                service.export([{'master': masters[0]}], folder=str(Path(stick, 'gone')))

    def test_export_can_be_stopped_and_says_what_failed(self):
        import queue
        with tempfile.TemporaryDirectory() as d:
            r = roll_with_frame(Path(d))
            negative(Path(d) / 'loose.tif')
            events = queue.Queue()
            service = gui_service.Service(events.put)
            import threading
            go, real = threading.Event(), service._export_one
            def held(item, tiff, redo, folder=None):          # frames after the first wait until the stop is asked
                if not item['master'].endswith('loose.tif'):
                    go.wait(30)
                return real(item, tiff, redo, folder)
            service._export_one = held
            items = [{'master': str(Path(d) / 'loose.tif')}] + [{'master': str(r / 'masters' / 'frame-01.tif')}] * 3
            service.export(items)
            first = events.get(timeout=120)
            self.assertEqual(first['status'], 'failed')
            self.assertIn('not in a roll folder', first['message'])
            self.assertTrue(service.cancel_export()['cancelled'])
            go.set()
            rest = []
            while not rest or rest[-1]['event'] != 'export-done':
                rest.append(events.get(timeout=120))
            self.assertTrue(rest[-1]['stopped'])
            self.assertEqual(len(rest) - 1, 1)               # the frame under way finishes; the rest don't start

    def test_film_and_scan_cards(self):
        with tempfile.TemporaryDirectory() as d:
            r = roll_with_frame(Path(d))
            master = r / 'masters' / 'frame-01.tif'
            (Path(str(master) + '.json')).write_text(json.dumps({
                'tiff': {'dpi_x': 3600.0}, 'started_utc': '2026-11-18T22:30:45+00:00',
                'scanner': {'vendor': 'PLUSTEK', 'model': 'OpticFilm 8200i SE'},
                'film': {'film': 'color-negative', 'stock': 'kodak-gold200', 'note': 'film edge frame 12A'}}))
            roll.set_choice(r, {'stock_id': 'kodak-gold200', 'profile_id': 'generic',
                                'edge_markings': 'KODAK GOLD 200 (read by hand, 18 Nov 2026)'})
            service = gui_service.Service(lambda ev: None)
            got = service.details(str(master))
            film, scan = got['film_card'], got['scan_card']
            self.assertEqual((film['name'], film['kind'], film['iso'], film['maker']),
                             ('Kodak Gold 200', 'Color negative', 200, 'kodak'))
            self.assertEqual(film['edge_print'], 'KODAK GOLD 200')        # the note in parentheses left off
            self.assertEqual(film['chosen'], 'roll')
            self.assertEqual((scan['dpi'], scan['scanner'], scan['frame']), (3600, 'Plustek OpticFilm 8200i SE', '12A'))
            self.assertEqual(scan['summary'], 'No problems found')
            self.assertEqual(service.preview(str(master))['film_card'], film)
            self.assertEqual(service.details(str(master), stock='fuji-superia400')['film_card']['chosen'], 'here')

    def test_a_scan_with_no_record_still_has_a_card(self):
        with tempfile.TemporaryDirectory() as d:
            negative(Path(d) / 'scan.tif')
            got = gui_service.Service(lambda ev: None).details(str(Path(d) / 'scan.tif'))
            self.assertEqual(got['film_card']['maker'], 'unknown')
            self.assertEqual((got['scan_card']['dpi'], got['scan_card']['frame'], got['scan_card']['scanner']), (None, None, None))
            self.assertEqual((got['scan_card']['width'], got['scan_card']['height']), (180, 120))

    def test_thumbnails_follow_the_frame_and_changed_says_what_differs(self):
        with tempfile.TemporaryDirectory() as d:
            r = roll_with_frame(Path(d))
            master = str(r / 'masters' / 'frame-01.tif')
            service = gui_service.Service(lambda ev: None)
            plain = service.thumb(master, master=True, side=120, settings={'rotate': 0})
            turned = service.thumb(master, master=True, side=120, settings={'rotate': 90})
            warm = service.thumb(master, master=True, side=120, settings={'rotate': 0, 'warmth': .5})
            size = lambda uri: Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1]))).size
            self.assertEqual(size(turned), size(plain)[::-1])
            self.assertNotEqual(warm, plain)
            self.assertEqual(service.changed([{'master': master, 'settings': {}}]), {master: None})   # never saved
            service.save(master, {'warmth': .2})
            self.assertEqual(service.changed([{'master': master, 'settings': {'warmth': .2}},
                                              ]), {master: False})
            self.assertEqual(service.changed([{'master': master, 'settings': {'warmth': .4}}]), {master: True})

    def test_preview_says_how_far_it_has_got(self):
        with tempfile.TemporaryDirectory() as d:
            r = roll_with_frame(Path(d))
            for n in (2, 3, 4):
                negative(r / 'masters' / f'frame-0{n}.tif', seed=n)
            events = []
            service = gui_service.Service(events.append)
            service.preview(str(r / 'masters' / 'frame-01.tif'), {'dust': True})
            steps = [e for e in events if e['event'] == 'preview-progress']
            fractions = [e['fraction'] for e in steps]
            self.assertEqual(fractions, sorted(fractions))                   # only forward
            self.assertEqual((fractions[0], fractions[-1]), (0., 1.))
            said = [e['step'] for e in steps]
            self.assertIn('Reading the scan', said)
            self.assertTrue(any(s.startswith("Measuring the roll's color (") for s in said))
            measuring = [e for e in steps if e['step'].startswith('Measuring')]
            self.assertIsNone(measuring[0]['seconds_left'])                  # no guess from the first frames
            self.assertIsNotNone(measuring[-1]['seconds_left'])              # 3 of 4 done: an estimate
            self.assertIn('Finding dust', said)
            again = []
            service.write = again.append
            service.preview(str(r / 'masters' / 'frame-01.tif'), {'warmth': .1})
            self.assertEqual([e['step'] for e in again], ['Done'])           # already worked out: no bar

    def test_a_zoomed_part_matches_the_preview_where_it_is(self):
        with tempfile.TemporaryDirectory() as d:
            r = roll_with_frame(Path(d))
            master = str(r / 'masters' / 'frame-01.tif')
            service = gui_service.Service(lambda ev: None)
            pic = lambda uri: np.asarray(Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1])))).astype(float)
            for turn in (0, 90, 180, 270):
                settings = {'rotate': turn, 'warmth': .3}
                whole = pic(service.preview(master, settings)['image'])
                h, w = whole.shape[:2]
                part = pic(service.preview_region(master, [0, 0, .5, .5], settings, side=max(h, w) // 2)['image'])
                want = whole[: h // 2, : w // 2]                     # the top-left quarter as shown
                self.assertEqual(part.shape[1] > part.shape[0], want.shape[1] > want.shape[0], turn)
                got = np.asarray(Image.fromarray(part.astype(np.uint8)).resize(want.shape[1::-1])).astype(float)
                self.assertLess(np.abs(got - want).mean(), 6, turn)   # same place, same look
            with self.assertRaises(gui_service.Problem):
                service.preview_region(master, [.5, .5, .5, .9])

    def test_save_outside_a_roll_asks_where(self):
        with tempfile.TemporaryDirectory() as d:
            negative(Path(d) / 'loose.tif')
            [reply] = talk({'id': 1, 'method': 'save', 'params': {'master': str(Path(d) / 'loose.tif')}})
            self.assertIn('Choose where to save it', reply['error'])

    def test_new_roll_refuses_an_existing_folder(self):
        with tempfile.TemporaryDirectory() as d:
            made, again = talk({'id': 1, 'method': 'new_roll', 'params': {'parent': d, 'name': 'Roll B'}},
                               {'id': 2, 'method': 'new_roll', 'params': {'parent': d, 'name': 'Roll B'}})
            self.assertTrue(roll.is_roll(made['result']['path']))
            self.assertIn('already a folder', again['error'])


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
def mapped(a):
    """True if `a` is, or is a view of, a memory-mapped file."""
    while a is not None:
        if isinstance(a, mmap.mmap):
            return True
        a = getattr(a, 'base', None)
    return False


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class OpenFiles(unittest.TestCase):
    """Nothing the service keeps holds a scan's file mapped. Windows won't delete or replace a file that's
    mapped, so a kept map blocked scanning a frame again, and every test's cleanup, there."""

    def test_a_small_scan_kept_for_previews_is_a_copy(self):
        with tempfile.TemporaryDirectory() as d:
            master = roll_with_frame(Path(d)) / 'masters' / 'frame-01.tif'
            self.assertTrue(mapped(conversion.raw_open(master)))   # stored plainly, so mapped: the case at hand
            frame = gui_service.Frame(master, side=4000)            # bigger than the scan: shrunk by 1
            self.assertEqual(frame.stride, 1)
            self.assertFalse(any(mapped(v) for v in vars(frame).values() if isinstance(v, np.ndarray)))


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class SaveMatchesPreview(unittest.TestCase):
    """A save with the automatic crop is the picture the preview showed: the same box and the same levels.
    Dogfooding 66 real negatives found convert finding both again on a copy of another size (the preview's is
    1400 px, convert's 1800), so saves came out up to 2% narrower and up to 24 levels off the preview."""

    def test_the_automatic_box_and_levels_are_the_previews(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d) / 'Roll A'
            roll.create(r, film='color-negative')
            master = r / 'masters' / 'frame-01.tif'
            negative(master, h=1100, w=3000)            # big enough that preview and convert shrink it differently
            service = gui_service.Service(lambda ev: None)
            service.preview(str(master))
            frame = service._frame(str(master), None)
            self.assertGreater(frame.stride, 1)
            out = service.save(str(master))['output']
            recipe = json.loads(Path(out + '.json').read_text())
            self.assertEqual(recipe['crop_choice'], 'automatic')
            self.assertEqual(recipe['raw_crop_pixels'], frame.auto_crop_px())
            self.assertEqual(recipe['levels_basis']['type'], 'as previewed')
            np.testing.assert_allclose(recipe['transmission_low_rgb'], frame.levels[0])
            np.testing.assert_allclose(recipe['transmission_high_rgb'], frame.levels[1])
            del frame
            service.frames.clear()


class PlainHealth(unittest.TestCase):
    def test_only_what_a_person_would_see_in_plain_words(self):
        from openfilmkit.gui_service import plain_health
        ch = lambda **kw: dict(dict(at_zero=0, at_full_scale=0), **kw)
        self.assertEqual(plain_health({'channels': {'blue': ch(at_zero=.001)}}), [])
        said = plain_health({'channels': {'red': ch(at_zero=.05), 'blue': ch(at_zero=.3, at_full_scale=.04)}})
        self.assertEqual(len(said), 2)                       # once each, however many channels
        self.assertTrue(all('GL128' not in w and 'exactly 0' not in w for w in said))
        self.assertIn('banding', plain_health({'channels': {'green': ch(missing_bits=3.1)}})[0])


class Destinations(unittest.TestCase):
    def test_plain_names(self):
        from openfilmkit.destinations import plain_name
        self.assertEqual(plain_name('Roll 2026-11-23', '/r/masters/frame-03.tif', '.jpg'), 'Roll 2026-11-23 03.jpg')
        self.assertEqual(plain_name('Beach', '/r/masters/odd.tif', '.tif'), 'Beach odd.tif')

    def test_copies_never_go_over_another_file(self):
        from openfilmkit import destinations
        with tempfile.TemporaryDirectory() as d:
            a, b, out = Path(d, 'a.jpg'), Path(d, 'b.jpg'), Path(d, 'out')
            out.mkdir()
            a.write_bytes(b'one'); b.write_bytes(b'two')
            self.assertEqual(destinations.copy_out(a, out, 'x.jpg').name, 'x.jpg')
            self.assertEqual(destinations.copy_out(a, out, 'x.jpg').name, 'x.jpg')        # the same: left alone
            self.assertEqual(destinations.copy_out(b, out, 'x.jpg').name, 'x (2).jpg')    # different: beside it
            self.assertEqual(Path(out, 'x.jpg').read_bytes(), b'one')
            self.assertEqual(sorted(os.listdir(out)), ['x (2).jpg', 'x.jpg'])            # no .partial left
            with mock.patch.object(destinations.shutil, 'disk_usage', return_value=mock.Mock(free=10)):
                with self.assertRaisesRegex(destinations.Problem, 'isn’t room'):
                    destinations.copy_out(a, out, 'y.jpg')
            with self.assertRaisesRegex(destinations.Problem, 'isn’t there'):
                destinations.copy_out(a, Path(d, 'gone'), 'x.jpg')

    def test_a_remembered_folder_is_offered_only_while_it_is_there(self):
        from openfilmkit import destinations
        with tempfile.TemporaryDirectory() as d:
            got = destinations.places(also=[d, str(Path(d, 'ejected'))])
            self.assertEqual(Path(got[0]['path']), Path(d).resolve())
            self.assertFalse(any(p['path'].endswith('ejected') for p in got))

    def test_places_starts_with_pictures_and_never_fails(self):
        from openfilmkit import destinations
        with mock.patch.object(destinations, '_mac_volumes', side_effect=RuntimeError), \
             mock.patch.object(destinations, '_linux_volumes', side_effect=RuntimeError):
            got = destinations.places()
        self.assertTrue(all(not p['removable'] for p in got))


class Crop(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        r = roll_with_frame(Path(self.temp.name))
        self.roll, self.master = r, str(r / 'masters' / 'frame-01.tif')
        self.service = gui_service.Service(lambda ev: None)

    def tearDown(self):
        self.temp.cleanup()

    def pic(self, uri):
        return np.asarray(Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1])))).astype(float)

    def test_boxes_turn_and_come_back(self):
        box = [.1, .2, .6, .9]
        for turn in (0, 90, 180, 270):
            back = gui_service.unturn_box(gui_service.turn_box(box, turn), turn)
            np.testing.assert_allclose(back, box, atol=1e-6)
        self.assertEqual(gui_service.turn_box(box, 90), [.1, .1, .8, .6])   # the top-left corner goes top-right

    def test_none_shows_the_whole_scan_and_a_box_shows_just_that(self):
        for turn in (0, 90, 180, 270):
            whole = self.service.preview(self.master, {'rotate': turn, 'crop': 'none'})
            self.assertIsNone(whole['crop_used'])
            W = self.pic(whole['image'])
            self.assertEqual(W.shape[:2], (180, 120) if turn in (90, 270) else (120, 180))
            box = [.1, .2, .6, .9]
            got = self.service.preview(self.master, {'rotate': turn, 'crop': box})
            self.assertEqual(got['crop_used'], box)
            P = self.pic(got['image'])
            h, w = W.shape[:2]
            self.assertLessEqual(abs(P.shape[1] - .5 * w), 2, turn)
            self.assertLessEqual(abs(P.shape[0] - .7 * h), 2, turn)
            # the same place in the whole scan, looking the same
            want = W[round(.2 * h):round(.2 * h) + P.shape[0], round(.1 * w):round(.1 * w) + P.shape[1]]
            self.assertLess(np.abs(P[:want.shape[0], :want.shape[1]] - want).mean(), 4, turn)

    def test_the_editor_view_is_the_whole_scan_with_the_crops_marked(self):
        got = self.service.preview(self.master, {'rotate': 90, 'crop': [.2, .2, .8, .8], 'uncropped': True})
        self.assertEqual(self.pic(got['image']).shape[:2], (180, 120))
        self.assertEqual(got['crop_used'], [.2, .2, .8, .8])
        self.assertEqual(len(got['auto_crop']), 4)
        plain = self.service.preview(self.master, {'rotate': 90})
        self.assertEqual(plain['crop_used'], plain['auto_crop'])          # automatic: the box used is the automatic one

    def test_moving_the_box_keeps_the_color(self):
        a = self.pic(self.service.preview(self.master, {'rotate': 0, 'crop': [0, 0, .5, 1]})['image'])
        b = self.pic(self.service.preview(self.master, {'rotate': 0, 'crop': [0, 0, .7, 1]})['image'])
        self.assertLess(np.abs(a[:, :80] - b[:, :80]).mean(), 2)          # the shared part looks the same

    def test_a_saved_crop_matches_the_preview_and_counts_as_a_change(self):
        box = [.1, .2, .6, .9]
        settings = {'rotate': 90, 'crop': box}
        shown = self.pic(self.service.preview(self.master, settings)['image'])
        out = self.service.save(self.master, settings)['output']
        recipe = json.loads(Path(out + '.json').read_text())
        self.assertEqual(recipe['crop_choice'], 'manual')
        x0, y0, x1, y1 = recipe['raw_crop_pixels']
        self.assertEqual((x1 - x0, y1 - y0), (126, 60))                   # the box unturned: 0.7 of 180 by 0.5 of 120
        saved = np.asarray(Image.open(out)).astype(float)
        self.assertEqual(saved.shape, shown.shape)
        self.assertLess(np.abs(saved - shown).mean(), 6)                  # the one on screen (JPEG noise aside, as for the automatic crop)
        self.assertEqual(self.service.changed([{'master': self.master, 'settings': settings}]), {self.master: False})
        moved = dict(settings, crop=[.1, .2, .5, .9])
        self.assertEqual(self.service.changed([{'master': self.master, 'settings': moved}]), {self.master: True})
        self.assertEqual(self.service.changed([{'master': self.master, 'settings': {'rotate': 90}}]), {self.master: True})
        whole = self.service.save(self.master, {'rotate': 0, 'crop': 'none'})['output']
        r = json.loads(Path(whole + '.json').read_text())
        self.assertEqual((r['crop_choice'], r['raw_crop_pixels']), ('none', [0, 0, 180, 120]))

    def test_mirror_flips_what_is_seen_at_every_turn(self):
        box = [.1, .2, .6, .9]
        for turn in (0, 90, 180, 270):
            plain = self.pic(self.service.preview(self.master, {'rotate': turn})['image'])
            flipped = self.pic(self.service.preview(self.master, {'rotate': turn, 'mirror': True})['image'])
            self.assertLess(np.abs(flipped - plain[:, ::-1]).mean(), 3, turn)
            # a box drawn on the mirrored picture is that part of it
            a = self.pic(self.service.preview(self.master, {'rotate': turn, 'mirror': True, 'crop': box})['image'])
            b = self.pic(self.service.preview(self.master, {'rotate': turn, 'crop': gui_service.mirror_box(box)})['image'])
            self.assertEqual(a.shape, b.shape, turn)
            self.assertLess(np.abs(a - b[:, ::-1]).mean(), 3, turn)
            # and a zoomed part of the mirrored picture is the same part of it
            h, w = flipped.shape[:2]
            part = self.pic(self.service.preview_region(self.master, [0, 0, .5, .5], {'rotate': turn, 'mirror': True},
                                                        side=max(h, w) // 2)['image'])
            part = np.asarray(Image.fromarray(part.astype(np.uint8)).resize((w // 2, h // 2))).astype(float)
            self.assertLess(np.abs(part - flipped[: h // 2, : w // 2]).mean(), 6, turn)

    def test_a_mirrored_save_matches_and_is_recorded(self):
        settings = {'rotate': 90, 'mirror': True, 'crop': [.1, .2, .6, .9]}
        shown = self.pic(self.service.preview(self.master, settings)['image'])
        out = self.service.save(self.master, settings)['output']
        r = json.loads(Path(out + '.json').read_text())
        self.assertTrue(r['mirror'])
        saved = np.asarray(Image.open(out)).astype(float)
        self.assertEqual(saved.shape, shown.shape)
        self.assertLess(np.abs(saved - shown).mean(), 6)
        self.assertEqual(self.service.changed([{'master': self.master, 'settings': settings}]), {self.master: False})
        unflipped = dict(settings, mirror=False)
        self.assertEqual(self.service.changed([{'master': self.master, 'settings': unflipped}]), {self.master: True})

    def test_a_zoomed_part_is_inside_the_manual_crop(self):
        settings = {'rotate': 0, 'crop': [.5, 0, 1, 1]}
        shown = self.pic(self.service.preview(self.master, settings)['image'])
        part = self.pic(self.service.preview_region(self.master, [0, 0, 1, 1], settings, side=max(shown.shape[:2]))['image'])
        part = np.asarray(Image.fromarray(part.astype(np.uint8)).resize(shown.shape[1::-1])).astype(float)
        self.assertLess(np.abs(part - shown).mean(), 6)


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class QueueTab(unittest.TestCase):
    def setUp(self):
        import os
        from unittest import mock
        from openfilmkit import queues
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.queue = self.root / 'queue'
        (self.root / 'config').mkdir()
        (self.root / 'config' / 'hosts.toml').write_text(f'[queues.home]\nqueue = {json.dumps(str(self.queue))}\n')
        self.patches = [mock.patch.dict(os.environ, {'OPENFILMKIT_CONFIG': str(self.root / 'config'),
                                                     'OPENFILMKIT_CACHE': str(self.root / 'cache')}),
                        mock.patch.object(queues, 'SPARE', 0)]
        for m in self.patches:
            m.start()
        self.photos = []
        for n in (1, 2):
            img = np.clip(120 + np.random.default_rng(n).normal(0, 3, (200, 300, 3)), 0, 255).astype(np.uint8)
            img[100:103, 150:153] = 255
            path = self.root / f'frame-0{n}-v1.png'
            Image.fromarray(img).save(path)
            self.photos.append(path)

    def tearDown(self):
        for m in reversed(self.patches):
            m.stop()
        self.temp.cleanup()

    @unittest.skipIf(not shutil.which('rsync'), 'needs rsync, as every queue does')
    def test_a_shared_queue_is_shown_collected_and_tidied(self):
        import contextlib, time
        from openfilmkit import cli, queues
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['clean', *map(str, self.photos), '--no-faces', '--queue', 'home', '--json']), 0)
        service = gui_service.Service(lambda ev: None)
        self.assertEqual(service.work()['queues'], ['home'])
        status = service.queue_status()
        self.assertEqual(status['counts']['waiting'], 2)
        self.assertEqual(status['jobs'][0]['why_waiting'], 'no worker has reported to this queue')
        self.assertEqual({j['photo'] for j in status['jobs']}, {'frame-01-v1.png', 'frame-02-v1.png'})
        w = queues.Worker(queues.worker_settings(queue=str(self.queue), name='w1', state=str(self.root / 'st'),
                                                 settle=0), run=lambda pkg, beat, every: queues.jobs.run(pkg),
                          now=time.time, say=lambda *a: None)
        w.once()
        status = service.queue_status()
        self.assertEqual((status['counts']['done'], status['counts']['waiting']), (1, 1))
        self.assertEqual(status['workers'][0]['name'], 'w1')
        collected = service.queue_collect()
        self.assertEqual(len(collected['collected']), 1)
        self.assertTrue(Path(collected['collected'][0]).is_file())
        with self.assertRaises(gui_service.Problem):
            service.queue_remove()                       # never everything by default
        left = service.queue_status()['jobs']
        self.assertEqual(service.queue_remove(jobs=[left[0]['short']])['removed'], [left[0]['id']])
        self.assertEqual(service.queue_status()['jobs'], [])

    @unittest.skipIf(not shutil.which('rsync'), 'needs rsync, as every queue does')
    def test_status_over_the_pipe_is_answered(self):
        (self.queue / 'jobs').mkdir(parents=True)
        (self.queue / 'drop').mkdir()
        [reply] = talk({'id': 7, 'method': 'queue_status', 'params': {}})
        self.assertEqual((reply['id'], reply['result']['jobs']), (7, []))
        self.queue.rename(self.root / 'gone')            # its drive not mounted, say: said in words
        [reply] = talk({'id': 8, 'method': 'queue_status', 'params': {}})
        self.assertIn("couldn't read the queue", reply['error'])

    def test_no_queue_set_up(self):
        (self.root / 'config' / 'hosts.toml').write_text('')
        service = gui_service.Service(lambda ev: None)
        self.assertEqual(service.work(), dict(export=None, queues=[]))
        with self.assertRaises(gui_service.Problem):
            service.queue_status()


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class Rotate(unittest.TestCase):
    def convert(self, root, name, rotate, tiff=False):
        args = argparse.Namespace(source=str(root / 'raw.tif'), output=str(root / name), stock='unknown', roll=None,
                                  profile=None, crop=None, levels_from=None, max_side=0, contrast=1.3, gamma=1.,
                                  warmth=0., saturation=1., rotate=rotate, tiff=tiff, auto=False)
        return conversion.convert(args)

    def test_turns_clockwise_and_records_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            negative(root / 'raw.tif')
            self.convert(root, 'up.png', 0)
            rec = self.convert(root, 'right.png', 90)
            up = np.asarray(Image.open(root / 'up.png'))
            right = np.asarray(Image.open(root / 'right.png'))
            self.assertTrue(np.array_equal(right, np.rot90(up, -1)))
            self.assertEqual(rec['rotation_cw'], 90)
            self.assertEqual(rec['output_dimensions'], [120, 180])
            rec = self.convert(root, 'half.tif', 180, tiff=True)
            self.assertEqual(tifffile.imread(root / 'half.tif').shape, (120, 180, 3))

    def test_refuses_other_angles(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            negative(root / 'raw.tif')
            with self.assertRaises(ValueError):
                self.convert(root, 'odd.png', 45)


if __name__ == '__main__':
    unittest.main()


class Scanning(unittest.TestCase):
    """What the app is told about scans and new rolls (found dogfooding with a fake scanner, 2026-11-22)."""

    def test_new_rolls_take_todays_local_date_and_a_free_name(self):
        import datetime
        from openfilmkit import gui_service as g
        svc = g.Service.__new__(g.Service)
        with tempfile.TemporaryDirectory() as d:
            today = datetime.date.today().isoformat()
            names = [Path(svc.new_roll(d)['path']).name for _ in range(3)]
            self.assertEqual(names, [f'Roll {today}', f'Roll {today} 2', f'Roll {today} 3'])
            with self.assertRaises(g.Problem):   # a name asked for by name is never changed
                svc.new_roll(d, f'Roll {today}')

    def test_outcomes_in_plain_words(self):
        from openfilmkit.gui_service import scan_outcome
        health = lambda full, zero=0: {'channels': {c: {'median': int(full * 65535), 'at_zero': zero} for c in 'rgb'}}
        good = lambda h: {'captures': [{'status': 'captured_verified', 'output': '/r/masters/frame-01.tif',
                                        'channel_health': h}]}
        ok = scan_outcome(0, good(health(.01)), [], False)
        self.assertEqual((ok['ok'], ok['output'], ok['warnings'], ok['empty']), (True, '/r/masters/frame-01.tif', [], False))
        empty = scan_outcome(0, good(health(.98)), [], False)
        self.assertTrue(empty['empty'] and 'empty' in empty['warnings'][0])
        self.assertIn('known fault of this scanner', scan_outcome(0, good(health(.01, .05)), [], False)['warnings'][0])
        self.assertEqual(scan_outcome(0, good(health(.01, .001)), [], False)['warnings'], [])   # too little to see
        failed = lambda err: {'captures': [{'status': 'failed_or_interrupted', 'error': err}]}
        jam = scan_outcome(1, failed('scanimage exited with status 9; see x.log'),
                           ['scanimage: sane_read: Error during device I/O'], False)
        self.assertEqual((jam['ok'], jam['output'], jam['stopped']), (False, None, False))
        self.assertIn('stopped partway', jam['message'])
        stop = scan_outcome(1, failed('stopped before it finished'), [], True)
        self.assertTrue(stop['stopped'] and 'You stopped' in stop['message'])
        self.assertIn('isn’t connected', scan_outcome(2, {'error': 'no scanner found'}, [], False)['message'])
        self.assertIn('scanner messages', scan_outcome(1, None, [], False)['message'])
        opened = 'scanimage: open of device genesys:libusb:000:002 failed: '
        self.assertIn('didn’t answer', scan_outcome(1, failed('scanimage exited with status 1; see x.log'),
                                                    [opened + 'Invalid argument'], False)['message'])
        self.assertIn('Another program', scan_outcome(1, failed('scanimage exited with status 1; see x.log'),
                                                      [opened + 'Device busy'], False)['message'])
        self.assertIn('didn’t answer', scan_outcome(1, failed('x'), [opened + 'Error during device I/O'], False)['message'])

    def test_progress_lines(self):
        from openfilmkit.gui_service import PROGRESS_LINE
        self.assertEqual(PROGRESS_LINE.match('Progress: 12.5%')[1], '12.5')
        self.assertIsNone(PROGRESS_LINE.match('Saved and verified: x'))


@unittest.skipIf(np is None, 'needs numpy, tifffile and Pillow')
class EmptySlots(unittest.TestCase):
    """A scan of no film (the holder past the end of the strip) is shown plainly and said to be empty, and doesn't
    count toward the roll's color."""

    def blank(self, path, level=65535):
        tifffile.imwrite(path, np.full((120, 180, 3), level, np.uint16), photometric='rgb')

    def test_an_empty_slot_previews_plainly_and_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            r = roll_with_frame(Path(d))
            for n, level in (('02', 65535), ('03', 65534)):   # exactly white, and a step under, as the scanner gives
                self.blank(r / 'masters' / f'frame-{n}.tif', level)
            for n in ('02', '03'):
                (reply,) = talk({'id': 1, 'method': 'preview', 'params': {'master': str(r / 'masters' / f'frame-{n}.tif')}})
                res = reply['result']
                self.assertEqual((res['empty'], res['auto_rotate']), (True, 0), n)
                self.assertEqual(len(res['warnings']), 1)
                self.assertIn('looks empty', res['warnings'][0])
                (thumb,) = talk({'id': 2, 'method': 'thumb', 'params': {'path': str(r / 'masters' / f'frame-{n}.tif'),
                                                                         'master': True}})
                self.assertTrue(thumb['result'].startswith('data:image/jpeg'))
            (reply,) = talk({'id': 3, 'method': 'preview', 'params': {'master': str(r / 'masters' / 'frame-01.tif')}})
            self.assertFalse(reply['result']['empty'])
            chosen = {'stock_id': 'x', 'profile_id': 'generic'}
            from openfilmkit.films import stocks
            table = conversion.roll_balance(r, chosen, stocks.profile('generic'))
            self.assertEqual(sorted(table), ['frame-01.tif'])
            self.assertEqual(sorted(conversion.roll_balance(r, chosen, stocks.profile('generic'), measure=False)),
                             ['frame-01.tif'])   # kept as empty, so not read again, and still left out
