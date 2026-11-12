"""A job queue several machines work from: `clean --queue`, `queue status`, `queue collect` and
`openfilmkit worker`.

The queue is a folder, on a machine reachable over SSH ("host:path") or mounted here (a path).
Nothing runs there: workers use only rsync, so each can have a key restricted to reading the
queue and writing its own drop folder.

  jobs/<id>/                       job packages (see jobs.py), put there by `clean --queue`
  drop/<worker>/worker.json        what the worker offers: version, memory, hours; when last seen
  drop/<worker>/leases/<id>.json   its claim on a job, renewed every RENEW s, void after LEASE s
  drop/<worker>/checkpoints/<id>/  the slow stages of a job under way, pushed as they finish
  drop/<worker>/results/<id>/out/  a finished result, its recipe last
  drop/<worker>/failed/<id>/why.json  a job that failed there, and why
  jobs/<id>/rejected.json          results collecting turned down (when it couldn't delete them)

A worker picks the oldest job it can do: the build that packed it, an estimated peak memory
within its limit (and what the machine has free), inside its working hours, not done, and with no
live claim by another worker. It writes a claim, waits SETTLE seconds and looks again; if another
worker claimed the same job, the earlier claim wins (then the worker's name) and the other backs
off. A job whose worker stopped renewing its claim is free again, and the next worker starts from
the checkpoints the last one pushed, so the work isn't lost. A worker that can't renew its claim
for a whole LEASE stops the job, and one that finds a good result already there doesn't publish
its own.

A worker shares its machine. It takes no job, and stops one under way, while a systemd unit named
in yield_to uses more than yield_mb of memory or a command in busy_when succeeds (a tape capture
that can't spare a moment of CPU, say). It can also be frozen (`systemctl freeze`) for hours and
thawed: it then counts from the thaw, retrying its connections for a whole lease before giving up
the job, and carries on unless another worker took the job meanwhile. Every rsync gives up on a
connection that has gone quiet, so one that died during a freeze doesn't hang the worker.

Claims keep workers out of each other's way; they can't make two runs of one job impossible
(rsync offers nothing atomic, and the machines' clocks may differ a little). That's safe by
design: a job gives the same file wherever it runs, each worker writes only its own drop folder,
and collecting keeps the first result that checks out. The worst case is a job done twice.

Collecting runs where the photos are, with SSH access to the queue's machine: each result is
checked against its recipe and its job before it's put beside each original, and the job is then
cleared from the queue (workers clear their own drop folders once a job is gone). A result that
doesn't check out is deleted or, where collecting may not delete it, marked rejected, and the job
runs again. Only jobs packed on this machine are put back beside their originals; others go to a
folder named with --to.
"""
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, jobs
from .remote import config_file

LEASE, RENEW, SETTLE, POLL, CHECK = 600, 120, 5, 60, 15
QUIET = 120        # seconds an rsync waits on a connection that sends nothing before giving up
NAME = re.compile(r'^[A-Za-z0-9._-]+$')
JOB_ID = re.compile(r'^[0-9a-f]{64}$')
SLOW_STAGES = ('scratch_probability.npz', 'fill-*.npz')   # worth pushing; the rest is quick to redo


class QueueError(Exception):
    pass


class Interrupted(Exception):
    """A job stopped for a reason of the worker's own (another program needs the memory, or the
    claim was lost): its slow work is pushed and the job left for later."""


def _config():
    import tomllib
    path = config_file()
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise QueueError(f'{path}: {e}') from e


def queue_settings(name=None):
    """{'name', 'queue', 'ssh_command'} for a queue in hosts.toml's [queues.NAME]; with no name,
    the only one there."""
    queues = _config().get('queues', {})
    if name is None:
        if len(queues) != 1:
            raise QueueError(f'name the queue (configured in {config_file()}: {", ".join(queues) or "none"})')
        name = next(iter(queues))
    if name not in queues:
        raise QueueError(f'no queue {name!r} in {config_file()}')
    q = queues[name]
    if 'queue' not in q:
        raise QueueError(f'[queues.{name}] needs queue = "host:path"')
    return {'name': name, 'queue': q['queue'], 'ssh_command': q.get('ssh_command', 'ssh')}


def _split(spec):
    """(host, path) for "host:path", or (None, path) for a local folder."""
    head, sep, tail = spec.partition(':')
    if sep and '/' not in head and head:
        return head, tail
    return None, spec


def _rsync(ssh_command, *args, quiet=False):
    argv = ['rsync', '-a', '--partial', f'--timeout={QUIET}', '-e', ssh_command, *args]
    r = subprocess.run(argv, stdout=subprocess.DEVNULL if quiet else None,
                       stderr=subprocess.DEVNULL if quiet else None)
    return r.returncode == 0


def _now():
    return time.time()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, indent=1) + '\n')
    os.replace(tmp, path)


# ---------------------------------------------------------------- what the queue holds

class View:
    """A local copy of the queue's small files (no photos), read into dicts."""

    def __init__(self, folder):
        folder = Path(folder)
        self.jobs, self.workers, self.leases, self.results, self.failed, self.checkpoints = {}, {}, {}, {}, {}, {}
        self.drops = []   # every worker with a drop folder
        self.rejected = {}   # {job: [(worker, when that run finished)]}
        for p in sorted((folder / 'jobs').glob('*/job.json')):
            data = _read_json(p)
            if data and JOB_ID.match(p.parent.name):
                self.jobs[p.parent.name] = (data['job'], data['about'])
                turned_down = _read_json(p.parent / 'rejected.json') or []
                self.rejected[p.parent.name] = [(r.get('worker'), r.get('finished_utc')) for r in turned_down]
        self.mine = {}       # {worker: {kind: [job]}}: what each drop folder holds
        for w in sorted((folder / 'drop').glob('*')):
            if not NAME.match(w.name):
                continue
            self.drops.append(w.name)
            info = _read_json(w / 'worker.json')
            if info:
                self.workers[w.name] = info
            for p in (w / 'leases').glob('*.json'):
                lease = _read_json(p)
                if lease:
                    self.leases.setdefault(p.stem, []).append(dict(lease, worker=w.name))
            held = self.mine.setdefault(w.name, {'results': [], 'checkpoints': [], 'failed': [], 'rejected': []})
            for p in (w / 'results').glob('*/out/*.json'):
                jid = p.parent.parent.name
                held['results'].append(jid)
                recipe = _read_json(p) or {}
                if (w.name, recipe.get('job', {}).get('finished_utc')) in self.rejected.get(jid, []):
                    held['rejected'].append(jid)
                else:
                    self.results.setdefault(jid, []).append(w.name)
            for p in (w / 'failed').glob('*/why.json'):
                held['failed'].append(p.parent.name)
                self.failed.setdefault(p.parent.name, []).append(dict(_read_json(p) or {}, worker=w.name))
            for p in (w / 'checkpoints').glob('*'):
                if p.is_dir():
                    held['checkpoints'].append(p.name)
                    self.checkpoints.setdefault(p.name, []).append(w.name)

    def live_leases(self, job_id, now):
        return [lease for lease in self.leases.get(job_id, []) if lease.get('until', 0) > now]


def pull_view(spec, ssh_command, folder):
    """Bring the queue's small files (job descriptions, leases, recipes, worker notes, and which
    workers hold checkpoints) into `folder`, and read them."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    # --ignore-times: these small files can change within a second at the same size (a redone
    # result's recipe), which rsync's quick check would miss.
    ok = _rsync(ssh_command, '--delete', '--ignore-times', '--include=/jobs/', '--include=/jobs/*/',
                '--include=/jobs/*/job.json',
                '--include=/jobs/*/rejected.json',
                '--include=/drop/', '--include=/drop/*/', '--include=/drop/*/worker.json',
                '--include=/drop/*/leases/', '--include=/drop/*/leases/*.json',
                '--include=/drop/*/failed/', '--include=/drop/*/failed/*/', '--include=/drop/*/failed/*/why.json',
                '--include=/drop/*/checkpoints/', '--include=/drop/*/checkpoints/*/',
                '--include=/drop/*/results/', '--include=/drop/*/results/*/', '--include=/drop/*/results/*/out/',
                '--include=/drop/*/results/*/out/*.json', '--exclude=*', f'{spec}/', f'{folder}/', quiet=True)
    if not ok:
        raise QueueError(f"couldn't read the queue at {spec}")
    return View(folder)


# ---------------------------------------------------------------- submitting and collecting

def _cache():
    return Path(os.environ.get('OPENFILMKIT_CACHE') or Path.home() / '.cache' / 'openfilmkit')


def outbox(q):
    """Where packages for queue `q` are made and kept until collected."""
    return _cache() / 'queues' / q['name'] / 'outbox'


def _remote(q, *command, quiet=False):
    """Run a command in the queue's machine (or here, for a local queue)."""
    host, _ = _split(q['queue'])
    argv = list(command) if host is None else shlex.split(q['ssh_command']) + [host, shlex.join(command)]
    return subprocess.run(argv, stderr=subprocess.DEVNULL if quiet else None).returncode == 0


def _path(q, *parts):
    return '/'.join([_split(q['queue'])[1].rstrip('/'), *parts])


def _free_bytes(q):
    """Free space where the queue is, or None if it can't be read."""
    host, path = _split(q['queue'])
    command = ['df', '-Pk', path]
    try:
        if host is None:
            out = subprocess.run(command, capture_output=True, text=True).stdout
        else:
            out = subprocess.run(shlex.split(q['ssh_command']) + [host, shlex.join(command)],
                                 capture_output=True, text=True).stdout
        return int(out.splitlines()[-1].split()[3]) * 1024
    except (OSError, ValueError, IndexError):
        return None


SPARE = 2 * 1024 ** 3   # what submitting leaves free on the queue's machine


def submit(q, packages, say=print):
    """Put local job packages into the queue, if they fit on its machine with SPARE left over."""
    if not _remote(q, 'mkdir', '-p', _path(q, 'jobs'), _path(q, 'drop')):
        raise QueueError(f"couldn't reach the queue at {q['queue']}")
    need = sum(f.stat().st_size for p in packages for f in Path(p).rglob('*') if f.is_file())
    free = _free_bytes(q)
    if free is not None and need > free - SPARE:
        raise QueueError(f"these jobs need {need / 1e9:.1f} GB and the queue's machine has {free / 1e9:.1f} GB free; "
                         f'queue fewer at a time (collected jobs free their space)')
    if not _rsync(q['ssh_command'], *[str(Path(p)) for p in packages], f"{q['queue'].rstrip('/')}/jobs/"):
        raise QueueError(f"couldn't copy the jobs to {q['queue']}")
    say(f"Queued {len(packages)} job{'s' if len(packages) != 1 else ''} on {q['name']}.")


def status(q, now=None):
    """{'jobs': [{'id', 'photo', 'state', ...}], 'workers': [...]} for the queue."""
    now = _now() if now is None else now
    view = pull_view(q['queue'], q['ssh_command'], _cache() / 'queues' / q['name'] / 'view')
    rows = []
    for jid, (job, about) in sorted(view.jobs.items(), key=lambda kv: (kv[1][1].get('packed_utc', ''), kv[0])):
        row = {'id': jid, 'photo': about.get('source'), 'output': about.get('output_name'),
               'memory_gb': jobs.memory_needed(job, about), 'version': job.get('openfilmkit')}
        live = view.live_leases(jid, now)
        if jid in view.results:
            row['state'], row['worker'] = 'done', view.results[jid][0]
        elif live:
            row['state'], row['worker'] = 'running', live[0]['worker']
            row['claim_until'] = live[0]['until']
        elif jid in view.failed:
            row['state'], row['worker'] = 'failed', view.failed[jid][0]['worker']
            row['error'] = view.failed[jid][0].get('error')
        elif jid in view.checkpoints:
            row['state'], row['worker'] = 'stopped', view.checkpoints[jid][0]
        else:
            row['state'] = 'waiting'
        rows.append(row)
    workers = [dict(info, name=name, seen_ago_s=round(now - info.get('seen', 0))) for name, info in view.workers.items()]
    return {'queue': q['name'], 'jobs': rows, 'workers': workers}


def _reject(q, view, jid, worker, package):
    """Mark a worker's result for a job as turned down, where collecting may not delete it (the
    worker clears it, and the job runs again)."""
    recipe = next((package / 'out').glob('*.json'), None)
    run = (_read_json(recipe) or {}).get('job', {}).get('finished_utc') if recipe else None   # that run alone
    turned_down = [{'worker': w, 'finished_utc': f} for w, f in view.rejected.get(jid, [])]
    turned_down.append({'worker': worker, 'finished_utc': run})
    note = _cache() / 'queues' / q['name'] / 'rejected' / jid / 'rejected.json'
    _write_json(note, turned_down)
    _rsync(q['ssh_command'], str(note), f"{q['queue'].rstrip('/')}/jobs/{jid}/", quiet=True)


def collect(q, to=None, say=print):
    """Bring back every finished job: check it, put it beside the original (or into `to`), and
    clear it from the queue. Returns [recipe or {'id', 'error'}]."""
    view = pull_view(q['queue'], q['ssh_command'], _cache() / 'queues' / q['name'] / 'view')
    box = outbox(q)
    out = []
    for jid in sorted(view.results):
        if jid not in view.jobs:
            continue
        package = box / jid
        if not (package / 'job.json').is_file():
            # Packed on another machine: its job.json says where the result should go, and that's
            # not for a stranger to choose. Its description will do, sent where we're told.
            if to is None:
                out.append({'id': jid, 'error': 'packed on another machine: collect it with --to DIR'})
                continue
            (package / 'out').mkdir(parents=True, exist_ok=True)
            _rsync(q['ssh_command'], f"{q['queue'].rstrip('/')}/jobs/{jid}/job.json", f'{package}/', quiet=True)
        try:
            jobs.read(package)   # the results are judged by it: without it (not fetched, say), leave them be
        except jobs.JobError as e:
            out.append({'id': jid, 'error': str(e)})
            continue
        record = None
        for worker in view.results[jid]:
            shutil.rmtree(package / 'out', ignore_errors=True)
            (package / 'out').mkdir(parents=True)
            src = f"{q['queue'].rstrip('/')}/drop/{worker}/results/{jid}/out/"
            got = _rsync(q['ssh_command'], src, f'{package}/out/', quiet=True)
            candidate = jobs.done(package) if got else None
            if candidate and candidate.get('job', {}).get('id') == jid \
                    and candidate.get('source_sha256') == view.jobs[jid][0]['input']['sha256']:
                record = candidate
                break
            say(f'{jid[:12]}: the result from {worker} does not check out; turning it down')
            if not _remote(q, 'rm', '-rf', '--', _path(q, 'drop', worker, 'results', jid), quiet=True):
                _reject(q, view, jid, worker, package)
        if record is None:
            out.append({'id': jid, 'error': 'no result checked out; the job is free to run again'})
            continue
        try:
            records = jobs.collect(package, to)   # one per destination
        except jobs.JobError as e:
            out.append({'id': jid, 'error': str(e)})
            continue
        _remote(q, 'rm', '-rf', '--', _path(q, 'jobs', jid))
        # The workers' files, where collecting may delete them (otherwise each worker clears its own).
        paths = [_path(q, 'drop', w, sub, jid) for w in view.drops for sub in ('results', 'checkpoints')]
        paths += [_path(q, 'drop', w, 'failed', jid) for w in view.drops]
        if paths:
            _remote(q, 'rm', '-rf', '--', *paths, quiet=True)
        shutil.rmtree(package, ignore_errors=True)
        for record in records:
            say(f"{jid[:12]}: {record['output']} (from {record.get('job', {}).get('ran_on')})")
        out += records
    return out


# ---------------------------------------------------------------- the worker

def worker_settings(**given):
    """The worker's settings: hosts.toml's [worker] table, then `given` (from the command line)."""
    s = {'name': socket.gethostname().split('.')[0], 'ssh_command': 'ssh', 'memory_gb': 2.5, 'hours': None,
         'lease': LEASE, 'renew': RENEW, 'settle': SETTLE, 'poll': POLL, 'check': CHECK,
         'yield_to': [], 'yield_mb': 300, 'busy_when': []}
    s.update(_config().get('worker', {}))
    s.update({k: v for k, v in given.items() if v is not None})
    if not s.get('queue'):
        raise QueueError(f'say which queue to work from (--queue host:path, or [worker] queue in {config_file()})')
    if not NAME.match(s['name']):
        raise QueueError(f'worker names use letters, digits, ".", "_" and "-": {s["name"]!r}')
    s.setdefault('drop', f"{s['queue'].rstrip('/')}/drop/{s['name']}")
    s['state'] = Path(s.get('state') or _cache() / 'worker')
    s['memory_gb'] = float(s['memory_gb'])
    for key in ('yield_to', 'busy_when'):
        if isinstance(s[key], str):
            s[key] = [s[key]]
    return s


def busy(units, over_mb):
    """The first of these systemd units using more than `over_mb` MB of memory, or None: another
    program at work that a job here would compete with for memory."""
    for unit in units:
        try:
            r = subprocess.run(['systemctl', 'show', '--property=MemoryCurrent', '--value', unit],
                               capture_output=True, text=True, timeout=10)
            used = int(r.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue   # not running, not systemd, or no figure ("[not set]")
        if used > over_mb * 1024 * 1024:
            return unit
    return None


def busy_command(commands):
    """The first of these shell commands that succeeds (exit status 0), or None: something on the
    machine says a job shouldn't run now. One that takes more than 10 s counts as saying so too,
    since then it can't be told otherwise."""
    for command in commands:
        try:
            r = subprocess.run(command, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except subprocess.TimeoutExpired:
            return command
        except OSError:
            continue
        if r.returncode == 0:
            return command
    return None


def in_hours(hours, at=None):
    """True inside working hours "START-END" (local hours, 24 h clock; END before START wraps
    past midnight, so "22-7" is overnight); always true when no hours are set."""
    if not hours:
        return True
    start, end = (int(v) for v in str(hours).split('-'))
    h = time.localtime(at).tm_hour
    return start <= h < end if start <= end else (h >= start or h < end)


def memory_free_gb():
    """Memory the machine has available (Linux), or None where that can't be read."""
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return None


def run_in_subprocess(package, beat, every):
    """Run a package with `openfilmkit job run` in its own process (so each job's memory is
    returned when it ends), calling beat() every `every` seconds while it runs. A job that fails
    there raises JobError with the reason it gave."""
    with tempfile.TemporaryFile('w+') as report:   # its --json report (a file: a pipe could fill up)
        proc = subprocess.Popen([sys.executable, '-m', 'openfilmkit', 'job', 'run', '--json', str(package)],
                                stdout=report)
        try:
            while True:
                try:
                    proc.wait(timeout=every)
                    break
                except subprocess.TimeoutExpired:
                    beat()
        except BaseException:
            proc.terminate()
            proc.wait()
            raise
        if not jobs.done(package):
            report.seek(0)
            try:
                reason = json.load(report)[0]['error']
            except (ValueError, LookupError, TypeError):   # no report (it crashed): its log says why
                reason = f'job run ended with status {proc.returncode}'
            raise jobs.JobError(reason)


class Worker:
    def __init__(self, settings, run=run_in_subprocess, now=_now, say=print):
        self.s, self.run, self.now, self.say = settings, run, now, say
        self.state = settings['state']
        self.mirror = self.state / 'drop'         # what this worker writes, pushed to its drop folder
        self.current = None
        self._pushed = True

    # -- writing to the drop folder
    def _push(self):
        ok = _rsync(self.s['ssh_command'], '--exclude=.*', '--exclude=/results/', f'{self.mirror}/',
                    f"{self.s['drop']}/", quiet=True)
        # A result always replaces what's there: a redone one can match a turned-down one in size
        # and (to the second) in time, which rsync would otherwise take for the same file.
        (self.mirror / 'results').mkdir(parents=True, exist_ok=True)
        ok &= _rsync(self.s['ssh_command'], '--ignore-times', '--exclude=.*', f'{self.mirror}/results/',
                     f"{self.s['drop']}/results/", quiet=True)
        ok &= _rsync(self.s['ssh_command'], '--delete', '--exclude=.*', f'{self.mirror}/leases/',
                     f"{self.s['drop']}/leases/", quiet=True)
        self._pushed = ok
        return ok

    def publish(self):
        _write_json(self.mirror / 'worker.json', {
            'name': self.s['name'], 'version': __version__, 'build': jobs.build(), 'memory_gb': self.s['memory_gb'],
            'hours': self.s['hours'], 'seen': self.now(), 'running': self.current})
        (self.mirror / 'leases').mkdir(parents=True, exist_ok=True)
        self._push()

    def view(self):
        return pull_view(self.s['queue'], self.s['ssh_command'], self.state / 'view')

    def other_work(self):
        """What on this machine a job would get in the way of, or None."""
        unit = busy(self.s['yield_to'], self.s['yield_mb'])
        if unit:
            return unit
        command = busy_command(self.s['busy_when'])
        return f'busy_when ({command[:60]})' if command else None

    def taken_meanwhile(self, jid):
        """Whether another worker holds a live claim on the job (False if the queue can't be read:
        then renewing decides)."""
        try:
            live = self.view().live_leases(jid, self.now())
        except QueueError:
            return False
        return any(lease['worker'] != self.s['name'] for lease in live)

    # -- choosing and claiming
    def candidates(self, view):
        now, free = self.now(), memory_free_gb()
        mine = set()
        for jid, failures in view.failed.items():
            if any(f['worker'] == self.s['name'] for f in failures):
                mine.add(jid)
        out = []
        for jid, (job, about) in view.jobs.items():
            need = jobs.memory_needed(job, about)
            if jid in view.results or jid in mine or job.get('openfilmkit') != __version__ \
                    or job.get('build') != jobs.build():
                continue
            if need > self.s['memory_gb'] or (free is not None and need > free):
                continue
            if [lease for lease in view.live_leases(jid, now) if lease['worker'] != self.s['name']]:
                continue
            out.append((about.get('packed_utc', ''), jid))
        return [jid for _, jid in sorted(out)]

    def tidy(self, view):
        """Clear this worker's drop folder of jobs that are gone from the queue (collected) and of
        its results that collecting turned down, and first its own copies of them here: the next
        push would put a copy left in the mirror back, and a job's package holds its photo."""
        held = view.mine.get(self.s['name'], {})
        gone = {jid for kind in ('results', 'checkpoints', 'failed') for jid in held.get(kind, [])
                if jid not in view.jobs}
        refused = set(held.get('rejected', []))
        for folder in (self.mirror / 'results', self.mirror / 'checkpoints', self.mirror / 'failed', self.state / 'jobs'):
            for p in folder.glob('*'):
                if JOB_ID.match(p.name) and p.name not in view.jobs:
                    shutil.rmtree(p, ignore_errors=True)
        for jid in refused:
            shutil.rmtree(self.mirror / 'results' / jid, ignore_errors=True)
        if not gone | refused:
            return
        for kind in ('results', 'checkpoints', 'failed'):
            for jid in sorted(gone | (refused if kind == 'results' else set())):
                self._empty(kind, jid)

    def _empty(self, kind, jid):
        """Empty drop/<kind>/<jid>/ (syncing an empty folder into it works with every rsync; the
        empty folder left behind counts for nothing)."""
        empty = self.state / 'empty'
        empty.mkdir(parents=True, exist_ok=True)
        _rsync(self.s['ssh_command'], '-r', '--delete', f'{empty}/', f"{self.s['drop']}/{kind}/{jid}/", quiet=True)

    def _lease(self, jid, claimed=None):
        now = self.now()
        lease = self.mirror / 'leases' / f'{jid}.json'
        old = _read_json(lease) or {}
        _write_json(lease, {'job': jid, 'claimed': claimed or old.get('claimed') or now,
                            'until': now + self.s['lease']})

    def release(self, jid):
        (self.mirror / 'leases' / f'{jid}.json').unlink(missing_ok=True)
        self._push()

    def claim(self, jid):
        """Claim a job; False if another worker's claim came first."""
        self._lease(jid, claimed=self.now())
        if not self._push():
            raise QueueError(f"couldn't write to {self.s['drop']}")
        time.sleep(self.s['settle'])
        live = self.view().live_leases(jid, self.now())
        first = min(live, key=lambda lease: (lease.get('claimed', 0), lease['worker']), default=None)
        if first and first['worker'] != self.s['name']:
            self.release(jid)
            return False
        return True

    # -- doing a job
    def _push_checkpoints(self, jid, package):
        made = package / 'work' / 'checkpoints'
        dest = self.mirror / 'checkpoints' / jid
        if made.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            for pattern in SLOW_STAGES:
                for f in made.glob(pattern):
                    if not (dest / f.name).exists():
                        shutil.copyfile(f, dest / f.name)
        self._push()

    def _seed_checkpoints(self, jid, package, view):
        """Start from the checkpoints the worker that got furthest pushed (any worker, this one too)."""
        best = None
        for worker in view.checkpoints.get(jid, []):
            got = self.state / 'seed' / worker
            shutil.rmtree(got, ignore_errors=True)
            got.mkdir(parents=True)
            _rsync(self.s['ssh_command'], f"{self.s['queue'].rstrip('/')}/drop/{worker}/checkpoints/{jid}/",
                   f'{got}/', quiet=True)
            if best is None or len(list(got.glob('*.npz'))) > len(list(best.glob('*.npz'))):
                best = got
        if best is not None:
            into = package / 'work' / 'checkpoints'
            into.mkdir(parents=True, exist_ok=True)
            for f in best.glob('*.npz'):
                shutil.copyfile(f, into / f.name)

    def do(self, jid, view):
        package = self.state / 'jobs' / jid
        package.parent.mkdir(parents=True, exist_ok=True)
        if not _rsync(self.s['ssh_command'], f"{self.s['queue'].rstrip('/')}/jobs/{jid}", f'{package.parent}/',
                      quiet=True):
            raise QueueError(f"couldn't fetch job {jid[:12]}")
        self._seed_checkpoints(jid, package, view)
        self.current = jid
        self.publish()

        renewed = {'at': self.now(), 'ok': self.now(), 'beat': self.now()}

        def beat():   # every `check` seconds while the job runs
            other = self.other_work()
            if other:
                raise Interrupted(f'{other} is at work')
            now = self.now()
            if now - renewed['beat'] > self.s['lease']:      # frozen (or asleep) longer than a lease
                self.say(f'{jid[:12]}: back after {now - renewed["beat"]:.0f} s stopped')
                if self.taken_meanwhile(jid):
                    raise Interrupted('another worker took the job while this one was stopped')
                renewed.update(at=now - self.s['renew'], ok=now)   # renew now, with a whole lease of tries
            renewed['beat'] = now
            if self.now() - renewed['at'] >= self.s['renew']:
                renewed['at'] = self.now()
                self._lease(jid)
                self._push_checkpoints(jid, package)
                self.publish()
                if self._pushed:
                    renewed['ok'] = self.now()
                elif self.now() - renewed['ok'] > self.s['lease']:
                    raise Interrupted("couldn't renew the claim for a whole lease; another worker may have it")
        try:
            self.run(package, beat, self.s['check'])
        except Interrupted as e:
            self.say(f'{jid[:12]}: stopped: {e}')
            self._push_checkpoints(jid, package)
            self.current = None
            self.release(jid)
            return False
        except jobs.JobError as e:
            self.say(f'{jid[:12]}: failed: {e}')
            _write_json(self.mirror / 'failed' / jid / 'why.json', {'job': jid, 'error': str(e), 'at': self.now()})
            self.current = None
            self.release(jid)
            shutil.rmtree(package, ignore_errors=True)   # not tried here again: its photo can go
            return False
        except BaseException:   # stopped (Ctrl-C, systemd): leave the slow work for whoever is next
            self._push_checkpoints(jid, package)
            self.current = None
            self.release(jid)
            raise
        if jid in self.view().results:   # another worker got there first (claims are advisory)
            self.say(f'{jid[:12]}: already done elsewhere; keeping the first result')
            self.current = None
            self.release(jid)
            shutil.rmtree(package, ignore_errors=True)
            return False
        self._empty('results', jid)   # no leftovers from a turned-down run
        result = self.mirror / 'results' / jid / 'out'
        result.mkdir(parents=True, exist_ok=True)
        for f in sorted((package / 'out').iterdir()):   # the image, then its recipe
            shutil.copyfile(f, result / f.name)
        self.current = None
        (self.mirror / 'leases' / f'{jid}.json').unlink(missing_ok=True)
        if self._push():
            shutil.rmtree(self.mirror / 'results' / jid, ignore_errors=True)
            shutil.rmtree(self.mirror / 'checkpoints' / jid, ignore_errors=True)
            shutil.rmtree(package, ignore_errors=True)
        self.say(f'{jid[:12]}: done')
        return True

    def once(self):
        """Do at most one job; returns its ID, or None when there was nothing to do now."""
        self.publish()
        if not in_hours(self.s['hours'], self.now()):
            return None
        view = self.view()
        self.tidy(view)
        other = self.other_work()
        if other:
            self.say(f'waiting: {other} is at work')
            return None
        for jid in self.candidates(view):
            if self.claim(jid):
                self.say(f'{jid[:12]}: {view.jobs[jid][1].get("output_name")}')
                self.do(jid, view)
                return jid
        return None

    def forever(self):
        while True:
            if self.once() is None:
                time.sleep(self.s['poll'])
