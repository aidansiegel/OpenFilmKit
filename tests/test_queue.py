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

    def fake_rsync(self, body):
        """A stand-in rsync first on PATH: `body` (sh) runs first, with $last the destination."""
        fake = self.root / 'bin'
        fake.mkdir(exist_ok=True)
        (fake / 'rsync').write_text(f'#!/bin/sh\nfor last; do :; done\n{body}\nexec "{shutil.which("rsync")}" "$@"\n')
        (fake / 'rsync').chmod(0o755)
        return mock.patch.dict(os.environ, {'PATH': f'{fake}{os.pathsep}{os.environ["PATH"]}'})

    @unittest.skipIf(os.name == 'nt', 'the stand-in rsync is a shell script')
    def test_a_cache_that_cant_take_the_queues_permissions_still_works(self):
        """The queue's folders are setgid (2750 on manhattan). rsync -p can't copy that onto a folder
        whose group isn't one of yours (a cache on another drive, or under /tmp on a Mac), and every
        look at the queue said "couldn't read the queue" and no more. Seen with macOS's openrsync,
        which sets folders' modes with or without -p. What is fetched now loses that bit.
        The stand-in refuses, as openrsync does there, unless the bit is cleared."""
        refuse = (f'case "$last" in "{self.root}/cache"*|"{self.root}/state-"*)\n'
                  '  case " $* " in *" --chmod=g-s "*|*" --server "*) ;; *)\n'   # rsync's own other end: let be
                  '    echo "rsync(1): error: drop: fchmodat (1) 1: Operation not permitted" >&2; exit 23;;\n'
                  '  esac;;\nesac')
        with self.fake_rsync(refuse):
            ids = self.queue_photos()
            self.assertEqual(self.states(), ['waiting', 'waiting'])
            w = self.worker('w1')
            self.assertEqual(sorted([w.once(), w.once()]), sorted(ids))
            self.assertEqual(self.states(), ['done', 'done'])
            code, recs = self.ofk('queue', 'collect')
            self.assertEqual((code, len(recs)), (0, 2))

    @unittest.skipIf(os.name == 'nt', 'the stand-in rsync is a shell script')
    def test_a_queue_that_cant_be_read_says_why(self):
        with self.fake_rsync('echo "rsync: connection unexpectedly closed" >&2; exit 12'):
            with self.assertRaises(queues.QueueError) as e:
                queues.status(self.q)
        self.assertIn('connection unexpectedly closed', str(e.exception))

    def test_a_result_stays_in_the_queue_while_its_photos_folder_is_away(self):
        self.queue_photos()
        w = self.worker('w1')
        w.once(), w.once()
        away = self.root / 'photos-away'
        (self.root / 'photos').rename(away)                                 # the photos' drive, unplugged
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['queue', 'collect']), 1)
        self.assertEqual(err.getvalue().count('not there'), 2)              # said, not only put in the JSON
        code, recs = self.ofk('queue', 'collect')
        self.assertEqual((code, ['not there' in r.get('error', '') for r in recs]), (1, [True, True]))
        self.assertFalse((self.root / 'photos').exists())
        self.assertEqual(self.states(), ['done', 'done'])
        away.rename(self.root / 'photos')
        code, recs = self.ofk('queue', 'collect')
        self.assertEqual((code, len(recs), queues.status(self.q)['jobs']), (0, 2, []))

    def test_working_hours_are_read_when_the_worker_starts(self):
        """"10pm-7am" or "22" raised an error at every look at the queue, after the worker had
        reported in: a service restarting every minute that `queue status` showed as fine."""
        def at(hour):
            return time.mktime((2026, 11, 12, hour, 30, 0, 0, 0, -1))
        for hours, hour, inside in (('22-7', 23, True), ('22-7', 12, False), ('10pm-7am', 23, True),
                                    ('10pm-7am', 6, True), ('10pm-7am', 8, False), ('9am-5pm', 12, True),
                                    ('9am-5pm', 17, False), ('12am-6am', 0, True), ('12pm-1pm', 12, True),
                                    ('10 PM - 7 AM', 2, True), ('0-24', 3, True), (None, 3, True), ('', 3, True)):
            self.assertEqual(queues.in_hours(hours, at(hour)), inside, (hours, hour))
        for wrong in ('22', 'night', '25-3', '13pm-2am', '0am-5am', '7-7', '-7', '22-', '10:30pm-7am', '22-7-3', 22):
            with self.assertRaisesRegex(queues.QueueError, 'hours'):
                queues.worker_settings(queue=str(self.queue), name='w1', hours=wrong)
        code, said = self.ofk('worker', '--queue', str(self.queue), '--hours', '22', '--once')
        self.assertEqual(code, 1)
        self.assertIn('10pm-7am', said['error'])                            # how to write them
        self.assertFalse((self.queue / 'drop').exists())                    # and it hasn't reported in as if at work

    def test_worker_settings_of_the_wrong_kind_are_said_too(self):
        hosts = self.root / 'config' / 'hosts.toml'
        for line, key in (('memory_gb = "lots"', 'memory_gb'), ('memory_gb = 0', 'memory_gb'), ('poll = "often"', 'poll'),
                          ('lease = -5', 'lease'), ('busy_when = 5', 'busy_when'), ('yield_to = [1, 2]', 'yield_to'),
                          ('name = 7', 'name'), ('ssh_command = ["ssh"]', 'ssh_command')):
            hosts.write_text(f'[worker]\nqueue = "{self.queue}"\n{line}\n')
            with self.assertRaisesRegex(queues.QueueError, key):
                queues.worker_settings()
        hosts.write_text(f'[worker]\nqueue = "{self.queue}"\nmemory_gb = "1.5"\nbusy_when = "true"\nlater = "ignored"\n')
        s = queues.worker_settings()
        self.assertEqual((s['memory_gb'], s['busy_when']), (1.5, ['true']))

    def test_status_says_when_a_worker_has_gone_quiet_and_why_a_job_waits(self):
        self.queue_photos()
        waits = lambda info: [row.get('why_waiting') for row in info['jobs']]
        self.assertEqual(waits(queues.status(self.q)), ['no worker has reported to this queue'] * 2)
        small = self.worker('small', memory_gb=0.1)
        self.assertIsNone(small.once())                                     # too big for it
        info = queues.status(self.q)
        self.assertTrue(all('small takes jobs up to 0.1 GB' in why for why in waits(info)), waits(info))
        with mock.patch.object(jobs, 'build', lambda: 'another-build'):
            self.worker('old').publish()
        info = queues.status(self.q)
        self.assertTrue(all('old runs another build' in why and 'small' in why for why in waits(info)), waits(info))
        able = self.worker('able')
        able.publish()
        info = queues.status(self.q)
        self.assertEqual((waits(info), [w['silent'] for w in info['workers']]), ([None, None], [False] * 3))
        later = time.time() + 37 * 3600 + 49 * 60                           # as portable was, on Nov 11
        info = queues.status(self.q, now=later)
        self.assertEqual([w['silent'] for w in info['workers']], [True] * 3)
        self.assertTrue(all('able has not reported for 37 hours' in why for why in waits(info)), waits(info))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(queues, '_now', lambda: later):
            self.assertEqual(cli.main(['queue', 'status']), 0)
        said = out.getvalue()
        self.assertIn('not seen for 37 hours', said)
        self.assertIn('no worker can take it now', said)
        self.assertNotIn(' s ago', said)

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

    def test_a_result_that_couldnt_be_fetched_is_left_for_the_next_collect(self):
        """A dropped connection was taken for a result that didn't check out: a good result was
        deleted, and the worker did the job again."""
        self.queue_photos()
        w = self.worker('w1')
        w.once(), w.once()
        real = queues._rsync

        def dropped(ssh_command, *args, **kw):
            return False if '/results/' in args[-2] else real(ssh_command, *args, **kw)
        with mock.patch.object(queues, '_rsync', dropped):
            code, recs = self.ofk('queue', 'collect')
        self.assertEqual((code, ["couldn't fetch" in r.get('error', '') for r in recs]), (1, [True, True]))
        self.assertEqual(len([p for p in (self.queue / 'drop' / 'w1' / 'results').rglob('*.png')]), 2)
        self.assertEqual(self.states(), ['done', 'done'])
        code, recs = self.ofk('queue', 'collect')
        self.assertEqual((code, len(recs), queues.status(self.q)['jobs']), (0, 2, []))

    def test_links_planted_in_the_queue_are_not_followed(self):
        """rsync -a brought symbolic links along. A job whose out/ pointed at a folder on the worker had
        its result written there, and everything in that folder published to the queue."""
        ids = self.queue_photos()
        private = self.root / 'private'
        private.mkdir()
        (private / 'key').write_text('secret')
        (private / 'frame-01-v1-clean-v1.png').write_bytes(b'mine')
        for jid in ids:
            os.symlink(private, self.queue / 'jobs' / jid / 'out')
        seeded = self.queue / 'drop' / 'other' / 'checkpoints' / ids[0]     # and one among a worker's checkpoints
        seeded.mkdir(parents=True)
        os.symlink(private / 'key', seeded / 'fill-000001.npz')
        w = self.worker('w1')
        self.assertEqual(sorted([w.once(), w.once()]), sorted(ids))
        self.assertEqual({p.name: p.read_bytes() for p in private.iterdir()},
                         {'key': b'secret', 'frame-01-v1-clean-v1.png': b'mine'})
        published = [p for p in (self.queue / 'drop' / 'w1').rglob('*') if p.is_file()]
        self.assertFalse([p for p in published if p.name == 'key' or p.read_bytes() == b'secret'])
        self.assertEqual(self.states(), ['done', 'done'])                   # and the jobs were done all the same
        with self.assertRaisesRegex(queues.QueueError, 'names'):
            queues.worker_settings(queue=str(self.queue), name='..')        # nor a name that climbs out of drop/

    def test_a_result_still_to_be_pushed_stays_when_the_queue_cant_be_read(self):
        """A job missing from the view counted as collected, and the worker cleared its own copy of the
        result: but a job.json cut short isn't read, and an unmounted queue is an empty folder."""
        self.photos = self.photos[:1]
        self.queue_photos()
        w = self.worker('w1')
        push = w._push
        w._push = lambda: False if any((w.mirror / 'results').glob('*/out/*')) else push()   # the result can't be sent
        jid = w.once()
        kept = w.mirror / 'results' / jid / 'out'
        self.assertEqual(len(list(kept.iterdir())), 2)
        (self.queue / 'jobs' / jid / 'job.json').write_text('{"job": {"format": 1, ')            # cut short
        w.once()
        self.assertEqual(len(list(kept.iterdir())), 2)
        states = {row['id']: row['state'] for row in queues.status(self.q)['jobs']}
        self.assertEqual(states[jid], 'damaged')                            # shown, and it can be taken out by name
        away = self.root / 'queue-away'
        self.queue.rename(away)
        self.queue.mkdir()                                                  # the mount point, with nothing mounted
        w.once()
        self.assertEqual(len(list(kept.iterdir())), 2)
        shutil.rmtree(self.queue)
        away.rename(self.queue)
        self.assertEqual(queues.remove(self.q, [jid[:10]], say=lambda *a: None), [jid])
        w._push = push
        w.once()                                                            # gone from the queue now: cleared
        self.assertFalse((w.mirror / 'results' / jid).exists())

    def test_a_folder_the_worker_cleared_is_gone_and_no_longer_counts_as_held(self):
        self.queue_photos()
        w = self.worker('w1')
        jid = w.once()
        held = w.mirror / 'checkpoints' / jid
        held.mkdir(parents=True)
        (held / 'fill-000000.npz').write_bytes(b'slow work')
        w._push()
        real = queues._remote

        def no_rights_in_drop(q, *command, **kw):   # as on a server: the drop folders belong to the workers
            if command[:1] == ('rm',) and any('/drop/' in c for c in command):
                return False
            return real(q, *command, **kw)
        with mock.patch.object(queues, '_remote', no_rights_in_drop):
            queues.collect(self.q, say=lambda *a: None)
        emptied = []
        with mock.patch.object(w, '_empty', side_effect=lambda kind, j: emptied.append((kind, j)) or queues.Worker._empty(w, kind, j)):
            for _ in range(3):
                w.once()
        self.assertEqual(sorted(k for k, j in emptied if j == jid), ['checkpoints', 'failed', 'results'])   # once, not at every look
        self.assertEqual([p for kind in ('results', 'checkpoints') for p in (self.queue / 'drop' / 'w1' / kind).glob(jid)], [])
        self.assertEqual(w.view().mine['w1']['checkpoints'], [])

    def test_a_photo_whose_name_starts_with_a_dot_comes_back_too(self):
        hidden = self.root / 'photos' / '.hidden.png'
        shutil.copyfile(self.photos[0], hidden)
        self.photos = [hidden]
        self.queue_photos()
        self.assertIsNotNone(self.worker('w1').once())
        code, recs = self.ofk('queue', 'collect')
        self.assertEqual((code, [Path(r['output']).name for r in recs]), (0, ['.hidden-clean-v1.png']))

    def test_files_of_the_wrong_shape_in_the_queue_stop_nothing(self):
        """The queue is a folder other machines write to. One file that was JSON but not what it should
        be raised an error in every worker and every queue command, `queue remove` among them."""
        ids = self.queue_photos()
        odd = self.queue / 'jobs' / ('0' * 64)
        odd.mkdir()
        (odd / 'job.json').write_text('[1, 2]')
        strange = self.queue / 'jobs' / ('1' * 64)
        strange.mkdir()
        (strange / 'job.json').write_text('{"job": 5, "about": {"packed_utc": 7}}')
        drop = self.queue / 'drop' / 'w9'
        for name, text in (('worker.json', '{"seen": "lately", "memory_gb": "lots"}'), ('leases/x.json', '{"until": "soon"}'),
                           (f'failed/{ids[0]}/why.json', '{"at": "then", "tries": "many"}'),
                           (f'results/{ids[1]}/out/x.json', '[]'), ('leases/y.json', '7')):
            (drop / name).parent.mkdir(parents=True, exist_ok=True)
            (drop / name).write_text(text)
        (self.queue / 'jobs' / ids[0] / 'rejected.json').write_text('{"not": "a list"}')
        (self.queue / 'jobs' / ids[0] / 'retry.json').write_text('[4, "x"]')
        info = queues.status(self.q)
        self.assertEqual(sorted(r['state'] for r in info['jobs']), ['damaged', 'damaged', 'done', 'failed'])
        w = self.worker('w1')
        self.assertEqual(w.once(), ids[0])                                   # the worker carries on with what it can
        self.assertEqual(self.ofk('queue', 'status')[0], 0)
        code, said = self.ofk('queue', 'remove', '--job', '0000', '--job', '1111')
        self.assertEqual((code, said['removed']), (0, ['0' * 64, '1' * 64]))
        self.assertEqual(sorted(p.name for p in (self.queue / 'jobs').iterdir()), sorted(ids))

    def test_one_job_that_cant_be_placed_doesnt_stop_the_rest_being_collected(self):
        self.queue_photos()
        w = self.worker('w1')
        w.once(), w.once()
        real, calls = jobs.collect, []

        def first_fails(package, to=None):
            calls.append(package)
            if len(calls) == 1:
                raise PermissionError(13, 'Permission denied', str(package))
            return real(package, to)
        with mock.patch.object(jobs, 'collect', first_fails):
            code, recs = self.ofk('queue', 'collect')
        self.assertEqual((code, sorted('error' in r for r in recs)), (1, [False, True]))
        self.assertEqual(self.states(), ['done'])                           # the one that couldn't be placed is kept
        self.assertEqual(self.ofk('queue', 'collect')[0], 0)

    def test_a_job_that_couldnt_be_cleared_is_kept_as_ours_and_cleared_next_time(self):
        self.photos = self.photos[:1]
        (jid,) = self.queue_photos()
        self.worker('w1').once()
        with mock.patch.object(queues, '_remote', lambda q, *c, **kw: False):
            code, recs = self.ofk('queue', 'collect')
        self.assertEqual((code, len(recs)), (0, 1))                         # placed beside the photo all the same
        self.assertTrue((queues.outbox(self.q) / jid / 'job.json').exists())
        code, recs = self.ofk('queue', 'collect')                           # still ours, so no "--to DIR" needed
        self.assertEqual((code, 'error' in recs[0], queues.status(self.q)['jobs']), (0, False, []))

    def test_a_home_folder_in_a_queues_address_is_understood(self):
        hosts = self.root / 'config' / 'hosts.toml'
        hosts.write_text('[queues.far]\nqueue = "server:~/openfilmkit-queue"\n[queues.near]\nqueue = "~/openfilmkit-queue"\n'
                         '[queues.odd]\nqueue = 5\n')
        self.assertEqual(queues.queue_settings('far')['queue'], 'server:openfilmkit-queue')
        self.assertEqual(queues.queue_settings('near')['queue'], str(Path.home() / 'openfilmkit-queue'))
        with self.assertRaisesRegex(queues.QueueError, 'host:path'):
            queues.queue_settings('odd')
        s = queues.worker_settings(queue='server:~/q', name='w1')
        self.assertEqual((s['queue'], s['drop']), ('server:q', 'server:q/drop/w1'))
        self.assertEqual(queues.worker_settings(queue='server:q', name='w1', drop='')['drop'], 'server:q/drop/w1')
        for memory in ('nan', 'inf'):                                       # neither is a size, nor JSON when reported
            with self.assertRaisesRegex(queues.QueueError, 'memory_gb'):
                queues.worker_settings(queue='server:q', name='w1', memory_gb=float(memory))

    def test_a_job_packed_elsewhere_stays_a_strangers_after_a_collect_that_failed(self):
        """`collect --to` fetched a stranger's job.json into the outbox, which is what marks a job as
        packed here: after a collect that failed, a plain collect put the result where the stranger's
        job.json said."""
        self.photos = self.photos[:1]
        (jid,) = self.queue_photos()
        shutil.rmtree(queues.outbox(self.q))                                # as on another machine
        w = self.worker('w1')
        w.once()
        result = next((self.queue / 'drop' / 'w1' / 'results' / jid / 'out').glob('*.png'))
        result.write_bytes(b'tampered')
        code, recs = self.ofk('queue', 'collect', '--to', str(self.root / 'inbox'))
        self.assertEqual((code, 'error' in recs[0]), (1, True))
        w.once()                                                            # done again
        code, recs = self.ofk('queue', 'collect')
        self.assertEqual((code, '--to DIR' in recs[0].get('error', '')), (1, True))
        self.assertFalse(self.photos[0].with_name('frame-01-v1-clean-v1.png').exists())
        code, recs = self.ofk('queue', 'collect', '--to', str(self.root / 'inbox'))
        self.assertEqual((code, Path(recs[0]['output']).parent.name), (0, 'inbox'))

    def test_a_batch_that_wouldnt_fit_on_the_queues_machine_is_refused(self):
        with mock.patch.object(queues, '_free_bytes', lambda q: 1000):
            code, err = self.ofk('clean', *map(str, self.photos), '--no-faces', '--queue', 'home')
        self.assertNotEqual(code, 0)
        self.assertIn('queue fewer at a time', err['error'])
        self.assertFalse((self.queue / 'jobs').exists() and any((self.queue / 'jobs').iterdir()))

    def test_a_job_is_offered_to_workers_only_once_its_photo_has_arrived(self):
        # A package's job.json is what offers it to workers, and rsync sends it before the photo: a
        # worker that looked during the upload fetched a job with no photo, failed it and left it.
        real, partway = queues._rsync, []

        def watched(ssh_command, *args, **kw):
            ok = real(ssh_command, *args, **kw)
            if args[-1] == f'{self.queue}/jobs/' and not partway:          # the upload's first pass is over
                partway.append(sorted(p.name for p in (self.queue / 'jobs').rglob('*') if p.is_file()))
                self.assertIsNone(self.worker('w1').once())                 # a worker looks: nothing for it yet
            return ok
        with mock.patch.object(queues, '_rsync', watched):
            ids = self.queue_photos()
        self.assertEqual(partway, [sorted(p.name for p in self.photos)])    # the photos, and no job.json
        w = self.worker('w1')
        self.assertEqual(sorted([w.once(), w.once()]), sorted(ids))
        self.assertEqual(self.states(), ['done', 'done'])

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

    def test_one_photo_queued_twice_with_other_options_comes_back_as_two_copies(self):
        # Both jobs were named -clean-v1 when they were packed, and the second to be collected was
        # refused ("a different file") on every collect after.
        del self.photos[1:]
        self.queue_photos()
        self.queue_photos('--color')
        w = self.worker('w1')
        w.once(), w.once()
        code, recs = self.ofk('queue', 'collect')
        self.assertEqual(code, 0, recs)
        self.assertEqual(sorted(Path(r['output']).name for r in recs),
                         ['frame-01-v1-clean-v1.png', 'frame-01-v1-clean-v2.png'])
        self.assertEqual(sorted(any(s['step'] == 'color' for s in r['steps']) for r in recs), [False, True])
        for r in recs:
            self.assertEqual(jobs.sha(r['output']), r['output_sha256'])
        self.assertEqual(queues.status(self.q)['jobs'], [])

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

    def test_a_job_that_cant_be_done_is_reported_and_not_retried_by_the_same_worker(self):
        self.queue_photos()

        def broken(package, beat, every):
            raise jobs.AlwaysFails('out of film')
        w1 = self.worker('w1', run=broken)
        jid = w1.once()
        rows = {r['id']: r for r in queues.status(self.q)['jobs']}
        self.assertEqual((rows[jid]['state'], rows[jid]['error']), ('failed', 'out of film'))
        self.assertNotIn(jid, w1.candidates(w1.view()))
        later = self.worker('w1', now=lambda: time.time() + 30 * 86400)     # nor a month on
        self.assertNotIn(jid, later.candidates(later.view()))
        self.assertIn(jid, self.worker('w2').candidates(self.worker('w2').view()))

    def test_a_failure_that_may_pass_is_tried_again_after_a_wait(self):
        # The memory cap killing the run, a download that dropped, a full disk: with one worker, a
        # job that failed once stayed failed for good.
        del self.photos[1:]
        (jid,) = self.queue_photos()
        clock = [time.time()]

        def killed(package, beat, every):
            raise jobs.JobError('job run ended with status -9')
        w = self.worker('w1', run=killed, now=lambda: clock[0])
        self.assertEqual(w.once(), jid)
        (row,) = queues.status(self.q)['jobs']
        self.assertEqual((row['state'], row['tries'], row['final']), ('failed', 1, False))
        self.assertTrue((w.state / 'jobs' / jid / 'input').is_dir())       # its photo is kept for the next try
        self.assertIsNone(w.once())                                         # which isn't at once
        clock[0] += queues.RETRY[0] + 1
        w.run = inproc
        self.assertEqual(w.once(), jid)
        self.assertEqual(self.states(), ['done'])

    def test_a_job_that_keeps_failing_is_left_after_three_tries(self):
        del self.photos[1:]
        (jid,) = self.queue_photos()
        clock = [time.time()]

        def killed(package, beat, every):
            raise jobs.JobError('job run ended with status -9')
        w = self.worker('w1', run=killed, now=lambda: clock[0])
        self.assertEqual(w.once(), jid)
        clock[0] += queues.RETRY[0] + 1
        self.assertEqual(w.once(), jid)                                     # the second try
        clock[0] += queues.RETRY[0] + 1
        self.assertIsNone(w.once())                                         # a longer wait before the third
        clock[0] += queues.RETRY[1]
        self.assertEqual(w.once(), jid)
        (row,) = queues.status(self.q)['jobs']
        self.assertEqual((row['state'], row['tries'], row['final']), ('failed', 3, True))
        self.assertFalse((w.state / 'jobs' / jid).exists())                # not tried here again: its copy goes
        clock[0] += 30 * 86400
        self.assertIsNone(w.once())

    def test_a_job_whose_process_dies_without_saying_why_is_tried_again_later(self):
        del self.photos[1:]
        self.queue_photos()
        real = subprocess.Popen

        def dies(argv, **kw):                                               # as when the memory cap kills it
            if list(argv[1:3]) == ['-m', 'openfilmkit']:
                argv = [sys.executable, '-c', 'raise SystemExit(9)']
            return real(argv, **kw)
        w = self.worker('w1', run=queues.run_in_subprocess)
        with mock.patch.object(subprocess, 'Popen', dies):
            w.once()
        (row,) = queues.status(self.q)['jobs']
        self.assertEqual((row['error'], row['final']), ('job run ended with status 9', False))

    def test_queue_retry_sets_failures_aside_and_the_worker_starts_over(self):
        # A failed job could only be freed by deleting the worker's files by hand on the queue's machine.
        del self.photos[1:]
        (jid,) = self.queue_photos()

        def broken(package, beat, every):
            raise jobs.AlwaysFails('out of film')
        w = self.worker('w1', run=broken)
        w.once()
        self.assertIsNone(w.once())
        self.assertEqual(self.ofk('queue', 'retry'), (0, {'retried': [jid]}))
        self.assertEqual(self.states(), ['waiting'])
        self.assertEqual(w.once(), jid)                                     # tried again, and failed again:
        self.assertEqual(self.states(), ['failed'])                         # the new failure stands
        self.assertEqual(self.ofk('queue', 'retry', '--job', jid[:12]), (0, {'retried': [jid]}))
        w.run = inproc
        self.assertEqual(w.once(), jid)
        self.assertEqual(self.states(), ['done'])
        self.assertEqual([p for p in (self.queue / 'drop' / 'w1' / 'failed').rglob('*') if p.is_file()], [])
        self.assertFalse((w.mirror / 'failed' / jid).exists())
        code, err = self.ofk('queue', 'retry', '--job', jid[:12])
        self.assertNotEqual(code, 0)
        self.assertIn('has not failed', err['error'])

    def test_queue_remove_takes_a_job_out_of_the_queue(self):
        self.queue_photos()

        def broken(package, beat, every):
            raise jobs.AlwaysFails('out of film')
        w = self.worker('w1', run=broken)
        failed = w.once()
        code, err = self.ofk('queue', 'remove', '--job', 'no-such-job')
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.states()), 2)
        self.assertEqual(self.ofk('queue', 'remove', '--job', failed[:12]), (0, {'removed': [failed]}))
        self.assertFalse((self.queue / 'jobs' / failed).exists())
        self.assertFalse((queues.outbox(self.q) / failed).exists())        # nor its photo's copy kept here
        (row,) = queues.status(self.q)['jobs']
        self.assertEqual(row['state'], 'waiting')
        w.run = inproc
        self.assertEqual(w.once(), row['id'])                               # the worker clears its own leftovers
        self.assertEqual([p for p in (self.queue / 'drop' / 'w1' / 'failed').rglob('*') if p.is_file()], [])
        self.assertFalse((w.mirror / 'failed' / failed).exists())

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
        self.assertTrue(row['final'])                                       # the photo's own: no use trying again
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

        def no_job_json(ssh_command, *args, **kw):                           # the connection drops just then
            if any(not a.startswith('-') and a.endswith('/job.json') for a in map(str, args)):
                return False
            return real(ssh_command, *args, **kw)
        with mock.patch.object(queues, '_rsync', no_job_json):
            recs = queues.collect(self.q, to=self.root / 'inbox', say=lambda *a: None)
        self.assertEqual([('error' in r) for r in recs], [True, True])
        self.assertEqual(self.states(), ['done', 'done'])
        recs = queues.collect(self.q, to=self.root / 'inbox', say=lambda *a: None)
        self.assertEqual(len([r for r in recs if 'error' not in r]), 2)


if __name__ == '__main__':
    unittest.main()
