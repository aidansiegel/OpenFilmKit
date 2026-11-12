"""Job packages: the same work packs to the same package, runs give what clean gives, a run that
stops partway leaves the job unfinished, and nothing is ever half done or overwritten."""
import contextlib
import io
import json
import os
import shutil
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

    def test_a_pack_that_fails_partway_leaves_nothing_behind(self):
        """On a full disk, say: the half-made package stayed for good, with its copy of the photo."""
        with mock.patch.object(jobs, '_copy', side_effect=OSError(28, 'No space left on device')):
            with self.assertRaisesRegex(jobs.JobError, 'No space left') as said:
                self.pack()
        self.assertIn(str(self.queue), str(said.exception))                 # where there is no space
        self.assertEqual(list(self.queue.iterdir()), [])
        with mock.patch.object(jobs, '_write_json', side_effect=KeyboardInterrupt):   # stopped as it finished
            with self.assertRaises(KeyboardInterrupt):
                self.pack()
        self.assertEqual(list(self.queue.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.pack().iterdir()), ['input', 'job.json'])

    def test_what_a_killed_pack_left_is_cleared_a_day_later(self):
        killed = self.queue / f".{'ab' * 32}.4242.packing"
        (killed / 'input').mkdir(parents=True)
        (killed / 'input' / 'photo.png').write_bytes(b'half a photo')
        under_way = self.queue / f".{'cd' * 32}.4243.packing"     # another pack, running now
        under_way.mkdir()
        not_ours = self.queue / '.notes.packing'                  # not a name the kit gives: left alone
        not_ours.mkdir()
        for folder in (killed, not_ours):
            os.utime(folder, (time.time() - 2 * 86400,) * 2)
        self.pack()
        self.assertEqual(sorted(p.name for p in self.queue.iterdir() if p.name.startswith('.')),
                         sorted([under_way.name, not_ours.name]))

    def test_a_result_isnt_collected_into_a_folder_that_has_gone(self):
        """The photo's drive not connected, say. On Linux the folder was made again under the mount
        point, on the internal disk, and the job counted as collected."""
        package = self.pack()
        jobs.run(package)
        away = self.root / 'photos-away'
        (self.root / 'photos').rename(away)
        with self.assertRaisesRegex(jobs.JobError, 'not there'):
            jobs.collect(package)
        self.assertFalse((self.root / 'photos').exists())
        self.assertTrue(jobs.done(package))                                  # the result waits in the package
        away.rename(self.root / 'photos')
        self.assertEqual(len(jobs.collect(package)), 1)                      # and is collected once it's back
        self.assertEqual(len(jobs.collect(package, self.root / 'new' / 'folder')), 1)   # one named with --to is made

    def by_hand(self, job=None, about=None, raw=None):
        """A package made by hand, as one from elsewhere can be: its folder named for whatever job it holds."""
        job = {'format': 1, 'command': 'clean', 'output_suffix': '.png', 'input': {'sha256': 'x'}, 'options': {},
               'openfilmkit': '0', 'models': {}} if job is None else job
        d = {'source': str(self.root / 'photos' / 'a.png'), 'output_name': 'a-clean-v1.png'}
        about = dict(d, input_name='a.png', destinations=[d]) if about is None else about
        package = self.root / 'by-hand' / jobs.job_id(job)
        shutil.rmtree(package, ignore_errors=True)
        (package / 'out').mkdir(parents=True)
        (package / 'job.json').write_text(raw if raw is not None else json.dumps({'job': job, 'about': about}))
        return package

    def test_a_package_made_elsewhere_cant_have_another_file_replaced(self):
        """collect writes where a package's job.json says. One made by hand, its output a name with no
        suffix, had any .json file on the collecting machine replaced by its own."""
        victim = self.root / 'victim'
        victim.mkdir()
        (victim / 'settings.json').write_text('{"mine": true}')
        job = {'format': 1, 'command': 'clean', 'output_suffix': '', 'input': {'sha256': 'x'}, 'options': {},
               'openfilmkit': '0', 'models': {}}
        d = {'source': str(victim / 'x'), 'output_name': 'settings'}
        package = self.by_hand(job, dict(d, input_name='a.png', destinations=[d]))
        (package / 'out' / 'settings').write_bytes(b'anything')
        (package / 'out' / 'settings.json').write_text(json.dumps({'output_sha256': jobs.sha(package / 'out' / 'settings')}))
        with self.assertRaisesRegex(jobs.JobError, 'kind'):
            jobs.collect(package)
        self.assertEqual((victim / 'settings.json').read_text(), '{"mine": true}')
        self.assertEqual(jobs.SUFFIXES, clean.OUTPUTS)

    def test_a_job_file_of_the_wrong_shape_is_not_a_package(self):
        good = {'format': 1, 'command': 'clean', 'output_suffix': '.png', 'input': {'sha256': 'x'}, 'options': {},
                'openfilmkit': '0', 'models': {}}
        d = {'source': '/photos/a.png', 'output_name': 'a-clean-v1.png'}
        for job, about, raw in ((None, None, '[1, 2]'), (None, None, '{"job": [], "about": {}}'), (None, None, 'null'),
                                (good, dict(d, input_name='a.png', destinations=None), None),
                                (good, dict(d, input_name='a.png', destinations=[{'output_name': 'a-clean-v1.png'}]), None),
                                (good, dict(d, input_name='a.png', destinations=['x']), None),
                                (dict(good, options=None), None, None), (dict(good, input='x'), None, None),
                                ({k: v for k, v in good.items() if k != 'openfilmkit'}, None, None)):
            package = self.by_hand(job, about, raw)
            for use in (jobs.read, jobs.done, jobs.run, jobs.collect, jobs.show):
                with self.assertRaises(jobs.JobError, msg=(raw, job, about, use.__name__)):
                    use(package)
        package = self.pack()
        shutil.rmtree(package / 'input')
        os.symlink(self.root / 'photos', package / 'input')                  # a link where its files should be
        with self.assertRaisesRegex(jobs.JobError, 'link'):
            jobs.run(package)

    def test_a_recipe_or_a_file_put_there_meanwhile_is_never_replaced(self):
        package = self.pack()
        jobs.run(package)
        dest = self.src.with_name('frame-v1-clean-v1.png')
        Path(str(dest) + '.json').write_text('{"approved": true}')           # notes on a copy that was moved away
        (rec,) = jobs.collect(package)
        self.assertEqual(Path(rec['output']).name, 'frame-v1-clean-v2.png')  # so this copy takes the next number
        self.assertEqual(Path(str(dest) + '.json').read_text(), '{"approved": true}')
        recipe = Path(rec['output'] + '.json')
        recipe.write_text(json.dumps(dict(json.loads(recipe.read_text()), approved=True)))
        jobs.collect(package)                                                # collecting again leaves what was added
        self.assertTrue(json.loads(recipe.read_text())['approved'])
        inbox, copy = self.root / 'inbox', shutil.copyfile

        def raced(src, dst):   # another file takes the name between the look and the placing
            copy(src, dst)
            (inbox / 'frame-v1-clean-v1.png').write_bytes(b'theirs')
        with mock.patch.object(jobs.shutil, 'copyfile', raced), self.assertRaisesRegex(jobs.JobError, 'appeared'):
            jobs.collect(package, inbox)
        self.assertEqual({p.name: p.read_bytes() for p in inbox.iterdir()}, {'frame-v1-clean-v1.png': b'theirs'})

    def test_a_result_that_couldnt_be_written_may_be_tried_again(self):
        package = self.pack()
        with mock.patch.object(clean, 'save', side_effect=OSError(28, 'No space left on device')):
            with self.assertRaisesRegex(jobs.JobError, 'No space left') as failed:
                jobs.run(package)
        self.assertNotIsInstance(failed.exception, jobs.AlwaysFails)        # a full disk passes; a photo that can't be read doesn't
        self.assertEqual(sorted(p.name for p in package.iterdir()), ['input', 'job.json', 'work'])
        self.assertEqual(jobs.run(package)['output_sha256'], jobs.sha(package / 'out' / 'frame-v1-clean-v1.png'))

    def test_only_checkpoint_folders_are_cleared_from_the_cache(self):
        cache = self.root / 'somewhere'
        for name in ('my-model-run', 'ab' * 32):
            (cache / 'checkpoints' / name).mkdir(parents=True)
            os.utime(cache / 'checkpoints' / name, (time.time() - 30 * 86400,) * 2)
        with mock.patch.dict(os.environ, {'OPENFILMKIT_CACHE': str(cache)}):
            jobs.local_checkpoints(jobs.clean_job(self.src)[0])
        self.assertEqual([p.name for p in (cache / 'checkpoints').iterdir()], ['my-model-run'])

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

    def test_the_same_photo_in_two_folders_is_one_job_and_both_get_the_result(self):
        twin = self.root / 'copies' / 'frame-v1-twin.png'
        twin.parent.mkdir()
        shutil.copyfile(self.src, twin)
        a, b = self.pack(), self.pack(twin)
        self.assertEqual(a, b)
        jobs.run(a)
        records = jobs.collect(a)
        self.assertEqual(sorted(r['output'] for r in records),
                         sorted([str(self.root / 'photos' / 'frame-v1-clean-v1.png'),
                                 str(self.root / 'copies' / 'frame-v1-twin-clean-v1.png')]))
        self.assertEqual({r['source'] for r in records}, {str(self.src), str(twin)})
        for r in records:
            self.assertEqual(jobs.sha(r['output']), r['output_sha256'])

    def test_a_package_naming_files_outside_itself_is_refused(self):
        package = self.pack()
        data = json.loads((package / 'job.json').read_text())
        for field, bad in (('input_name', '../../etc/passwd'), ('output_name', '../escape.png')):
            tampered = json.loads(json.dumps(data))
            tampered['about'][field] = bad
            (package / 'job.json').write_text(json.dumps(tampered))
            with self.assertRaisesRegex(jobs.JobError, 'outside'):
                jobs.read(package)

    def test_a_job_runs_only_on_the_build_that_packed_it(self):
        package = self.pack()
        with mock.patch.object(jobs, 'build', lambda: '0' * 16):              # same version, other code
            with self.assertRaisesRegex(jobs.JobError, 'other OpenFilmKit'):
                jobs.run(package)

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
        (record,) = jobs.collect(package)
        dest = self.root / 'photos' / 'frame-v1-clean-v1.png'
        self.assertEqual(record['output'], str(dest))
        self.assertEqual(jobs.sha(dest), record['output_sha256'])
        self.assertEqual(json.loads(Path(str(dest) + '.json').read_text())['output'], str(dest))
        self.assertEqual(jobs.collect(package)[0]['output'], str(dest))       # again: harmless
        chosen = self.pack(output='best.png')
        jobs.run(chosen)
        (self.root / 'photos' / 'best.png').write_bytes(b'something else')
        with self.assertRaisesRegex(jobs.JobError, 'refusing'):
            jobs.collect(chosen)

    def test_a_numbered_copy_whose_number_was_taken_meanwhile_gets_the_next(self):
        # The number is picked when the job is packed; by the time it's collected another copy
        # (the same photo cleaned with other options, say) can have it.
        package = self.pack()
        jobs.run(package)
        taken = self.root / 'photos' / 'frame-v1-clean-v1.png'
        taken.write_bytes(b'something else')
        (record,) = jobs.collect(package)
        dest = self.root / 'photos' / 'frame-v1-clean-v2.png'
        self.assertEqual(record['output'], str(dest))
        self.assertEqual(jobs.sha(dest), record['output_sha256'])
        self.assertEqual(json.loads(Path(str(dest) + '.json').read_text())['output'], str(dest))
        self.assertEqual(taken.read_bytes(), b'something else')
        self.assertEqual(jobs.collect(package)[0]['output'], str(dest))       # again: harmless, and no v3
        self.assertFalse((self.root / 'photos' / 'frame-v1-clean-v3.png').exists())

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
        self.assertEqual(packed[0]['id'], packed[1]['id'])                   # identical frames: one job...
        code, ran = ofk('job', 'run', *[p['package'] for p in packed])
        self.assertEqual((code, len(ran)), (0, 2))
        code, info = ofk('job', 'show', packed[0]['package'])
        self.assertEqual(info['state'], 'done')
        code, records = ofk('job', 'collect', packed[1]['package'])
        self.assertEqual(sorted(Path(r['output']).name for r in records),    # ...and both frames get it
                         ['frame-01-v1-clean-v1.png', 'frame-02-v1-clean-v1.png'])
        self.assertTrue(all(Path(r['output']).is_file() for r in records))


if __name__ == '__main__':
    unittest.main()
