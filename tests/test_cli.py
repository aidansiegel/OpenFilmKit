import os
import subprocess
import sys
import unittest
from pathlib import Path

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



class Options(unittest.TestCase):
    def test_color_has_a_hidden_british_alias(self):
        sys.path.insert(0, str(ROOT))
        from openfilmkit import cli
        for flag in ('--color', '--colour'):
            self.assertTrue(cli.parser().parse_args(['clean', 'x.jpg', flag]).color, flag)
        clean = cli.parser()._subparsers._group_actions[0].choices['clean']
        self.assertIn('--color', clean.format_help())
        self.assertNotIn('--colour', clean.format_help())

if __name__ == '__main__':
    unittest.main()
