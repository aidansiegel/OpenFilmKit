"""clean --on HOST, end to end, with a stand-in for ssh that runs the command here in a folder
standing for the host's home: the packages really go and come back by rsync, and really run in
a separate `openfilmkit job run`."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import numpy as np
    from PIL import Image
    from openfilmkit import clean
except ImportError:
    clean = None
from openfilmkit import cli

FAKE_SSH = '''import os, subprocess, sys
# ssh HOST COMMAND...: run COMMAND here, in the folder standing for HOST's home
sys.exit(subprocess.run(['sh', '-c', ' '.join(sys.argv[2:])], cwd=os.environ['FAKE_HOME']).returncode)
'''


@unittest.skipIf(clean is None, 'needs OpenCV, numpy and Pillow')
@unittest.skipIf(os.name == 'nt' or not shutil.which('rsync'), 'needs rsync and a POSIX shell')
class OnHost(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'pi-home'
        self.home.mkdir()
        (self.root / 'fake_ssh.py').write_text(FAKE_SSH)
        (self.root / 'config').mkdir()
        self.write_hosts(f'{sys.executable} -m openfilmkit')
        (self.root / 'photos').mkdir()
        rng = np.random.default_rng(3)
        img = np.clip(120 + rng.normal(0, 3, (300, 400, 3)), 0, 255).astype(np.uint8)
        img[150:153, 200:203] = 255
        self.src = self.root / 'photos' / 'frame-v1.png'
        Image.fromarray(img).save(self.src)
        self.env = mock.patch.dict(os.environ, {
            'OPENFILMKIT_CONFIG': str(self.root / 'config'), 'OPENFILMKIT_CACHE': str(self.root / 'cache'),
            'FAKE_HOME': str(self.home), 'PYTHONPATH': str(ROOT)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write_hosts(self, openfilmkit):
        (self.root / 'config' / 'hosts.toml').write_text(
            f'[hosts.pi]\nssh_command = "{sys.executable} {self.root / "fake_ssh.py"}"\n'
            f'openfilmkit = "{openfilmkit}"\njobs = "~/ofk-jobs"\n')

    def ofk(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main([*argv, '--json'])
        return code, json.loads(out.getvalue())

    def test_a_photo_is_cleaned_on_the_host_and_comes_back_beside_the_original(self):
        code, recs = self.ofk('clean', str(self.src), '--no-faces', '--on', 'pi')
        self.assertEqual(code, 0, recs)
        dest = self.root / 'photos' / 'frame-v1-clean-v1.png'
        self.assertEqual(recs[0]['output'], str(dest))
        direct = clean.clean(self.src, self.root / 'direct.png', protect_faces=False)
        self.assertEqual(clean.sha(dest), direct['output_sha256'])
        self.assertEqual(recs[0]['source'], str(self.src))
        self.assertEqual(list((self.home / 'ofk-jobs').iterdir()), [])          # tidied up there...
        self.assertEqual(list((self.root / 'cache' / 'outbox' / 'pi').iterdir()), [])   # ...and here

    def test_kept_jobs_are_found_again_and_not_run_twice(self):
        code, first = self.ofk('clean', str(self.src), '--no-faces', '--on', 'pi', '--keep-remote')
        self.assertEqual(code, 0, first)
        (job,) = (self.home / 'ofk-jobs').iterdir()
        stamp = (job / 'out' / 'frame-v1-clean-v1.png.json').stat().st_mtime_ns
        code, again = self.ofk('clean', str(self.src), '--no-faces', '--on', 'pi', '--keep-remote')
        self.assertEqual(code, 0, again)
        self.assertEqual(again[0]['output_sha256'], first[0]['output_sha256'])
        self.assertEqual((job / 'out' / 'frame-v1-clean-v1.png.json').stat().st_mtime_ns, stamp)

    def test_a_host_with_another_version_is_refused_before_anything_is_sent(self):
        self.write_hosts('echo 0.0.1')
        code, err = self.ofk('clean', str(self.src), '--no-faces', '--on', 'pi')
        self.assertNotEqual(code, 0)
        self.assertIn('0.0.1', err['error'])
        self.assertFalse((self.home / 'ofk-jobs').exists())


if __name__ == '__main__':
    unittest.main()
