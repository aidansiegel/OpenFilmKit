"""Faults in the queue: every call it makes to the outside (rsync, a command on the queue's
machine, df) goes wrong in turn while a photo makes its way through (queued, done by a worker,
collected), and the same commands are then run again, as a person or a service would.

Whatever went wrong, the job must still be in the queue or its result beside the original, the
result the same file as with no fault, the work done once, and nothing of the job left behind
once it's collected. A subtest that fails here is a finding: its message lists what is wrong and
then every command that was run and what it said.

The queue is a local folder, as in test_queue.py. The default sweep makes each call fail and
kills the worker at each of its calls; OPENFILMKIT_FAULTS=all adds the calls that go through but
are reported as failed, and the worker stopped by Ctrl-C at each call.
"""
import contextlib
import functools
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import numpy as np
    from PIL import Image
    from openfilmkit import clean, jobs, queues
except ImportError:
    clean = None

OUTSIDE = ('_rsync', '_remote', '_free_bytes')   # every way queues.py reaches the queue's machine
EVERY = os.environ.get('OPENFILMKIT_FAULTS') == 'all'
FAILED = object()   # a command that ended with a message (or was killed) instead of a result


class Killed(BaseException):
    """The worker's process is gone (a power cut, kill -9): nothing more of it reaches the queue."""


@functools.lru_cache(maxsize=1)
def photo_bytes():
    rng = np.random.default_rng(3)
    img = np.clip(120 + rng.normal(0, 3, (150, 200, 3)), 0, 255).astype(np.uint8)
    img[75:78, 100:103] = 255
    held = io.BytesIO()
    Image.fromarray(img).save(held, 'PNG')
    return held.getvalue()


class World:
    """A queue in a local folder with one photo to clean, a clock that is moved by hand, and every
    call to the outside counted, so that call number k can be made to go wrong."""

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.queue = self.root / 'queue'
        (self.root / 'config').mkdir()
        (self.root / 'config' / 'hosts.toml').write_text(f'[queues.home]\nqueue = "{self.queue}"\n')
        self.now = time.time()
        self.calls, self.fault, self.dead, self.doing = [], None, False, None
        self.story, self.escaped, self.runs, self.workers = [], [], 0, {}
        self.real = {name: getattr(queues, name) for name in OUTSIDE}
        for patch in (mock.patch.dict(os.environ, {'OPENFILMKIT_CONFIG': str(self.root / 'config'),
                                                   'OPENFILMKIT_CACHE': str(self.root / 'cache')}),
                      mock.patch.object(queues, 'SPARE', 0),   # however full this machine's disk is
                      mock.patch.object(queues, '_now', lambda: self.now),
                      mock.patch.multiple(queues, **{name: self._outside(name) for name in OUTSIDE})):
            self.stack.enter_context(patch)
        (self.root / 'photos').mkdir()
        self.photo = self.root / 'photos' / 'frame-01-v1.png'
        self.photo.write_bytes(photo_bytes())
        self.result = self.photo.with_name('frame-01-v1-clean-v1.png')
        self.recipe = Path(str(self.result) + '.json')
        self.jid = jobs.job_id(jobs.clean_job(self.photo, protect_faces=False)[0])
        self.q = queues.queue_settings('home')
        return self

    def __exit__(self, *exc):
        self.stack.close()

    # -- the outside
    def _outside(self, name):
        failed = None if name == '_free_bytes' else False   # what the real one gives on a dropped connection

        def call(*args, **kwargs):
            words = [str(a) for a in args[1:] if not str(a).startswith('-')]
            text = re.sub('[0-9a-f]{64}', 'JOB', ' '.join(words).replace(f'{self.root}/', ''))
            self.calls.append((self.doing, f'{name[1:]} {text}'.strip()))
            how = self.fault[1] if self.fault and self.fault[0] == len(self.calls) else None
            if self.dead or how == 'killed':
                self.dead = True
                raise Killed
            if how == 'interrupted':   # Ctrl-C, or systemd's stop: the worker may still tidy up after itself
                raise KeyboardInterrupt
            if how == 'fails':
                return failed
            got = self.real[name](*args, **kwargs)
            return failed if how == 'unheard' else got   # unheard: done, but the answer never arrived
        return call

    # -- the commands
    def say(self, *words):
        self.story.append('    ' + ' '.join(map(str, words)))

    def _run(self, name, command):
        """Run a command as the CLI would: a QueueError or JobError is a message for the person,
        and anything else that comes out is noted as a traceback."""
        self.doing = name
        try:
            got = command()
            self.story.append(f'{name}: {got!r}')
            return got
        except (queues.QueueError, jobs.JobError) as e:
            self.story.append(f'{name}: {type(e).__name__}: {e}')
        except (Killed, KeyboardInterrupt) as e:
            self.story.append(f'{name}: {type(e).__name__}')
        except Exception as e:
            self.escaped.append(f'{name}: {type(e).__name__}: {e}')
            self.story.append(f'{name}: TRACEBACK {type(e).__name__}: {e}')
        finally:
            self.dead = False   # the next command is another process
        return FAILED

    def submit(self):   # clean PHOTO --no-faces --queue home
        def command():
            package = jobs.pack_clean(self.photo, queues.outbox(self.q), protect_faces=False)
            queues.submit(self.q, [package], say=self.say)
            return 'queued'
        return self._run('clean --queue', command)

    def job(self, package, beat, every):
        """Run the job as a worker does one that takes a while: its claim is renewed once on the way."""
        self.now += queues.RENEW
        beat()
        if not jobs.done(package):
            self.runs += 1
        jobs.run(package)

    def work(self, name='w1'):   # one look by `openfilmkit worker`
        if name not in self.workers:
            s = queues.worker_settings(queue=str(self.queue), name=name, state=str(self.root / f'state-{name}'), settle=0)
            self.workers[name] = queues.Worker(s, run=self.job, now=lambda: self.now, say=self.say)
        got = self._run(f'worker {name}', lambda: (self.workers[name].once() or 'nothing to do')[:12])
        if got is FAILED:
            del self.workers[name]   # the process ended; the service starts a new one on the same state folder
        return got

    def collect(self):   # queue collect
        def command():
            return [r.get('error') or Path(r['output']).name for r in queues.collect(self.q, say=self.say)]
        return self._run('queue collect', command)

    def go(self, fault=None):
        """The photo's whole way, each command once; `fault` is (k, how) for the call that goes wrong."""
        self.fault = fault
        self.submitted = self.submit()
        if self.submitted is FAILED:
            # Told the copy failed, a person gives the command again there and then, before anything else
            # is run. (Given again only after the result is back, it would rightly make a second copy.)
            self.submitted = self.submit()
        self.work()
        self.collect()
        self.work()   # the worker's next look, which tidies up
        self.fault = None

    def again(self):
        """The same commands again, at once and then after each of the queue's waits (a claim
        lapsing, the waits before a failed job's next tries), until the result is back and the
        queue has let go of the job."""
        if self.submitted is FAILED:
            self.submitted = self.submit()
        for wait in (0, queues.LEASE + 1, queues.RETRY[0] + 1, queues.RETRY[1] + 1):
            self.now += wait
            self.work()
            self.collect()
            if self.collected() and not (self.queue / 'jobs' / self.jid).exists():
                break
        self.work()

    # -- where things stand
    def collected(self):
        return self.result.is_file() and self.recipe.is_file()

    def row(self):
        """The job's line in `queue status`, or None."""
        return next((r for r in queues.status(self.q)['jobs'] if r['id'] == self.jid), None)

    def idle_calls(self):
        """How many calls to the outside a worker's look makes now."""
        before = len(self.calls)
        self.work()
        return len(self.calls) - before

    def leftovers(self):
        """Files of the job outside the photos' folder (the copies of the queue's small files that
        `status` and the workers keep are caches, replaced on their next look)."""
        return [str(p.relative_to(self.root)).replace(self.jid, 'JOB') for p in sorted(self.root.rglob('*'))
                if p.is_file() and self.jid in str(p) and 'view' not in p.relative_to(self.root).parts
                and self.root / 'photos' not in p.parents]


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
@unittest.skipIf(not shutil.which('rsync'), 'needs rsync')
class QueueFaults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with World() as world:   # with no fault: what the calls are, and what comes back
            world.go()
            cls.calls = list(world.calls)
            world.again()
            cls.fine = {'bytes': world.result.read_bytes(), 'steps': json.loads(world.recipe.read_text())['steps'],
                        'idle': world.idle_calls()}

    def wrong(self, world):
        """What is wrong with the world now that every command has been run again."""
        wrong = [f'a traceback where a message belongs: {e}' for e in world.escaped]
        if world.photo.read_bytes() != photo_bytes():
            wrong.append('the original photo was changed')
        if not world.collected():
            row = world.row()
            wrong.append(f"the result isn't back; the queue shows the job as {row['state']} ({row.get('error')})" if row
                         else "the result isn't back and the job isn't in the queue: lost")
        else:
            try:
                recipe = json.loads(world.recipe.read_text())
            except ValueError:
                recipe = {}
            if world.result.read_bytes() != self.fine['bytes']:
                wrong.append('the collected file is not the one a run with no fault gives')
            if recipe.get('output_sha256') != jobs.sha(world.result) or recipe.get('steps') != self.fine['steps'] \
                    or recipe.get('source_sha256') != jobs.sha(world.photo) or recipe.get('job', {}).get('id') != world.jid:
                wrong.append("the collected recipe doesn't describe the collected file")
        beside = sorted(p.name for p in world.photo.parent.iterdir())
        if world.collected() and beside != sorted([world.photo.name, world.result.name, world.recipe.name]):
            wrong.append(f'beside the original: {beside}')
        if (world.queue / 'jobs' / world.jid).exists():
            wrong.append('the queue still holds the job')
        if world.runs != 1:
            wrong.append(f'the job was run {world.runs} times')
        idle = world.idle_calls()
        if idle != self.fine['idle']:
            wrong.append(f"a worker's look at the empty queue makes {idle} calls, not {self.fine['idle']}")
        if world.leftovers():
            wrong.append(f'left behind: {world.leftovers()}')
        return wrong

    def sweep(self, how, only=''):
        for k, (command, call) in enumerate(self.calls, 1):
            if not command.startswith(only):
                continue
            with self.subTest(k=k, how=how, during=command, call=call), World() as world:
                world.go((k, how))
                story = lambda: '\n' + '\n'.join(world.story)
                # A command that said it worked can't have lost the job, even before anything is run again.
                self.assertTrue(world.submitted is FAILED or world.collected() or world.row(),
                                'the job is neither in the queue nor collected' + story())
                world.again()
                self.assertEqual(self.wrong(world), [], story())

    def test_with_no_fault_the_photo_comes_back_and_nothing_is_left(self):
        with World() as world:
            world.go()
            world.again()
            self.assertEqual(self.wrong(world), [], '\n' + '\n'.join(world.story))

    def test_each_call_failing_in_turn_loses_nothing_and_the_same_commands_finish_the_job(self):
        self.sweep('fails')

    def test_a_worker_killed_at_each_call_is_made_up_for_by_the_next_one_started(self):
        self.sweep('killed', only='worker')

    @unittest.skipUnless(EVERY, 'set OPENFILMKIT_FAULTS=all for the exhaustive sweep')
    def test_each_call_done_but_reported_as_failed_loses_nothing(self):
        self.sweep('unheard')

    @unittest.skipUnless(EVERY, 'set OPENFILMKIT_FAULTS=all for the exhaustive sweep')
    def test_a_worker_stopped_by_ctrl_c_at_each_call_leaves_the_job_for_later(self):
        self.sweep('interrupted', only='worker')


if __name__ == '__main__':
    unittest.main()
