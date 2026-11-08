import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import build_sane  # noqa: E402
from openfilmkit import runtime  # noqa: E402

PIN = json.loads((ROOT / 'packaging' / 'sane-pin.json').read_text())
FORMULA = (ROOT / 'packaging' / 'homebrew' / 'openfilmkit.rb').read_text()


class Formula(unittest.TestCase):
    def test_formula_builds_the_pinned_commit(self):
        self.assertIn(f'revision: "{PIN["commit"]}"', FORMULA)

    def test_formula_installs_the_pinned_python_packages(self):
        reqs = (ROOT / 'packaging' / 'requirements-brew.txt').read_text()
        self.assertIn('"--require-hashes"', FORMULA)
        self.assertIn('packaging/requirements-brew.txt', FORMULA)
        for name in ('numpy', 'pillow', 'opencv-python-headless', 'setuptools'):
            self.assertRegex(reqs, rf'(?m)^{re.escape(name)}==\S+ \\$', name)
        self.assertNotRegex(reqs, r'(?m)^opencv-python-headless==5\.', 'clean is tested on OpenCV 4.x')

    def test_formula_test_checks_the_patch(self):
        for patch in (ROOT / 'patches' / 'series').read_text().split():
            if patch.endswith('.diff'):
                self.assertIn(patch, FORMULA)

    def test_pyproject_ships_the_data(self):
        text = (ROOT / 'pyproject.toml').read_text()
        for pattern in ('data/*.json', 'data/profiles/*.json.gz', 'scanners/*/*.json'):
            self.assertIn(pattern, text)
        self.assertIn('openfilmkit = "openfilmkit.cli:run"', text)


class DllConf(unittest.TestCase):
    def test_fake_scanner_only_in_the_test_config(self):
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d, 'etc', 'sane.d')
            conf.mkdir(parents=True)
            (conf / 'genesys.conf').write_text('usb 0x07b3 0x1825\n')
            build_sane.write_dll_conf(d, ['genesys', 'test'])
            enabled = lambda f: [l for l in f.read_text().splitlines() if l and not l.startswith('#')]
            self.assertEqual(enabled(conf / 'dll.conf'), ['genesys'])
            test = Path(d, 'etc', 'sane.d-test')
            self.assertEqual(enabled(test / 'dll.conf'), ['genesys', 'test'])
            self.assertTrue((test / 'genesys.conf').is_file())


class UdevRules(unittest.TestCase):
    TABLE = {'sane_commit': 'abc', 'backends': {'genesys': [
        {'usb': {'vendor': '07b3', 'product': '1825', 'bcd': None}},
        {'usb': {'vendor': '07b3', 'product': '0c3b', 'bcd': '0400'}},
        {'usb': {'vendor': '07b3', 'product': '0c3b', 'bcd': '0605'}},
    ]}}

    def test_one_rule_per_usb_id(self):
        with tempfile.TemporaryDirectory() as d:
            text = build_sane.write_udev_rules(d, self.TABLE).read_text()
        rules = [line for line in text.splitlines() if line.startswith('ATTR{idVendor}')]
        self.assertEqual(rules, [
            'ATTR{idVendor}=="07b3", ATTR{idProduct}=="0c3b", MODE="0660", TAG+="uaccess"',
            'ATTR{idVendor}=="07b3", ATTR{idProduct}=="1825", MODE="0660", TAG+="uaccess"'])
        self.assertIn('LABEL="openfilmkit_end"', text)

    def fake_runtime(self, d, shipped=True):
        if shipped:
            rules = Path(d, 'share', 'openfilmkit', runtime.UDEV_RULES)
            rules.parent.mkdir(parents=True)
            rules.write_text('# rule\n')
        return mock.Mock(prefix=Path(d), given=Path(d))

    def test_doctor_offers_the_install_command_on_linux(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as etc:
            status = runtime.udev_status(self.fake_runtime(d), 'linux', dirs=[etc])
            self.assertIsNone(status['installed'])
            self.assertIn(f'/etc/udev/rules.d/{runtime.UDEV_RULES}', status['fix'])
            self.assertIn('udevadm trigger', status['fix'])
            Path(etc, runtime.UDEV_RULES).write_text('# installed\n')
            status = runtime.udev_status(self.fake_runtime(d + '-2', shipped=False), 'linux', dirs=[etc])
            self.assertTrue(status['installed'])
            self.assertNotIn('fix', status)

    def test_install_command_uses_the_path_as_given(self):
        # Homebrew points the launcher at opt/…/sane, a link to the versioned Cellar folder.
        with tempfile.TemporaryDirectory() as d:
            cellar = Path(d, 'Cellar', 'sane')
            self.fake_runtime(cellar)
            (cellar / 'build-provenance.json').write_text('{}')
            opt = Path(d, 'opt-sane')
            opt.symlink_to(cellar)
            rt = runtime.Runtime(opt)
            status = runtime.udev_status(rt, 'linux', dirs=[d + '/none'])
            self.assertIn(str(opt / 'share'), status['fix'])
            self.assertEqual(status['shipped'], str(cellar.resolve() / 'share' / 'openfilmkit' / runtime.UDEV_RULES))

    def test_no_rule_needed_on_macos(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(runtime.udev_status(self.fake_runtime(d), 'darwin'))


if __name__ == '__main__':
    unittest.main()
