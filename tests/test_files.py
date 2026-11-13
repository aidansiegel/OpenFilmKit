"""files.place(): a finished file takes its name, and never replaces a file that has it."""
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openfilmkit import files


def no_links(*args, **kw):
    raise OSError(errno.EPERM, 'Operation not permitted')    # as on exFAT


class Place(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.d = Path(self.temp.name)
        self.tmp, self.dest = self.d / '.frame.tif.part', self.d / 'frame.tif'
        self.tmp.write_bytes(b'new scan')

    def both_ways(self):
        yield 'links'
        with mock.patch.object(files.os, 'link', no_links):
            yield 'no links'

    def test_the_finished_file_takes_its_name(self):
        for way in self.both_ways():
            with self.subTest(way):
                self.tmp.write_bytes(b'new scan')
                self.dest.unlink(missing_ok=True)
                files.place(self.tmp, self.dest)
                self.assertEqual((self.dest.read_bytes(), self.tmp.exists()), (b'new scan', False))

    def test_a_file_with_the_name_is_never_replaced(self):
        for way in self.both_ways():
            with self.subTest(way):
                self.dest.write_bytes(b'archival master')
                with self.assertRaises(FileExistsError):
                    files.place(self.tmp, self.dest)
                self.assertEqual((self.dest.read_bytes(), self.tmp.read_bytes()), (b'archival master', b'new scan'))

    def test_without_links_a_failed_rename_takes_its_claim_away(self):
        with mock.patch.object(files.os, 'link', no_links), \
                mock.patch.object(files.os, 'replace', side_effect=OSError(errno.EIO, 'I/O error')):
            with self.assertRaises(OSError):
                files.place(self.tmp, self.dest)
        self.assertEqual((self.dest.exists(), self.tmp.read_bytes()), (False, b'new scan'))

    @unittest.skipIf(os.name == 'nt', 'hard links on the test drive')
    def test_stopped_after_the_link_both_names_are_the_finished_file(self):
        with mock.patch.object(Path, 'unlink', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                files.place(self.tmp, self.dest)
        self.assertTrue(files.mine(self.tmp, self.dest))
        self.assertFalse(files.mine(self.tmp, self.d / 'other.tif'))


if __name__ == '__main__':
    unittest.main()
