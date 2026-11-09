"""Run job packages on another machine over SSH: `clean ... --on HOST`.

The work goes as job packages (see jobs.py), so this is only transport: pack here, copy the
packages to the host with rsync, run `openfilmkit job run` there, copy each package's out/ back,
check the result against its recipe and put it beside the original. A package's folder name is
its job ID, so running the same command again finds the same packages on the host: finished
ones are only copied back, and stopped ones resume from their checkpoints.

HOST is a name ssh knows (from ~/.ssh/config, say). Settings for a host can go in
~/.config/openfilmkit/hosts.toml (or $OPENFILMKIT_CONFIG/hosts.toml):

    [hosts.pi]
    ssh = "pi.local"                                         # what to give ssh (default: the name)
    openfilmkit = "/home/linuxbrew/.linuxbrew/bin/openfilmkit"   # default: openfilmkit
    jobs = "openfilmkit-jobs"                                # job folder, from the home folder

The host needs the same OpenFilmKit version (jobs run only on the version that packed them),
rsync and an SSH login that doesn't ask for a password each time (a key).
"""
import json
import os
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

from . import __version__, jobs

DEFAULTS = {'openfilmkit': 'openfilmkit', 'jobs': 'openfilmkit-jobs'}


class RemoteError(Exception):
    pass


def config_file():
    base = os.environ.get('OPENFILMKIT_CONFIG')
    return (Path(base) if base else Path.home() / '.config' / 'openfilmkit') / 'hosts.toml'


def host(name):
    """Settings for host `name`: {'name', 'ssh', 'openfilmkit', 'jobs', 'ssh_command'}."""
    settings = {}
    path = config_file()
    if path.is_file():
        try:
            settings = tomllib.loads(path.read_text()).get('hosts', {}).get(name, {})
        except tomllib.TOMLDecodeError as e:
            raise RemoteError(f'{path}: {e}') from e
    out = {**DEFAULTS, 'ssh': name, 'ssh_command': 'ssh', **settings, 'name': name}
    jobs_dir = str(out['jobs'])
    out['jobs'] = jobs_dir[2:] if jobs_dir.startswith('~/') else jobs_dir   # ssh starts in the home folder
    return out


def _ssh(h, command, capture=True):
    """Run `command` (a list) on the host; stderr comes through to the user."""
    argv = shlex.split(h['ssh_command']) + [h['ssh'], shlex.join(command)]
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE if capture else None, text=True)
    except FileNotFoundError as e:
        raise RemoteError(f'{argv[0]} not found') from e
    return r


def _rsync(h, *args):
    if not shutil.which('rsync'):
        raise RemoteError('rsync not found here')
    argv = ['rsync', '-a', '--partial', '-e', h['ssh_command'], *args]
    if subprocess.run(argv).returncode:
        raise RemoteError(f'copying to or from {h["name"]} failed (rsync)')


def check(h):
    """The host's OpenFilmKit must be this version."""
    r = _ssh(h, shlex.split(h['openfilmkit']) + ['--version'])
    found = (r.stdout or '').strip()
    if r.returncode or not found:
        raise RemoteError(f"couldn't run {h['openfilmkit']!r} on {h['name']} (set its path in {config_file()})")
    if found != __version__:
        raise RemoteError(f'{h["name"]} has OpenFilmKit {found}; jobs from here need {__version__}')


def run_on(name, packages, to=None, keep=False, say=print):
    """Run local job packages on host `name` and collect their results (into folder `to`, or
    beside each original). Returns [recipe or {'package', 'error'}] in the packages' order."""
    h = host(name)
    check(h)
    asked = [Path(p) for p in packages]
    packages = list(dict.fromkeys(asked))   # the same work twice is one package
    remote = [f'{h["jobs"]}/{p.name}' for p in packages]
    r = _ssh(h, ['mkdir', '-p', h['jobs']])
    if r.returncode:
        raise RemoteError(f'could not make {h["jobs"]} on {h["name"]}')
    todo = [p for p in packages if not jobs.done(p)]
    failed = {}
    if todo:
        say(f'Copying {len(todo)} job{"s" if len(todo) != 1 else ""} to {h["name"]}...')
        _rsync(h, *[str(p) for p in todo], f'{h["ssh"]}:{h["jobs"]}/')
        say(f'Running on {h["name"]}...')
        r = _ssh(h, shlex.split(h['openfilmkit']) + ['job', 'run', '--json'] + [f'{h["jobs"]}/{p.name}' for p in todo])
        try:
            for p, rec in zip(todo, json.loads(r.stdout)):
                if 'error' in rec:
                    failed[p] = rec['error']
        except (TypeError, ValueError):
            pass   # cut off (a dropped connection, say): each job is checked below
    results = []
    for p, path in zip(packages, remote):
        try:
            if p in failed:
                raise RemoteError(f'on {h["name"]}: {failed[p]}')
            if not jobs.done(p):
                (p / 'out').mkdir(exist_ok=True)
                _rsync(h, f'{h["ssh"]}:{path}/out/', f'{p}/out/')
            if not jobs.done(p):   # checks the result against its recipe
                raise RemoteError(f'the job did not finish on {h["name"]}; run the same command again to resume it')
            record = jobs.collect(p, to)
            if not keep:   # collected and checked: the copies there and here can go
                _ssh(h, shlex.split(h['openfilmkit']) + ['job', 'remove', path])
                jobs.remove(p)
            results.append(record)
        except (RemoteError, jobs.JobError) as e:
            results.append({'package': str(p), 'error': str(e)})
    by_package = dict(zip(packages, results))
    return [by_package[p] for p in asked]


def outbox(name):
    """Where packages for host `name` are made: a cache folder kept until they're collected."""
    base = Path(os.environ.get('OPENFILMKIT_CACHE') or Path.home() / '.cache' / 'openfilmkit')
    return base / 'outbox' / name
