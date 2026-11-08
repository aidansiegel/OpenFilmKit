"""Build the kit's SANE runtime from the pinned commit plus patches/.

    python3 tools/build_sane.py --work /path/with/2GB/free \
        --prefix ~/.local/share/openfilmkit/runtime/8ebb7c50

Steps: fetch the pinned commit (git verifies it by hash), apply patches/series
in order, configure with only the pinned backends, build and install the
libraries, scanimage and sane-find-scanner into --prefix, build and run the
genesys unit tests, regenerate openfilmkit/data/sane-models.json from the same
tree, and write build-provenance.json into --prefix.

--work holds the source tree and logs and needs about 2 GB; it may be on an
external drive. --prefix holds the installed runtime (about 10 MB) and must be
on a disk with symlinks (not exFAT). Any failure stops the build.
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / 'packaging' / 'sane-pin.json'
PATCHES = ROOT / 'patches'
MODELS_OUT = ROOT / 'openfilmkit' / 'data' / 'sane-models.json'
MARKER = '.kit-build'
INSTALL_DIRS = ['include', 'lib', 'sanei', 'backend', 'frontend', 'tools']
WORK_SPACE = 2 * 1024 ** 3
PREFIX_SPACE = 200 * 1024 ** 2
UDEV_RULES = '60-openfilmkit.rules'

sys.path.insert(0, str(ROOT / 'tools'))
import extract_models  # noqa: E402
import parse_desc  # noqa: E402


class BuildError(Exception):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def free_bytes(path):
    path = Path(path)
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def run(args, cwd, log, env=None):
    """Run a step, appending its output to `log`; raise with the log's tail."""
    with open(log, 'a') as out:
        out.write(f'\n$ {" ".join(map(str, args))}\n')
        out.flush()
        result = subprocess.run(list(map(str, args)), cwd=cwd, env=env, stdout=out,
                                stderr=subprocess.STDOUT)
    if result.returncode != 0:
        tail = ''.join(Path(log).read_text(errors='replace').splitlines(True)[-25:])
        raise BuildError(f'{args[0]} failed (exit {result.returncode}); see {log}\n{tail}')


def output(args, cwd):
    return subprocess.run(list(map(str, args)), cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def remove_appledouble(tree):
    # macOS writes ._name sidecars on exFAT; autotools and make trip over them.
    for path in Path(tree).rglob('._*'):
        if path.is_file():
            path.unlink()


def series():
    names = [line.strip() for line in (PATCHES / 'series').read_text().splitlines()
             if line.strip() and not line.startswith('#')]
    for name in names:
        if not (PATCHES / name).is_file():
            raise BuildError(f'patches/series lists {name}, which does not exist')
    return [PATCHES / name for name in names]


def fetch(pin, src, log):
    """A clean checkout of the pinned commit at src."""
    if src.exists():
        if not (src / MARKER).exists():
            raise BuildError(f'{src} exists and was not made by this script; choose another --work')
        # On exFAT, deleting a file also deletes its ._ sidecar, which git then
        # fails to find; clear the sidecars before git sees them.
        remove_appledouble(src)
        run(['git', 'checkout', '-q', '--force', pin['commit']], src, log)
        remove_appledouble(src)
        run(['git', 'clean', '-q', '-f', '-d', '-x', '-e', MARKER], src, log)
    else:
        src.mkdir(parents=True)
        (src / MARKER).write_text('Created by tools/build_sane.py; safe to delete.\n')
        run(['git', 'init', '-q'], src, log)
        run(['git', 'remote', 'add', 'origin', pin['repository']], src, log)
        run(['git', 'fetch', '-q', '--depth', '1', 'origin', pin['commit']], src, log)
        run(['git', 'checkout', '-q', '--force', 'FETCH_HEAD'], src, log)
    head = output(['git', 'rev-parse', 'HEAD'], src)
    if head != pin['commit']:
        raise BuildError(f'checked out {head}, expected {pin["commit"]}')


def apply_patches(src, log):
    applied = []
    for patch in series():
        run(['git', 'apply', '--check', patch], src, log)
        run(['git', 'apply', patch], src, log)
        applied.append({'file': patch.name, 'sha256': sha256(patch)})
    return applied


def write_dll_conf(prefix, backends):
    """Enable the real backends this runtime ships; SANE installs a list of all of them.

    SANE's fake `test` scanner is built for OpenFilmKit's own tests but enabled only in a second
    config folder, etc/sane.d-test, which the runtime uses when OPENFILMKIT_TEST_BACKEND=1.
    Otherwise every install would list two fake scanners beside the real one."""
    conf = Path(prefix) / 'etc' / 'sane.d'
    dll_d = conf / 'dll.d'
    if dll_d.is_dir() and any(dll_d.iterdir()):
        raise BuildError(f'{dll_d} is not empty; it would enable other backends')
    real = [b for b in backends if b != 'test']
    (conf / 'dll.conf').write_text(
        '# Written by tools/build_sane.py: the real backends built into this runtime.\n' +
        ''.join(f'{b}\n' for b in real))
    test_conf = Path(prefix) / 'etc' / 'sane.d-test'
    if 'test' in backends:
        if test_conf.exists():
            shutil.rmtree(test_conf)
        shutil.copytree(conf, test_conf)
        (test_conf / 'dll.conf').write_text(
            '# Written by tools/build_sane.py: as sane.d, plus SANE\'s fake test scanner.\n' +
            ''.join(f'{b}\n' for b in backends))


def unit_tests(src, log, env, jobs):
    tests = src / 'testsuite' / 'backend' / 'genesys'
    run(['make', f'-j{jobs}', 'genesys_unit_tests'], tests, log, env)
    result = subprocess.run(['./genesys_unit_tests'], cwd=tests, capture_output=True, text=True)
    with open(log, 'a') as out:
        out.write(result.stdout + result.stderr)
    m = re.search(r'Successes:\s*(\d+)\s+failures:\s*(\d+)', result.stdout + result.stderr)
    if not m:
        raise BuildError(f'could not read the genesys test summary; see {log}')
    passed, failed = int(m.group(1)), int(m.group(2))
    if failed or result.returncode != 0:
        raise BuildError(f'genesys unit tests: {passed} passed, {failed} failed; see {log}')
    return {'genesys_unit_tests': {'passed': passed, 'failed': failed}}


def write_udev_rules(prefix, table):
    """A udev rule letting the logged-in user open every scanner this runtime's backends support.

    Linux only needs it; installing it takes root, so `openfilmkit doctor` prints the command."""
    ids = sorted({(e['usb']['vendor'], e['usb']['product'])
                  for entries in table['backends'].values() for e in entries})
    lines = [f"# OpenFilmKit: scanners supported by its SANE runtime (SANE {table['sane_commit']}).",
             '# Lets the logged-in user open them without root. Generated by tools/build_sane.py.',
             'ACTION!="add|change", GOTO="openfilmkit_end"',
             'SUBSYSTEM!="usb", GOTO="openfilmkit_end"',
             'ENV{DEVTYPE}!="usb_device", GOTO="openfilmkit_end"']
    lines += [f'ATTR{{idVendor}}=="{v}", ATTR{{idProduct}}=="{p}", MODE="0660", TAG+="uaccess"'
              for v, p in ids]
    lines += ['LABEL="openfilmkit_end"', '']
    out = Path(prefix) / 'share' / 'openfilmkit' / UDEV_RULES
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(lines))
    return out


def build(work, prefix, jobs, models_out, source=None):
    """Build into `prefix`. With `source`, use that SANE tree (already the pinned commit, as
    verified by the caller, e.g. Homebrew's git revision check) instead of fetching one."""
    pin = json.loads(PIN.read_text())
    work, prefix = Path(work).expanduser().resolve(), Path(prefix).expanduser().resolve()
    build_space = Path(source).resolve() if source else work
    if free_bytes(build_space) < WORK_SPACE:
        raise BuildError(f'{build_space} needs 2 GB free; {free_bytes(build_space) // 1024 ** 2} MB available')
    if free_bytes(prefix) < PREFIX_SPACE:
        raise BuildError(f'{prefix} needs 200 MB free; {free_bytes(prefix) // 1024 ** 2} MB available')
    if prefix.exists() and any(prefix.iterdir()):
        raise BuildError(f'{prefix} is not empty; each build installs into a fresh prefix')

    short = pin['commit'][:8]
    src = Path(source).resolve() if source else work / f'sane-{short}'
    logs = work / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    log = logs / f'build-{short}-{stamp}.log'

    env = dict(os.environ, BACKENDS=' '.join(pin['backends']), CFLAGS=pin['cflags'],
               CXXFLAGS=pin['cxxflags'], LC_ALL='C')

    if source:
        print(f'using the supplied source tree for {short}')
        if not (src / 'backend' / 'genesys' / 'gl128.cpp').is_file():
            raise BuildError(f'{src} does not look like a sane-backends source tree')
        origin = f'supplied tree, verified by the caller as {pin["commit"]}'
    else:
        print(f'fetching {short}')
        fetch(pin, src, log)
        origin = 'fetched by build_sane.py; git verified the commit hash'
    print('applying patches')
    applied = apply_patches(src, log)
    # git-version-gen prefers this file; a shallow clone has no tags to describe.
    (src / '.tarball-version').write_text(pin['version_label'] + '\n')
    remove_appledouble(src)

    print('configuring')
    run(['./autogen.sh'], src, log, env)
    remove_appledouble(src)
    run(['./configure', f'--prefix={prefix}', *pin['configure']], src, log, env)

    print('building')
    for d in INSTALL_DIRS:
        remove_appledouble(src / d)
        run(['make', f'-j{jobs}'], src / d, log, env)
    print('installing')
    for d in INSTALL_DIRS:
        run(['make', 'install'], src / d, log, env)
    for conf in ['dll.conf', 'genesys.conf']:
        if not (prefix / 'etc' / 'sane.d' / conf).is_file():
            raise BuildError(f'install did not produce etc/sane.d/{conf}')
    write_dll_conf(prefix, pin['backends'])

    print('running genesys unit tests')
    tests = unit_tests(src, log, env, jobs)

    print('generating the driver model table and scanner list')
    table = extract_models.build(src, pin['commit'])
    Path(models_out).write_text(json.dumps(table, indent=1) + '\n')
    scanners_out = Path(models_out).with_name('sane-scanners.json')
    parse_desc.write(src, scanners_out, pin['commit'])
    write_udev_rules(prefix, table)

    provenance = {
        'built_utc': stamp,
        'repository': pin['repository'],
        'commit': pin['commit'],
        'source': origin,
        'version_label': pin['version_label'],
        'patches': applied,
        'backends': pin['backends'],
        'cflags': pin['cflags'],
        'cxxflags': pin['cxxflags'],
        'configure': pin['configure'],
        'compiler': output(['cc', '--version'], src).splitlines()[0],
        'platform': f'{platform.system()} {platform.release()} {platform.machine()}',
        'tests': tests,
        'models_table_sha256': sha256(models_out),
        'physical_scanner_tested': False,
    }
    (prefix / 'build-provenance.json').write_text(json.dumps(provenance, indent=1) + '\n')
    # Keep the full log (unit-test output included) with the runtime: build folders, such as
    # Homebrew's, are deleted after a successful install.
    shutil.copyfile(log, prefix / 'build.log')
    print(f'installed {prefix}\nlog {log}')
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--work', required=True, help='source tree and logs (~2 GB)')
    parser.add_argument('--source', help='an existing sane-backends tree at the pinned commit '
                                          '(Homebrew passes its download); skips fetching')
    parser.add_argument('--prefix', required=True, help='install location (~10 MB, not exFAT)')
    parser.add_argument('--jobs', type=int, default=os.cpu_count() or 2)
    parser.add_argument('--models-out', default=str(MODELS_OUT))
    args = parser.parse_args()
    try:
        build(args.work, args.prefix, args.jobs, args.models_out, args.source)
    except (BuildError, extract_models.TableError) as e:
        sys.exit(f'build_sane: {e}')


if __name__ == '__main__':
    main()
