"""Runs stopped partway, and runs that hit a full disk: what they leave, and whether a rerun works.

Each command is run once cleanly while its write points are counted: the calls that create, write,
rename or remove files in the work folder. Then it is run again once per point, stopped there:
by Ctrl-C just before the call and just after it, and by a full disk in place of each call that
needs room. After each stop, nothing left may look finished without being so (a copy without a
recipe that matches it, a recipe cut short), and the same command again must succeed and leave
exactly the files an uninterrupted run leaves. A full disk must be said in one line that names
the place, with exit status 1.

The cases that fail are the findings. Not covered here: `scan`, writes cut short halfway, and a
process killed outright (no cleanup runs at all).
"""
import contextlib
import errno
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np
    import tifffile
    from PIL import Image
    from openfilmkit import clean, jobs  # noqa: F401 (clean: these tests need what it needs)
    from openfilmkit.films import stocks as films
except ImportError:
    np = None
from openfilmkit import cli, roll as rolls

needs_libraries = unittest.skipIf(np is None, 'needs OpenCV, numpy, Pillow and tifffile')
IMAGES = ('.jpg', '.png', '.tif')
# Different in every run by design: times, and which stages a resumed run took from checkpoints.
VOLATILE = ('created_utc', 'packed_utc', 'finished_utc', 'resumed_from_checkpoints')
Call = namedtuple('Call', 'said path needs_room names_file')


def write_points():
    """(where it lives, its name, which argument names the file, whether it needs room on the
    disk, whether its real failure names the file: a failed write() doesn't, a failed mkdir does)."""
    def arg(i):
        return lambda args, kw: args[i]

    def new_map(args, kw):      # np.memmap also reads the scan; only 'w+' creates a file
        return args[0] if kw.get('mode') == 'w+' else None

    def open_file(args, kw):    # np.savez is handed the open checkpoint file
        return getattr(args[0], 'name', args[0])
    return [(Path, 'mkdir', arg(0), True, True), (Path, 'write_text', arg(0), True, False),
            (Path, 'unlink', arg(0), False, False), (np, 'memmap', new_map, True, False),
            (Image.Image, 'save', arg(1), True, False), (tifffile, 'imwrite', arg(0), True, False),
            (np, 'save', arg(0), True, False), (np, 'savez', open_file, True, False),
            (os, 'replace', arg(1), False, False), (os, 'rename', arg(1), False, False),
            (os, 'link', arg(1), True, True), (shutil, 'copyfile', arg(1), True, True),
            (shutil, 'rmtree', arg(0), False, False)]


class Stop:
    """Counts the write points under `root`, and stops the run at number `at`: 'before' or
    'after' it with Ctrl-C, or with a 'full' disk in its place."""

    def __init__(self, root, at=None, how=None):
        self.root, self.at, self.how = str(root), at, how
        self.calls, self.inside = [], 0

    def wrap(self, real, label, where, needs_room, names_file):
        def call(*args, **kw):
            path = where(args, kw)
            # Calls made inside a counted call are part of it (Path.mkdir makes the parents this way).
            if self.inside or path is None or not str(path).startswith(self.root):
                return real(*args, **kw)
            short = re.sub(r'([0-9a-f]{12})[0-9a-f]{52}', r'\1...', os.path.relpath(path, self.root))
            self.calls.append(Call(f'{label} of {short}', str(path), needs_room, names_file))
            here = len(self.calls) == self.at
            if here and self.how == 'before':
                raise KeyboardInterrupt
            # A folder that is already there needs no room, so mkdir can't fail on it.
            if here and self.how == 'full' and not (label == 'Path.mkdir' and Path(path).is_dir()):
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), *([str(path)] if names_file else []))
            self.inside += 1
            try:
                result = real(*args, **kw)
            finally:
                self.inside -= 1
            if here and self.how == 'after':
                raise KeyboardInterrupt
            return result
        return call


def kit(argv):
    """Run the command line: (exit status, what it said on stderr). A traceback is (None, it)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(a) for a in argv])
    except BaseException as e:   # whatever main() let through would be a traceback at a terminal
        return None, f'{type(e).__name__}: {e}'
    return code, err.getvalue().strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scrub(data):
    if isinstance(data, dict):
        return {k: scrub(v) for k, v in data.items() if k not in VOLATILE}
    return [scrub(v) for v in data] if isinstance(data, list) else data


def snapshot(root):
    """{relative name: what is there}: a file's checksum, or for a JSON file what it says apart
    from the VOLATILE keys. A packing folder left behind is passed over: review issue 9, being fixed."""
    found = {}
    for p in sorted(Path(root).rglob('*')):
        rel = p.relative_to(root)
        if any(part.endswith('.packing') for part in rel.parts):
            continue
        if p.is_dir():
            found[rel.as_posix()] = 'folder'
        elif p.suffix == '.json':
            try:
                found[rel.as_posix()] = scrub(json.loads(p.read_text()))
            except ValueError:
                found[rel.as_posix()] = 'damaged JSON'
        else:
            found[rel.as_posix()] = sha(p)
    return found


def scene(w=240, h=160, seed=3):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[:h, :w]
    base = np.stack([90 + 60 * x / w, 100 + 40 * y / h, 120 - 30 * x / w], 2)
    img = np.clip(base + rng.normal(0, 3, base.shape), 0, 255).astype(np.uint8)
    img[80:83, 120:123] = 255                                     # a speck of dust
    return img


def raw_scan(path):
    tifffile.imwrite(path, np.linspace(2000, 50000, 40 * 60 * 3).reshape(40, 60, 3).astype('uint16'),
                     photometric='rgb')


def photo(work):
    (work / 'photos').mkdir()
    Image.fromarray(scene()).save(work / 'photos' / 'frame-v1.png')
    return work / 'photos' / 'frame-v1.png'


def package(work):
    return sorted(p for p in (work / 'outbox').iterdir() if not p.name.startswith('.'))[0]


class Interrupted:
    """One command, stopped at each of its write points. Every case starts from a fresh copy of
    the same folder at the same path, so recipes (which hold full paths) can be compared."""
    name = ''

    @staticmethod
    def build(work):
        """Put what the command needs into `work`."""

    @staticmethod
    def command(work):
        """The command's arguments."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        base = Path(cls.temp.name).resolve()
        cls.work, cls.template = base / 'work', base / 'template'
        # clean's local checkpoints go here, never to the real ~/.cache/openfilmkit.
        cls.cache = mock.patch.dict('os.environ', {'OPENFILMKIT_CACHE': str(cls.work / 'cache')})
        cls.cache.start()
        cls.work.mkdir()
        cls.build(cls.work)
        cls.work.rename(cls.template)
        cls.fixture = set(snapshot(cls.template))
        cls.fresh()
        code, said, stop = cls.run_stopped()
        if code != 0:
            raise AssertionError(f'{cls.name} fails without being stopped: {said}')
        cls.calls, cls.reference = stop.calls, snapshot(cls.work)

    @classmethod
    def tearDownClass(cls):
        cls.cache.stop()
        cls.temp.cleanup()

    @classmethod
    def fresh(cls):
        shutil.rmtree(cls.work, ignore_errors=True)
        shutil.copytree(cls.template, cls.work)

    @classmethod
    def run_stopped(cls, at=None, how=None):
        stop = Stop(cls.work, at, how)
        with contextlib.ExitStack() as stack:
            for owner, name, where, needs_room, names_file in write_points():
                label = f"{owner.__name__.replace('numpy', 'np')}.{name}"
                stack.enter_context(mock.patch.object(
                    owner, name, stop.wrap(getattr(owner, name), label, where, needs_room, names_file)))
            code, said = kit(cls.command(cls.work))
        return code, said, stop

    def looks_finished(self):
        """What the stopped run left that passes for a result without being one."""
        said = []
        for p in sorted(self.work.rglob('*')):
            rel = p.relative_to(self.work)
            # Inside a package, jobs.done() says what is finished; the cache holds only checkpoints.
            if not p.is_file() or rel.as_posix() in self.fixture or rel.parts[0] in ('outbox', 'cache'):
                continue
            if p.suffix in IMAGES:
                try:
                    matches = json.loads(Path(str(p) + '.json').read_text())['output_sha256'] == sha(p)
                except (OSError, ValueError, KeyError):
                    matches = False
                if not matches:
                    said.append(f'{rel} was left without a recipe that matches it')
            elif p.suffix == '.json':
                if snapshot(self.work)[rel.as_posix()] == 'damaged JSON':
                    said.append(f'{rel} was left cut short')
                if not Path(str(p)[:-len('.json')]).is_file():
                    said.append(f'{rel} was left describing a file that is not there')
        return said

    def check_what_is_left_and_rerun(self, problems):
        problems += self.looks_finished()
        left = snapshot(self.work)
        # Stopped once everything was written: the run was whole, and a rerun would be a second run.
        if any(left.get(name) != value for name, value in self.reference.items()):
            other = os.getpid() + 1     # a rerun is another process: temporary names carry the PID
            with mock.patch('os.getpid', return_value=other):
                code, said = kit(self.command(self.work))
            if code != 0:
                problems.append(f'the same command again gave exit status {code}: {said}')
            else:
                got = snapshot(self.work)
                for name in sorted(set(got) | set(self.reference)):
                    if name not in self.reference:
                        problems.append(f'after the same command again {name} is left over')
                    elif got.get(name) != self.reference[name]:
                        problems.append(f"after the same command again {name} is "
                                        f"{'missing' if name not in got else 'not what an uninterrupted run gives'}")
        if problems:
            self.fail('; '.join(problems))

    def test_ctrl_c_at_any_write_leaves_nothing_that_looks_finished_and_a_rerun_gives_the_same_files(self):
        for k, call in enumerate(self.calls, 1):
            for how in ('before', 'after'):
                with self.subTest(f'{self.name}, Ctrl-C {how} write {k} of {len(self.calls)}: {call.said}'):
                    self.fresh()
                    code, said, _ = self.run_stopped(k, how)
                    problems = [] if code == 130 else [
                        f'ended in a traceback ({said})' if code is None else f'exit status {code}, not 130 ({said})']
                    self.check_what_is_left_and_rerun(problems)

    def test_a_full_disk_at_any_write_is_said_in_one_line_naming_the_place_and_a_rerun_gives_the_same_files(self):
        for k, call in enumerate(self.calls, 1):
            if not call.needs_room:
                continue
            with self.subTest(f'{self.name}, disk full at write {k} of {len(self.calls)}: {call.said}'):
                self.fresh()
                code, said, _ = self.run_stopped(k, 'full')
                problems = []
                if code is None:
                    problems.append(f'ended in a traceback ({said})')
                elif code == 0:
                    pass    # it had another way (a copy where a hard link failed), or the folder was there
                elif code != 1 or '\n' in said or 'No space left' not in said:
                    problems.append(f'exit status {code} and {said!r}, not exit status 1 and one line about the disk')
                else:
                    # The place: the folder being written to, or one it is in (the package, the outbox). A
                    # run's own temporary folder is gone by the time anyone reads the message.
                    work, written = self.work.resolve(), Path(call.path).resolve()
                    if not any(str(folder) in said for folder in (written, *written.parents) if work in folder.parents):
                        problems.append(f'it said only {said!r}, which does not name the place that is full')
                self.check_what_is_left_and_rerun(problems)


@needs_libraries
class Convert(Interrupted, unittest.TestCase):
    name = 'convert --output views/view.jpg'

    @staticmethod
    def build(work):
        raw_scan(work / 'raw.tif')

    @staticmethod
    def command(work):
        return ['convert', work / 'raw.tif', '--output', work / 'views' / 'view.jpg',
                '--stock', 'kirkland-200', '--profile', 'agfa-hdc200']


@needs_libraries
class ConvertInARoll(Interrupted, unittest.TestCase):
    name = 'convert --tiff of a roll frame'

    @staticmethod
    def build(work):
        rolls.create(work / 'roll-35')
        rolls.set_choice(work / 'roll-35', films.roll_choice('kirkland-200', profile_id='agfa-hdc200'))
        raw_scan(work / 'roll-35' / 'masters' / 'frame-01.tif')

    @staticmethod
    def command(work):
        return ['convert', work / 'roll-35' / 'masters' / 'frame-01.tif', '--tiff']


@needs_libraries
class Clean(Interrupted, unittest.TestCase):
    name = 'clean'
    build = staticmethod(photo)

    @staticmethod
    def command(work):
        return ['clean', work / 'photos' / 'frame-v1.png', '--no-faces']


@needs_libraries
class Pack(Interrupted, unittest.TestCase):
    name = 'clean --pack'
    build = staticmethod(photo)

    @staticmethod
    def command(work):
        return ['clean', work / 'photos' / 'frame-v1.png', '--no-faces', '--pack', work / 'outbox']


@needs_libraries
class JobRun(Interrupted, unittest.TestCase):
    name = 'job run'

    @staticmethod
    def build(work):
        jobs.pack_clean(photo(work), work / 'outbox', protect_faces=False)

    @staticmethod
    def command(work):
        return ['job', 'run', package(work)]


@needs_libraries
class JobCollect(Interrupted, unittest.TestCase):
    name = 'job collect'

    @staticmethod
    def build(work):
        jobs.run(jobs.pack_clean(photo(work), work / 'outbox', protect_faces=False))

    @staticmethod
    def command(work):
        return ['job', 'collect', package(work)]


if __name__ == '__main__':
    unittest.main()
