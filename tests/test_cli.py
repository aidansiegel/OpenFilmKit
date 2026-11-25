import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class ClosedPipe(unittest.TestCase):
    def test_reader_closing_early_is_quiet(self):
        # `openfilmkit profiles | head -1`: the reader leaves before we finish writing.
        r, w = os.pipe()
        os.close(r)
        try:
            proc = subprocess.run(
                [sys.executable, '-m', 'openfilmkit', 'profiles'],
                cwd=ROOT, stdout=w, stderr=subprocess.PIPE, text=True, timeout=60,
                env={**os.environ, 'PYTHONPATH': str(ROOT)})
        finally:
            os.close(w)
        self.assertEqual(proc.returncode, 141, proc.stderr)
        self.assertEqual(proc.stderr, '')



class StopWithParent(unittest.TestCase):
    """A scan the desktop app started stops when the app's back end goes (the end of its stdin), the way Stop
    stops it, instead of running on unseen. Dogfooding found scans finishing frames after the back end was
    killed, and Quit kills it."""
    CHILD = '''
import signal, sys, time
from openfilmkit import cli
def stop(signum, frame):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, stop)
cli.stop_when_parent_goes()
print('waiting', flush=True)
try:
    for _ in range(600):
        time.sleep(.1)        # like a scan waiting on scanimage
    print('ran on', flush=True)
except KeyboardInterrupt:
    print('stopped', flush=True)
'''

    def child(self, stop_with_parent):
        env = {**os.environ, 'PYTHONPATH': str(ROOT)}
        if stop_with_parent:
            env['OPENFILMKIT_STOP_WITH_PARENT'] = '1'
        return subprocess.Popen([sys.executable, '-c', self.CHILD], cwd=ROOT, env=env, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_the_end_of_stdin_stops_it_like_stop(self):
        p = self.child(True)
        self.assertEqual(p.stdout.readline().strip(), 'waiting')
        p.stdin.close()                      # the back end has gone
        out, err = p.communicate(timeout=30)
        self.assertEqual(out.strip(), 'stopped', err)

    def test_a_scan_that_ends_while_the_app_is_still_there_exits_cleanly(self):
        # The watcher is still waiting on stdin when the scan finishes. Reading through sys.stdin, it held the
        # buffer's lock at shutdown and Python aborted (-6), so every scan from the app read as failed. That
        # abort showed in a real scan (the fake Plustek, through gui_service) but not in this small process;
        # this checks the clean exit, and the overnight dogfood soak covers the real path.
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'OPENFILMKIT_STOP_WITH_PARENT': '1'}
        p = subprocess.Popen([sys.executable, '-c', 'from openfilmkit import cli\ncli.stop_when_parent_goes()\nprint("done")'],
                             cwd=ROOT, env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        try:
            p.wait(timeout=30)                     # stdin stays open: the app is still there when it ends
            out, err = p.stdout.read(), p.stderr.read()
        finally:
            p.kill()
            p.stdin.close()
            p.stdout.close()
            p.stderr.close()
        self.assertEqual((p.returncode, out.strip()), (0, 'done'), err)
        self.assertNotIn('Fatal Python error', err)

    def test_without_the_apps_word_stdin_closing_changes_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != 'OPENFILMKIT_STOP_WITH_PARENT'}
        p = subprocess.Popen([sys.executable, '-c', self.CHILD.replace('cli.stop_when_parent_goes()',
                              "import os\nif os.environ.get(cli.STOP_WITH_PARENT) == '1': cli.stop_when_parent_goes()")],
                             cwd=ROOT, env={**env, 'PYTHONPATH': str(ROOT)}, text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(p.stdout.readline().strip(), 'waiting')
            p.stdin.close()                  # a terminal's stdin closing is not the app going
            with self.assertRaises(subprocess.TimeoutExpired):
                p.wait(timeout=2)
        finally:
            p.kill()
            p.communicate()


class Options(unittest.TestCase):
    def test_color_has_a_hidden_british_alias(self):
        sys.path.insert(0, str(ROOT))
        from openfilmkit import cli
        for flag in ('--color', '--colour'):
            self.assertTrue(cli.parser().parse_args(['clean', 'x.jpg', flag]).color, flag)
        clean = cli.parser()._subparsers._group_actions[0].choices['clean']
        self.assertIn('--color', clean.format_help())
        self.assertNotIn('--colour', clean.format_help())

class Errors(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT))
        from openfilmkit import cli
        self.cli = cli

    def test_an_error_nobody_foresaw_is_said_in_a_line_and_its_traceback_kept(self):
        def stocks(args, out):
            return 1 / 0
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {'OPENFILMKIT_CACHE': d}), \
                mock.patch.dict(self.cli.COMMANDS, {'stocks': stocks}):
            os.environ.pop('OPENFILMKIT_TRACEBACK', None)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.cli.main(['stocks', '--json'])
            said = json.loads(out.getvalue())
            self.assertEqual((code, len(err.getvalue().strip().splitlines()), said['unexpected']), (1, 1, True))
            self.assertIn('unexpected error: ZeroDivisionError: division by zero (test_cli.py, line', err.getvalue())
            kept = Path(said['details']).read_text()
            self.assertTrue(kept.startswith('openfilmkit ') and 'stocks --json' in kept and 'Traceback' in kept)
            with mock.patch.dict(os.environ, {'OPENFILMKIT_TRACEBACK': '1'}), self.assertRaises(ZeroDivisionError):
                self.cli.main(['stocks'])                                # for whoever wants it on the screen

    def test_a_file_that_went_wrong_is_named_as_it_is(self):
        full = OSError(28, 'No space left on device', 'C:\\Users\\me\\scans')   # Python's own text doubles each backslash
        self.assertEqual(self.cli._plain(full), 'No space left on device: C:\\Users\\me\\scans')
        self.assertEqual(self.cli._plain(OSError('no screen here')), 'no screen here')

    def test_a_line_is_still_said_where_its_arrow_cant_be_shown(self):
        raw = io.BytesIO()
        windows = io.TextIOWrapper(raw, encoding='cp1252')                # output piped or sent to a file there
        self.cli.Out._print('frame-01.tif \u2192 frame-01-v1.jpg', windows)
        windows.flush()
        self.assertEqual(raw.getvalue().decode('cp1252').strip(), 'frame-01.tif ? frame-01-v1.jpg')


if __name__ == '__main__':
    unittest.main()
