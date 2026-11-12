"""The job queue: workers claim jobs, keep off each other's, pick up stopped work from each
other's checkpoints, and collecting brings back only results that check out.

The queue here is a local folder (rsync copies to and from it as it would over SSH) and the
workers run in this process, with stand-ins for the scratch pass's networks where it's used.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / 'tests'))

try:
    import numpy as np
    from PIL import Image
    from openfilmkit import clean, jobs, queues
    from openfilmkit.clean import checkpoints, scratches
except ImportError:
    clean = None
else:
    from test_checkpoints import Stop, photo, stand_ins
from openfilmkit import cli


def inproc(package, beat, every):
    jobs.run(package)


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
@unittest.skipIf(not shutil.which('rsync'), 'needs rsync')
class Queue(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.queue = self.root / 'queue'
        (self.root / 'config').mkdir()
        (self.root / 'config' / 'hosts.toml').write_text(f'[queues.home]\nqueue = "{self.queue}"\n')
        self.env = mock.patch.dict(os.environ, {'OPENFILMKIT_CONFIG': str(self.root / 'config'),
                                                'OPENFILMKIT_CACHE': str(self.root / 'cache')})
        self.env.start()
        self.spare = mock.patch.object(queues, 'SPARE', 0)   # however full this machine's disk is
        self.spare.start()
        (self.root / 'photos').mkdir()
        self.photos = []
        for n, seed in ((1, 3), (2, 5)):
            rng = np.random.default_rng(seed)
            img = np.clip(120 + rng.normal(0, 3, (300, 400, 3)), 0, 255).astype(np.uint8)
            img[150:153, 200:203] = 255
            path = self.root / 'photos' / f'frame-0{n}-v1.png'
            Image.fromarray(img).save(path)
            self.photos.append(path)
        self.q = queues.queue_settings('home')

    def tearDown(self):
        self.spare.stop()
        self.env.stop()
        self.temp.cleanup()

    def worker(self, name, run=inproc, now=None, **settings):
        s = queues.worker_settings(queue=str(self.queue), name=name, state=str(self.root / f'state-{name}'),
                                   settle=0, **settings)
        return queues.Worker(s, run=run, now=now or time.time, say=lambda *a: None)

    def ofk(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main([*argv, '--json'])
        return code, json.loads(out.getvalue())

    def queue_photos(self, *extra):
        code, packed = self.ofk('clean', *map(str, self.photos), '--no-faces', '--queue', 'home', *extra)
        self.assertEqual(code, 0, packed)
        return [p['id'] for p in packed]

    def states(self):
        return [row['state'] for row in queues.status(self.q)['jobs']]

    def test_queued_work_is_done_by_a_worker_and_collected(self):
        ids = self.queue_photos()
        self.assertEqual(self.states(), ['waiting', 'waiting'])
        w = self.worker('w1')
        self.assertEqual(sorted([w.once(), w.once()]), sorted(ids))
        self.assertIsNone(w.once())                                         # nothing left
        info = queues.status(self.q)
        self.assertEqual([(r['state'], r['worker']) for r in info['jobs']], [('done', 'w1')] * 2)
        self.assertEqual(info['workers'][0]['name'], 'w1')
        code, recs = self.ofk('queue', 'collect')
        self.assertEqual((code, len(recs)), (0, 2))
        for photo, rec in zip(self.photos, sorted(recs, key=lambda r: r['source'])):
            direct = clean.clean(photo, self.root / f'direct-{photo.name}', protect_faces=False)
            self.assertEqual(rec['output'], str(photo.with_name(photo.stem + '-clean-v1.png')))
            self.assertEqual(rec['output_sha256'], direct['output_sha256'])
        self.assertEqual(list((self.queue / 'jobs').iterdir()), [])
        self.assertEqual(list((self.queue / 'drop' / 'w1' / 'results').iterdir()), [])
        self.assertEqual(queues.status(self.q)['jobs'], [])

    def test_a_worker_runs_each_job_in_its_own_process_and_keeps_its_claim_alive(self):
        self.queue_photos()
        beats = []
        w = self.worker('w1', renew=0.05, check=0.05)
        real = w._lease

        def renewed(jid, claimed=None):
            beats.append(jid)
            real(jid, claimed)
        w._lease = renewed
        with mock.patch.dict(os.environ, {'PYTHONPATH': str(ROOT)}):
            w.run = queues.run_in_subprocess
            jid = w.once()
        self.assertGreater(beats.count(jid), 1)                             # claimed, then renewed
        self.assertEqual([r['state'] for r in queues.status(self.q)['jobs'] if r['id'] == jid], ['done'])

    def test_a_batch_that_wouldnt_fit_on_the_queues_machine_is_refused(self):
        with mock.patch.object(queues, '_free_bytes', lambda q: 1000):
            code, err = self.ofk('clean', *map(str, self.photos), '--no-faces', '--queue', 'home')
        self.assertNotEqual(code, 0)
        self.assertIn('queue fewer at a time', err['error'])
        self.assertFalse((self.queue / 'jobs').exists() and any((self.queue / 'jobs').iterdir()))

    def test_the_same_photo_in_two_folders_comes_back_to_both(self):
        twin = self.root / 'elsewhere' / 'twin.png'
        twin.parent.mkdir()
        shutil.copyfile(self.photos[0], twin)
        code, packed = self.ofk('clean', str(self.photos[0]), str(twin), '--no-faces', '--queue', 'home')
        self.assertEqual(packed[0]['id'], packed[1]['id'])
        self.worker('w1').once()
        recs = queues.collect(self.q, say=lambda *a: None)
        self.assertEqual(sorted(r['output'] for r in recs),
                         sorted([str(self.photos[0].with_name('frame-01-v1-clean-v1.png')),
                                 str(twin.with_name('twin-clean-v1.png'))]))

    def test_where_collecting_may_not_delete_a_workers_files_the_worker_clears_them(self):
        self.queue_photos()
        w1 = self.worker('w1')
        jid = w1.once()
        real = queues._remote

        def no_rights_in_drop(q, *command, **kw):   # as on a server: the drop folders belong to the workers
            if command[:1] == ('rm',) and any('/drop/' in c for c in command):
                return False
            return real(q, *command, **kw)
        with mock.patch.object(queues, '_remote', no_rights_in_drop):
            queues.collect(self.q, say=lambda *a: None)
        self.assertTrue((self.queue / 'drop' / 'w1' / 'results' / jid).exists())
        w1.once()                                                          # the next look tidies up
        self.assertEqual([p for p in (self.queue / 'drop' / 'w1' / 'results' / jid).rglob('*') if p.is_file()], [])

    def test_a_bad_result_collecting_may_not_delete_is_turned_down_and_redone(self):
        self.queue_photos()
        w1 = self.worker('w1')
        jid = w1.once()
        out = self.queue / 'drop' / 'w1' / 'results' / jid / 'out'
        next(p for p in out.iterdir() if p.suffix == '.png').write_bytes(b'tampered')
        with mock.patch.object(queues, '_remote', lambda q, *c, **kw: False if c[:1] == ('rm',) else True):
            queues.collect(self.q, say=lambda *a: None)
        self.assertTrue((self.queue / 'jobs' / jid / 'rejected.json').exists())
        self.assertIn(jid, w1.candidates(w1.view()))                        # free to run again
        self.assertEqual(w1.once(), jid)                                    # tidies the bad one, redoes it
        rec = next(r for r in queues.collect(self.q, say=lambda *a: None) if r.get('job', {}).get('id') == jid)
        self.assertEqual(jobs.sha(rec['output']), rec['output_sha256'])

    def test_a_worker_makes_way_for_another_program_and_comes_back(self):
        self.queue_photos()
        with mock.patch.object(queues, 'busy', lambda units, mb: 'photo-worker.service'):
            self.assertIsNone(self.worker('w1', yield_to='photo-worker.service').once())
        calls = []

        def runner(package, beat, every):
            beat()
            jobs.run(package)

        def busy_once(units, mb):     # free when the job is picked, busy at its first check
            calls.append(1)
            return 'photo-worker.service' if len(calls) == 2 else None
        w = self.worker('w1', run=runner, yield_to='photo-worker.service')
        with mock.patch.object(queues, 'busy', busy_once):
            jid = w.once()
        self.assertNotIn('done', [r['state'] for r in queues.status(self.q)['jobs'] if r['id'] == jid])
        self.assertFalse((self.queue / 'drop' / 'w1' / 'leases' / f'{jid}.json').exists())   # claim let go
        self.assertEqual(w.once(), jid)                                     # and taken up again later

    def test_a_worker_takes_no_job_while_busy_when_says_so_and_stops_one_if_it_starts(self):
        self.queue_photos()
        self.assertIsNone(self.worker('w1', busy_when='true').once())        # a capture under way
        flag = self.root / 'recording'

        started = []

        def runner(package, beat, every):
            if not started:
                started.append(flag.touch())                                # a capture starts mid-job
            beat()
            jobs.run(package)
        w = self.worker('w1', run=runner, busy_when=[f'test -e "{flag}"'])
        jid = w.once()
        self.assertFalse((self.queue / 'drop' / 'w1' / 'results' / jid).exists())
        self.assertFalse((self.queue / 'drop' / 'w1' / 'leases' / f'{jid}.json').exists())   # claim let go
        flag.unlink()
        self.assertEqual(w.once(), jid)                                     # done once it's over
        self.assertIn(jid, [r['id'] for r in queues.status(self.q)['jobs'] if r['state'] == 'done'])

    def test_a_check_that_hangs_counts_as_busy(self):
        with mock.patch.object(queues.subprocess, 'run', side_effect=subprocess.TimeoutExpired('x', 10)):
            self.assertEqual(queues.busy_command(['curl http://localhost/status.json']), 'curl http://localhost/status.json')
        self.assertIsNone(queues.busy_command(['false']))

    def test_a_worker_frozen_for_hours_carries_on_after_the_thaw(self):
        self.queue_photos()
        clock = [time.time()]
        w = self.worker('w1', now=lambda: clock[0])
        real_push, tries = w._push, []

        def flaky_push():                                                   # the first beat after the thaw can't reach the queue
            tries.append(1)
            if len(tries) <= 2:
                w._pushed = False
                return False
            return real_push()

        def runner(package, beat, every):
            clock[0] += 3 * 3600                                            # frozen for three hours
            w._push = flaky_push
            beat()                                                          # back: a failed renewal isn't the end
            clock[0] += queues.RENEW
            beat()                                                          # and the next one gets through
            w._push = real_push
            jobs.run(package)
        w.run = runner
        jid = w.once()
        self.assertGreater(len(tries), 2)
        self.assertTrue(any(r['id'] == jid and r['state'] == 'done' for r in queues.status(self.q)['jobs']))

    def test_a_worker_frozen_while_another_took_its_job_backs_off(self):
        self.queue_photos()
        clock = [time.time()]
        w1 = self.worker('w1', now=lambda: clock[0])

        def runner(package, beat, every):
            clock[0] += 3 * 3600
            w2 = self.worker('w2', now=lambda: clock[0])                    # took the lapsed job meanwhile
            w2._lease(jid_box[0], claimed=clock[0] - 60)
            w2._push()
            beat()
            jobs.run(package)
        jid_box = [w1.candidates(w1.view())[0]]
        w1.run = runner
        self.assertEqual(w1.once(), jid_box[0])
        self.assertFalse((self.queue / 'drop' / 'w1' / 'results' / jid_box[0]).exists())

    def test_a_worker_that_cant_renew_its_claim_stops_without_publishing(self):
        self.queue_photos()
        w = self.worker('w1', renew=0, lease=0)

        def runner(package, beat, every):
            w._push = lambda: setattr(w, '_pushed', False) or False         # the network goes away
            beat()
            jobs.run(package)
        w.run = runner
        jid = w.once()
        self.assertFalse((self.queue / 'drop' / 'w1' / 'results' / jid).exists())

    def test_jobs_packed_elsewhere_go_only_where_youre_told(self):
        ids = self.queue_photos()
        shutil.rmtree(queues.outbox(self.q))                                # as if packed on another machine
        w = self.worker('w1')
        w.once(), w.once()
        recs = queues.collect(self.q, say=lambda *a: None)
        self.assertTrue(all('--to' in r['error'] for r in recs))
        recs = queues.collect(self.q, to=self.root / 'inbox', say=lambda *a: None)
        self.assertEqual(sorted(Path(r['output']).parent.name for r in recs), ['inbox', 'inbox'])
        self.assertEqual(len(ids), 2)

    def test_a_live_claim_keeps_other_workers_off_and_a_lapsed_one_doesnt(self):
        self.queue_photos()
        t = time.time()
        w1 = self.worker('w1', now=lambda: t)
        first = w1.candidates(w1.view())[0]
        self.assertTrue(w1.claim(first))
        w2 = self.worker('w2', now=lambda: t + 60)
        self.assertNotIn(first, w2.candidates(w2.view()))
        later = self.worker('w2', now=lambda: t + queues.LEASE + 1)        # w1 stopped renewing
        self.assertIn(first, later.candidates(later.view()))

    def test_when_two_workers_claim_at_once_the_earlier_claim_wins(self):
        self.queue_photos()
        t = time.time()
        w1, w2 = self.worker('w1', now=lambda: t), self.worker('w2', now=lambda: t + 1)
        jid = w1.candidates(w1.view())[0]
        w1._lease(jid, claimed=t)                  # w1's claim lands first...
        w1._push()
        self.assertFalse(w2.claim(jid))            # ...so w2, a second later, backs off
        self.assertFalse((self.queue / 'drop' / 'w2' / 'leases' / f'{jid}.json').exists())
        self.assertTrue(w1.claim(jid))

    def test_a_stopped_job_is_finished_by_another_worker_from_its_checkpoints(self):
        src = self.root / 'photos' / 'scratched.png'
        Image.fromarray(photo()).save(src)
        opts = dict(crop=False, protect_faces=False, scratches=True)
        with mock.patch.object(scratches, 'load_nets', stand_ins):
            ref = clean.clean(src, self.root / 'ref.png', **opts)
            queues.submit(self.q, [jobs.pack_clean(src, queues.outbox(self.q), **opts)], say=lambda *a: None)
            real = checkpoints.Checkpoints.save

            def save(ck, name, info=None, **arrays):
                real(ck, name, info=info, **arrays)
                if name == 'fill-000001':
                    raise Stop
            with mock.patch.object(checkpoints.Checkpoints, 'save', save), self.assertRaises(Stop):
                self.worker('w1').once()
            self.assertEqual(self.states(), ['stopped'])
            self.assertTrue((self.queue / 'drop' / 'w1' / 'checkpoints').is_dir())
            self.worker('w2').once()
        (rec,) = queues.collect(self.q, say=lambda *a: None)
        self.assertEqual(rec['output_sha256'], ref['output_sha256'])
        self.assertEqual([r['stage'] for r in rec['resumed_from_checkpoints']], ['scratch_probability', 'fill'])

    def test_jobs_a_worker_cannot_do_are_left_for_others(self):
        self.queue_photos()
        with mock.patch.object(queues, '__version__', '9.9.9'):
            self.assertEqual(self.worker('old').candidates(self.worker('old').view()), [])
        small = self.worker('small', memory_gb=0.1)
        self.assertEqual(small.candidates(small.view()), [])
        hour = time.localtime().tm_hour
        asleep = self.worker('night', hours=f'{(hour + 1) % 24}-{(hour + 2) % 24}')
        self.assertIsNone(asleep.once())
        self.assertEqual(self.states(), ['waiting', 'waiting'])

    def test_a_result_that_doesnt_check_out_is_deleted_and_the_job_freed(self):
        self.queue_photos()
        w1 = self.worker('w1')
        jid = w1.once()
        out = self.queue / 'drop' / 'w1' / 'results' / jid / 'out'
        image = next(p for p in out.iterdir() if p.suffix == '.png')
        image.write_bytes(b'not what the recipe says')
        recs = queues.collect(self.q, say=lambda *a: None)
        self.assertIn('error', next(r for r in recs if r.get('id') == jid))
        self.assertFalse(out.exists())
        self.assertIn(jid, self.worker('w2').candidates(self.worker('w2').view()))

    def test_a_failed_job_is_reported_and_not_retried_by_the_same_worker(self):
        self.queue_photos()

        def broken(package, beat, every):
            raise jobs.JobError('out of film')
        w1 = self.worker('w1', run=broken)
        jid = w1.once()
        rows = {r['id']: r for r in queues.status(self.q)['jobs']}
        self.assertEqual((rows[jid]['state'], rows[jid]['error']), ('failed', 'out of film'))
        self.assertNotIn(jid, w1.candidates(w1.view()))
        self.assertIn(jid, self.worker('w2').candidates(self.worker('w2').view()))

    def test_a_job_that_fails_in_its_own_process_says_why(self):
        bad = self.root / 'photos' / 'broken.png'
        bad.write_bytes(b'not a picture')
        queues.submit(self.q, [jobs.pack_clean(bad, queues.outbox(self.q), protect_faces=False)], say=lambda *a: None)
        w = self.worker('w1', run=queues.run_in_subprocess)
        with mock.patch.dict(os.environ, {'PYTHONPATH': str(ROOT)}):
            jid = w.once()
        (row,) = queues.status(self.q)['jobs']
        self.assertEqual(row['state'], 'failed')
        self.assertIn('cannot read image', row['error'])
        self.assertFalse((w.state / 'jobs' / jid).exists())               # not tried here again: its copy goes

    def test_a_worker_clears_its_own_copies_of_a_job_once_it_is_gone(self):
        # A job it failed (or was stopped on) that another worker did: once collected, a copy of its
        # files left here was pushed back to its drop folder on every look.
        self.queue_photos()

        def broken(package, beat, every):
            raise jobs.JobError('out of film')
        w1 = self.worker('w1', run=broken)
        jid = w1.once()
        stopped = w1.mirror / 'checkpoints' / jid
        stopped.mkdir(parents=True)
        (stopped / 'fill-000000.npz').write_bytes(b'slow work')          # as a stopped run leaves it
        w1.publish()
        w2 = self.worker('w2')
        while w2.once():
            pass
        queues.collect(self.q, say=lambda *a: None)
        w1.once()
        w1.publish()
        drop = self.queue / 'drop' / 'w1'
        self.assertEqual([p for kind in ('failed', 'checkpoints') for p in (drop / kind).rglob('*') if p.is_file()], [])
        self.assertFalse((w1.mirror / 'failed' / jid).exists() or stopped.exists())

    def test_a_result_whose_recipe_was_cut_short_is_turned_down_and_the_rest_collected(self):
        ids = self.queue_photos()
        w = self.worker('w1')
        w.once(), w.once()
        bad = sorted(ids)[0]
        recipe = next((self.queue / 'drop' / 'w1' / 'results' / bad / 'out').glob('*.json'))
        recipe.write_text(recipe.read_text()[:200])                         # rsync --partial keeps what arrived
        recs = queues.collect(self.q, say=lambda *a: None)
        self.assertIn('error', next(r for r in recs if r.get('id') == bad))
        self.assertEqual(len([r for r in recs if 'error' not in r]), 1)
        self.assertIn(bad, w.candidates(w.view()))                          # free to run again

    def test_results_are_left_alone_when_their_jobs_description_cant_be_fetched(self):
        self.queue_photos()
        shutil.rmtree(queues.outbox(self.q))                                # packed on another machine
        w = self.worker('w1')
        w.once(), w.once()
        real = queues._rsync

        def no_job_json(ssh_command, *args, quiet=False):                    # the connection drops just then
            if any(not a.startswith('-') and a.endswith('/job.json') for a in map(str, args)):
                return False
            return real(ssh_command, *args, quiet=quiet)
        with mock.patch.object(queues, '_rsync', no_job_json):
            recs = queues.collect(self.q, to=self.root / 'inbox', say=lambda *a: None)
        self.assertEqual([('error' in r) for r in recs], [True, True])
        self.assertEqual(self.states(), ['done', 'done'])
        recs = queues.collect(self.q, to=self.root / 'inbox', say=lambda *a: None)
        self.assertEqual(len([r for r in recs if 'error' not in r]), 2)


if __name__ == '__main__':
    unittest.main()
