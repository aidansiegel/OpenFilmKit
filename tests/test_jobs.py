"""Job packages: the same work packs to the same package, runs give what clean gives, a run that
stops partway leaves the job unfinished, and nothing is ever half done or overwritten."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np
    from PIL import Image
    from openfilmkit import clean, jobs
except ImportError:
    clean = None
from openfilmkit import cli, roll


def scene(w=600, h=400, seed=3):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[:h, :w]
    base = np.stack([90 + 60 * x / w, 100 + 40 * y / h, 120 - 30 * x / w], 2)
    img = np.clip(base + rng.normal(0, 3, base.shape), 0, 255).astype(np.uint8)
    img[200:203, 300:303] = 255                                   # a speck of dust
    return img


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
class Jobs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / 'photos').mkdir()
        self.src = self.root / 'photos' / 'frame-v1.png'
        Image.fromarray(scene()).save(self.src)
        self.queue = self.root / 'queue'

    def tearDown(self):
        self.temp.cleanup()

    def pack(self, src=None, **options):
        return jobs.pack_clean(src or self.src, self.queue, protect_faces=False, **options)

    def test_the_same_work_packs_to_the_same_package_from_any_folder(self):
        first = self.pack()
        self.assertEqual(self.pack(), first)
        moved = self.root / 'elsewhere' / 'renamed.png'
        moved.parent.mkdir()
        shutil.copyfile(self.src, moved)
        self.assertEqual(self.pack(moved, output='renamed-clean-v1.png').name, first.name)
        self.assertNotEqual(self.pack(color=True).name, first.name)          # other options: other work
        self.assertEqual(sorted(p.name for p in first.iterdir()), ['input', 'job.json'])
        job, about = jobs.read(first)
        self.assertEqual((job['command'], job['input']['sha256']), ('clean', jobs.sha(self.src)))
        self.assertEqual((about['source'], about['output_name']), (str(self.src), 'frame-v1-clean-v1.png'))

    def test_a_job_gives_what_clean_gives_and_a_second_run_does_nothing(self):
        package = self.pack()
        record = jobs.run(package)
        direct = clean.clean(self.src, self.root / 'direct.png', protect_faces=False)
        out = package / 'out' / 'frame-v1-clean-v1.png'
        self.assertEqual(jobs.sha(out), direct['output_sha256'])
        self.assertEqual((record['job']['id'], record['source']), (package.name, str(self.src)))
        recipe = Path(str(out) + '.json')
        stamp = recipe.stat().st_mtime_ns
        self.assertEqual(jobs.run(package), json.loads(recipe.read_text()))
        self.assertEqual(recipe.stat().st_mtime_ns, stamp)
        self.assertEqual(jobs.show(package)['state'], 'done')
        self.assertEqual(list((package / 'work').iterdir()), [])

    def test_a_run_that_stops_partway_leaves_the_job_unfinished(self):
        package = self.pack()
        real = jobs.os.replace

        def stop_before_the_recipe(src, dst):
            if str(dst).endswith('.png.json') and Path(dst).parent.name == 'out':
                raise KeyboardInterrupt
            return real(src, dst)
        with mock.patch.object(jobs.os, 'replace', stop_before_the_recipe), self.assertRaises(KeyboardInterrupt):
            jobs.run(package)
        self.assertTrue((package / 'out' / 'frame-v1-clean-v1.png').exists())   # the image got there...
        self.assertIsNone(jobs.done(package))                                   # ...but the job isn't done
        (package / 'work' / 'run-otherhost-99').mkdir()                         # a machine that died mid-run
        self.assertEqual(jobs.show(package)['state'], 'started')
        jobs.run(package)
        self.assertIsNotNone(jobs.done(package))

    def test_a_job_runs_only_on_the_version_that_packed_it(self):
        package = self.pack()
        with mock.patch.object(jobs, '__version__', '9.9.9'):
            with self.assertRaisesRegex(jobs.JobError, 'packed for OpenFilmKit'):
                jobs.run(package)
        jobs.run(package)
        with mock.patch.object(jobs, '__version__', '9.9.9'):
            self.assertIsNotNone(jobs.run(package))                            # a finished one is just read

    def test_a_changed_package_is_refused(self):
        package = self.pack()
        data = json.loads((package / 'job.json').read_text())
        data['job']['options']['color'] = True
        (package / 'job.json').write_text(json.dumps(data))
        with self.assertRaisesRegex(jobs.JobError, 'changed'):
            jobs.run(package)
        package = self.pack(red_eye=True)
        img = np.asarray(Image.open(self.src)).copy()
        img[0, 0] = 0
        target = package / 'input' / self.src.name
        target.unlink()                                  # the input may be a hard link to the original
        Image.fromarray(img).save(target)
        with self.assertRaisesRegex(jobs.JobError, 'checksum'):
            jobs.run(package)

    def test_collecting_puts_it_beside_the_original_and_never_replaces_another_file(self):
        package = self.pack()
        with self.assertRaisesRegex(jobs.JobError, 'not finished'):
            jobs.collect(package)
        jobs.run(package)
        record = jobs.collect(package)
        dest = self.root / 'photos' / 'frame-v1-clean-v1.png'
        self.assertEqual(record['output'], str(dest))
        self.assertEqual(jobs.sha(dest), record['output_sha256'])
        self.assertEqual(json.loads(Path(str(dest) + '.json').read_text())['output'], str(dest))
        self.assertEqual(jobs.collect(package)['output'], str(dest))          # again: harmless
        other = self.root / 'other'
        other.mkdir()
        (other / 'frame-v1-clean-v1.png').write_bytes(b'something else')
        with self.assertRaisesRegex(jobs.JobError, 'refusing'):
            jobs.collect(package, other)

    def test_a_roll_is_packed_and_run_from_the_command_line(self):
        r = self.root / 'roll-04'
        roll.create(r)
        (r / 'derived').mkdir()
        for name in ('frame-01-v1.png', 'frame-02-v1.png'):
            shutil.copyfile(self.src, r / 'derived' / name)

        def ofk(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main([*argv, '--json'])
            return code, json.loads(out.getvalue())
        code, packed = ofk('clean', str(r), '--no-faces', '--pack', str(self.queue))
        self.assertEqual((code, len(packed)), (0, 2))
        self.assertEqual(packed[0]['id'], packed[1]['id'])                   # identical frames: one job
        code, ran = ofk('job', 'run', *[p['package'] for p in packed])
        self.assertEqual((code, len(ran)), (0, 2))
        code, info = ofk('job', 'show', packed[0]['package'])
        self.assertEqual(info['state'], 'done')
        code, record = ofk('job', 'collect', packed[1]['package'], '--to', str(self.root / 'back'))
        self.assertTrue(Path(record['output']).is_file())


if __name__ == '__main__':
    unittest.main()
