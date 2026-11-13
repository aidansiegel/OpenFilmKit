"""Stage checkpoints: a clean stopped after any stage, then resumed, gives the same file byte for
byte as one that never stopped.

The scratch pass runs here with stand-ins for the two networks (fast and deterministic, through
the same tiling, mask, window and checkpoint code), so every stop can be tried in seconds.
"""
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
    import cv2
    import numpy as np
    from PIL import Image
    from openfilmkit import clean, jobs
    from openfilmkit.clean import checkpoints, scratches
except ImportError:
    clean = None


class Stop(Exception):
    """A run that stops partway (power cut, Ctrl-C, a machine that sleeps)."""


class ScratchNet:
    """Stands in for the scratch model: bright pixels are scratches."""
    def setInput(self, x):
        self.x = x

    def forward(self):
        return (self.x > 0.6).astype(np.float32)


class FillNet:
    """Stands in for MI-GAN: paints holes mid-gray."""
    def setInput(self, x):
        pass

    def forward(self):
        return np.zeros((1, 3, 512, 512), np.float32)


def stand_ins(say=None):
    return lambda name: ScratchNet() if name == 'scratch' else FillNet()


def photo(w=1600, h=1000, seed=4):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[:h, :w]
    img = np.stack([80 + 50 * x / w, 90 + 30 * y / h, 110 - 20 * x / w], 2) + rng.normal(0, 3, (h, w, 3))
    img = np.clip(img, 0, 255).astype(np.uint8)
    for (x0, y0, x1, y1) in [(100, 100, 400, 160), (900, 200, 1300, 230), (300, 700, 340, 950),
                             (1200, 650, 1500, 900)]:
        cv2.line(img, (x0, y0), (x1, y1), (250, 250, 250), 3)            # scratches in four places
    for x0, y0 in [(700, 500), (1450, 120), (150, 450)]:
        img[y0:y0 + 3, x0:x0 + 3] = 255                                   # and dust
    return img


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
class Resume(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.src = cls.root / 'frame-v1.png'
        Image.fromarray(photo()).save(cls.src)
        cls.opts = dict(crop=False, protect_faces=False, scratches=True)
        with mock.patch.object(scratches, 'load_nets', stand_ins):
            cls.ref = clean.clean(cls.src, cls.root / 'ref.png', **cls.opts)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def stop_after(self, stage, folder):
        """Run until `stage` is saved, then stop."""
        real = checkpoints.Checkpoints.save

        def save(ck, name, info=None, **arrays):
            real(ck, name, info=info, **arrays)
            if name == stage:
                raise Stop
        with mock.patch.object(scratches, 'load_nets', stand_ins), \
                mock.patch.object(checkpoints.Checkpoints, 'save', save), self.assertRaises(Stop):
            clean.clean(self.src, folder / 'out.png', checkpoints=folder / 'ck', **self.opts)

    def resume(self, folder):
        with mock.patch.object(scratches, 'load_nets', stand_ins):
            return clean.clean(self.src, folder / 'out.png', checkpoints=folder / 'ck', **self.opts)

    def test_the_stand_ins_give_a_real_pass_with_several_windows(self):
        step = [s for s in self.ref['steps'] if s['step'] == 'scratches'][0]
        self.assertGreaterEqual(step['model_calls'], 4)
        self.assertGreater(step['pixels_filled'], 1000)

    def test_a_run_stopped_after_any_stage_resumes_to_the_same_file(self):
        for stage in ('setup', 'dust', 'scratch_probability', 'scratch_mask', 'fill-000000', 'fill-000002'):
            with self.subTest(stage=stage):
                folder = self.root / f'stop-{stage}'
                self.stop_after(stage, folder)
                rec = self.resume(folder)
                self.assertEqual(rec['output_sha256'], self.ref['output_sha256'])
                resumed = [r['stage'] for r in rec['resumed_from_checkpoints']]
                self.assertIn(stage.split('-')[0], resumed)
                self.assertEqual(rec['steps'], self.ref['steps'])

    def test_a_saved_window_that_doesnt_fit_is_worked_out_again(self):
        folder = self.root / 'odd-window'
        self.stop_after('fill-000002', folder)
        path = folder / 'ck' / 'fill-000001.npz'
        with np.load(path) as z:
            data = dict(z)
        meta = json.loads(str(data['_meta']))
        meta['info']['at'][0] += 1                                  # saved for some other window
        data['_meta'] = np.array(json.dumps(meta))
        with open(path, 'wb') as f:
            np.savez(f, **data)
        self.assertEqual(self.resume(folder)['output_sha256'], self.ref['output_sha256'])

    def test_checkpoints_from_another_version_are_ignored(self):
        folder = self.root / 'other-version'
        self.stop_after('scratch_mask', folder)
        mine = checkpoints.made_with()
        with mock.patch.object(checkpoints, 'made_with', lambda: dict(mine, openfilmkit='0.0.1')):
            rec = self.resume(folder)
        self.assertEqual(rec['resumed_from_checkpoints'], [])
        self.assertEqual(rec['output_sha256'], self.ref['output_sha256'])

    def test_checkpoints_cut_short_in_transit_are_worked_out_again(self):
        # Workers pass checkpoints on with rsync --partial, which keeps what arrived of a file under
        # its own name when the connection drops.
        folder = self.root / 'cut-short'
        self.stop_after('fill-000002', folder)
        mask = folder / 'ck' / 'scratch_mask.npz'
        mask.write_bytes(mask.read_bytes()[:mask.stat().st_size // 2])
        (folder / 'ck' / 'fill-000001.npz').write_bytes(b'')
        rec = self.resume(folder)
        self.assertNotIn('scratch_mask', [r['stage'] for r in rec['resumed_from_checkpoints']])
        self.assertEqual(rec['output_sha256'], self.ref['output_sha256'])

    def test_a_stopped_job_resumes_and_tidies_up(self):
        package = jobs.pack_clean(self.src, self.root / 'queue', output='frame-v1-clean-v1.png', **self.opts)
        real = checkpoints.Checkpoints.save

        def save(ck, name, info=None, **arrays):
            real(ck, name, info=info, **arrays)
            if name == 'fill-000001':
                raise Stop
        with mock.patch.object(scratches, 'load_nets', stand_ins), \
                mock.patch.object(checkpoints.Checkpoints, 'save', save), self.assertRaises(Stop):
            jobs.run(package)
        info = jobs.show(package)
        self.assertEqual(info['state'], 'started')
        self.assertIn('scratch_probability', info['checkpoints'])
        with mock.patch.object(scratches, 'load_nets', stand_ins):
            rec = jobs.run(package)
        self.assertEqual(rec['output_sha256'], self.ref['output_sha256'])
        self.assertEqual([r['stage'] for r in rec['resumed_from_checkpoints']],
                         ['setup', 'dust', 'scratch_mask', 'fill'])
        self.assertFalse((package / 'work' / 'checkpoints').exists())


    def test_stopping_a_clean_here_keeps_the_slow_work_for_the_same_command(self):
        from openfilmkit import cli
        cache = self.root / 'cache'
        argv = ['clean', str(self.src), '--no-crop', '--no-faces', '--scratches', '--json']
        real = checkpoints.Checkpoints.save

        def save(ck, name, info=None, **arrays):
            real(ck, name, info=info, **arrays)
            if name == 'scratch_mask':
                raise Stop

        def ofk(*extra):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(argv + list(extra)), 0)
            return json.loads(out.getvalue())[0]
        with mock.patch.dict('os.environ', {'OPENFILMKIT_CACHE': str(cache)}), \
                mock.patch.object(scratches, 'load_nets', stand_ins):
            # OPENFILMKIT_TRACEBACK: the stop goes straight out, as from a process that dies, with no last word.
            with mock.patch.object(checkpoints.Checkpoints, 'save', save), self.assertRaises(Stop), \
                    mock.patch.dict('os.environ', {'OPENFILMKIT_TRACEBACK': '1'}):
                ofk()
            other = ofk('--color')                         # other options: other work, nothing reused
            self.assertEqual(other['resumed_from_checkpoints'], [])
            again = ofk()                                  # the same command picks up where it stopped
        self.assertEqual([r['stage'] for r in again['resumed_from_checkpoints']], ['setup', 'dust', 'scratch_mask'])
        self.assertEqual(again['output_sha256'], self.ref['output_sha256'])
        self.assertEqual(list((cache / 'checkpoints').iterdir()), [])   # tidied up once done


if __name__ == '__main__':
    unittest.main()
