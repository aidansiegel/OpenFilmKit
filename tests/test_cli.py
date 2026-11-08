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


if __name__ == '__main__':
    unittest.main()
