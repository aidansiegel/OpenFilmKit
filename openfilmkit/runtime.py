"""The kit's SANE runtime: where it is, and how to run its programs.

A runtime is a prefix written by tools/build_sane.py, recognized by its
build-provenance.json. OPENFILMKIT_RUNTIME selects one explicitly; otherwise
the newest under ~/.local/share/openfilmkit/runtime is used.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

RUNTIMES = Path.home() / '.local' / 'share' / 'openfilmkit' / 'runtime'


class RuntimeMissing(Exception):
    pass


class Runtime:
    def __init__(self, prefix):
        self.prefix = Path(prefix).expanduser().resolve()
        # As given, for paths shown to people: Homebrew's opt/ link survives upgrades, the
        # resolved Cellar path doesn't.
        self.given = Path(prefix).expanduser().absolute()
        self.provenance_path = self.prefix / 'build-provenance.json'
        if not self.provenance_path.is_file():
            raise RuntimeMissing(f'{self.prefix} is not a kit runtime (no build-provenance.json)')
        self.provenance = json.loads(self.provenance_path.read_text())
        self.scanimage = self.prefix / 'bin' / 'scanimage'
        self.config = self.prefix / 'etc' / 'sane.d'
        # SANE's fake test scanner is enabled only for OpenFilmKit's own tests.
        if os.environ.get('OPENFILMKIT_TEST_BACKEND') == '1':
            self.config = self.prefix / 'etc' / 'sane.d-test'
            if not self.config.is_dir():
                raise RuntimeMissing(f'{self.prefix} has no test-backend config (etc/sane.d-test)')

    @classmethod
    def find(cls, prefix=None):
        prefix = prefix or os.environ.get('OPENFILMKIT_RUNTIME')
        if prefix:
            return cls(prefix)
        candidates = sorted((p for p in RUNTIMES.glob('*') if (p / 'build-provenance.json').is_file()),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeMissing(f'no kit runtime under {RUNTIMES}; build one with tools/build_sane.py')
        return cls(candidates[0])

    def env(self):
        # Always this runtime's own config, whatever the caller's environment says.
        env = dict(os.environ, SANE_CONFIG_DIR=str(self.config), LC_ALL='C')
        env.pop('SANE_DEBUG_DLL', None)
        return env

    def run(self, args, timeout=60):
        return subprocess.run([str(self.scanimage), *map(str, args)], env=self.env(),
                              capture_output=True, text=True, timeout=timeout, check=True)

    def version(self):
        return self.run(['--version']).stdout.strip()

    def summary(self):
        p = self.provenance
        return {'prefix': str(self.prefix), 'commit': p.get('commit'),
                'patches': [x['file'] for x in p.get('patches', [])],
                'backends': p.get('backends'), 'tests': p.get('tests'),
                'built_utc': p.get('built_utc')}


UDEV_RULES = '60-openfilmkit.rules'
UDEV_DIRS = ('/etc/udev/rules.d', '/usr/lib/udev/rules.d', '/lib/udev/rules.d')


def udev_status(runtime, platform=None, dirs=UDEV_DIRS):
    """On Linux: is OpenFilmKit's USB rule installed, and if not, the command that installs it.
    None on other systems (they need no rule)."""
    platform = platform or sys.platform
    if not platform.startswith('linux'):
        return None
    shipped = runtime.prefix / 'share' / 'openfilmkit' / UDEV_RULES
    shown = getattr(runtime, 'given', runtime.prefix) / 'share' / 'openfilmkit' / UDEV_RULES
    installed = next((str(Path(d) / UDEV_RULES) for d in dirs if (Path(d) / UDEV_RULES).is_file()), None)
    status = {'installed': installed, 'shipped': str(shipped) if shipped.is_file() else None}
    if not installed and status['shipped']:
        status['fix'] = (f'sudo install -m 644 {shown} /etc/udev/rules.d/{UDEV_RULES} && '
                         'sudo udevadm control --reload && sudo udevadm trigger')
    return status


def tiffinfo():
    path = shutil.which('tiffinfo') or '/opt/homebrew/bin/tiffinfo'
    if not Path(path).exists():
        raise RuntimeMissing('tiffinfo (libtiff) is required to verify captures')
    return path
