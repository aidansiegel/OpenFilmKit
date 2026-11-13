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
  drop/<worker>/failed/<id>/why.json  a job that failed there, why, and after how many tries
  jobs/<id>/rejected.json          results collecting turned down (when it couldn't delete them)
  jobs/<id>/retry.json             failures `queue retry` set aside (each worker clears its own)

A worker picks the oldest job it can do: the build that packed it, an estimated peak memory
within its limit (and what the machine has free), inside its working hours, not done, and with no
live claim by another worker. It writes a claim, waits SETTLE seconds and looks again; if another
worker claimed the same job, the earlier claim wins (then the worker's name) and the other backs
off. A job whose worker stopped renewing its claim is free again, and the next worker starts from
the checkpoints the last one pushed, so the work isn't lost. A worker that can't renew its claim
for a whole LEASE stops the job, and one that finds a good result already there doesn't publish
its own.

A job that fails on a worker is left to the others. The worker itself tries again after a wait
(RETRY) when the failure may pass (the run was killed for its memory, a download dropped), up to
TRIES times, and not at all when the job can't be done (its photo can't be read). `queue retry`
sets failures aside so the workers start over on those jobs, and `queue remove` takes a job out.

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
import math
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
RETRY = (900, 3600)   # seconds a worker waits before its second and third tries at a job it failed
TRIES = len(RETRY) + 1
NAME = re.compile(r'^(?!\.+$)[A-Za-z0-9._-]+$')   # not "." or "..": a name is a folder in the queue
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
    if not isinstance(q, dict) or not isinstance(q.get('queue'), str) or not q['queue']:
        raise QueueError(f'[queues.{name}] needs queue = "host:path"')
    if not isinstance(q.get('ssh_command', 'ssh'), str):
        raise QueueError(f'[queues.{name}] ssh_command is text, in quotes')
    return {'name': name, 'queue': _spec(q['queue']), 'ssh_command': q.get('ssh_command', 'ssh')}


def _split(spec):
    """(host, path) for "host:path", or (None, path) for a local folder."""
    head, sep, tail = spec.partition(':')
    if sep and '/' not in head and head:
        return head, tail
    return None, spec


def _spec(spec):
    """A queue or drop folder as the settings give it, with a leading ~/ dealt with: ssh and rsync
    start in the home folder over there, so it is dropped (quoted for a remote `rm`, it named a
    folder called "~" that wasn't there, and removing nothing counted as done); here it is
    filled in, since nothing else would."""
    host, path = _split(spec)
    if host is None:
        return str(Path(path).expanduser()) if path.startswith('~') else spec
    return f'{host}:{path[2:]}' if path.startswith('~/') else spec


def _rsync(ssh_command, *args, quiet=False, pull=False, said=None):
    """Copy with rsync; True if it worked. `pull`: from the queue to this machine. `said`, a list,
    gets what rsync said on stderr when `quiet` (otherwise it goes to the terminal)."""
    # -rpt, not -a: symbolic links, devices and pipes are left where they are. The queue is a shared
    # folder, and a link planted there (a job's out/ pointing at a folder on the worker, say) would
    # otherwise be brought over and followed.
    # What is fetched loses the setgid bit (--chmod=g-s): the queue's folders have it, and a folder
    # can't be given it where its group isn't one of ours (a cache on another drive, or under /tmp
    # on a Mac), so the copy failed. Leaving out -p isn't enough: macOS's openrsync sets folders'
    # modes regardless, and applies --chmod only with -p.
    argv = ['rsync', '-rpt', *(['--chmod=g-s'] if pull else []), '--partial', f'--timeout={QUIET}',
            '-e', ssh_command, *args]
    r = subprocess.run(argv, stdout=subprocess.DEVNULL if quiet else None,
                       stderr=subprocess.PIPE if quiet else None, text=True, errors='replace')
    if said is not None and r.stderr:
        said.extend(line.strip() for line in r.stderr.splitlines() if line.strip())
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


# What's read from the queue was written by other machines: each file is taken for what it should
# be only if it has that shape, so that one odd file can't stop every worker and every command.
def _dict(value):
    return value if isinstance(value, dict) else {}


def _dicts(value):
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _numbers(record, **defaults):
    """`record` with each of these keys a number: its own, or the default where it's anything else."""
    for key, default in defaults.items():
        if isinstance(record.get(key), bool) or not isinstance(record.get(key), (int, float)):
            record[key] = default
    return record


def _a_job(data):
    """Whether a job.json's content has the shape of one, as far as the queue reads it."""
    job, about = _dict(data).get('job'), _dict(data).get('about')
    return (isinstance(job, dict) and isinstance(about, dict) and isinstance(job.get('options', {}), dict)
            and isinstance(job.get('input'), dict) and isinstance(about.get('packed_utc', ''), str)
            and isinstance(about.get('megapixels') or 0, (int, float)))


# ---------------------------------------------------------------- what the queue holds

class View:
    """A local copy of the queue's small files (no photos), read into dicts."""

    def __init__(self, folder):
        folder = Path(folder)
        self.jobs, self.workers, self.leases, self.results, self.failed, self.checkpoints = {}, {}, {}, {}, {}, {}
        self.drops = []   # every worker with a drop folder
        self.rejected = {}   # {job: [(worker, when that run finished)]}
        self.retried = {}    # {job: [(worker, when it failed)]}: failures `queue retry` set aside
        # Whether there is a queue here at all (an empty folder where one should be, its drive not
        # mounted, has no jobs/), and every job folder in it, with a job.json that can be read or not
        # (cut short in an upload, say): a job is gone only when its folder is.
        self.there, self.present = (folder / 'jobs').is_dir(), set()
        for p in sorted((folder / 'jobs').glob('*')):
            if not (JOB_ID.match(p.name) and p.is_dir()):
                continue
            self.present.add(p.name)
            data = _read_json(p / 'job.json')
            if _a_job(data):
                self.jobs[p.name] = (data['job'], data['about'])
                turned_down = _dicts(_read_json(p / 'rejected.json'))
                self.rejected[p.name] = [(r.get('worker'), r.get('finished_utc')) for r in turned_down]
                set_aside = _dicts(_read_json(p / 'retry.json'))
                self.retried[p.name] = [(r.get('worker'), r.get('at')) for r in set_aside]
        self.mine = {}       # {worker: {kind: [job]}}: what each drop folder holds
        for w in sorted((folder / 'drop').glob('*')):
            if not NAME.match(w.name):
                continue
            self.drops.append(w.name)
            info = _dict(_read_json(w / 'worker.json'))
            if info:
                self.workers[w.name] = _numbers(info, seen=0, memory_gb=0)
            for p in (w / 'leases').glob('*.json'):
                lease = _dict(_read_json(p))
                if lease:
                    self.leases.setdefault(p.stem, []).append(dict(_numbers(lease, until=0, claimed=0), worker=w.name))
            held = self.mine.setdefault(w.name, {'results': [], 'checkpoints': [], 'failed': [], 'rejected': [],
                                                 'retried': []})
            for p in (w / 'results').glob('*/out/*.json'):
                jid = p.parent.parent.name
                held['results'].append(jid)
                recipe = _dict(_read_json(p))
                if (w.name, _dict(recipe.get('job')).get('finished_utc')) in self.rejected.get(jid, []):
                    held['rejected'].append(jid)
                else:
                    self.results.setdefault(jid, []).append(w.name)
            for p in (w / 'failed').glob('*/why.json'):
                jid, why = p.parent.name, _numbers(_dict(_read_json(p)), at=0, tries=1)
                held['failed'].append(jid)
                if (w.name, why.get('at')) in self.retried.get(jid, []):
                    held['retried'].append(jid)
                else:
                    self.failed.setdefault(jid, []).append(dict(why, worker=w.name))
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
    said = []
    ok = _rsync(ssh_command, '--delete', '--ignore-times', '--include=/jobs/', '--include=/jobs/*/',
                '--include=/jobs/*/job.json',
                '--include=/jobs/*/rejected.json', '--include=/jobs/*/retry.json',
                '--include=/drop/', '--include=/drop/*/', '--include=/drop/*/worker.json',
                '--include=/drop/*/leases/', '--include=/drop/*/leases/*.json',
                '--include=/drop/*/failed/', '--include=/drop/*/failed/*/', '--include=/drop/*/failed/*/why.json',
                '--include=/drop/*/checkpoints/', '--include=/drop/*/checkpoints/*/',
                '--include=/drop/*/results/', '--include=/drop/*/results/*/', '--include=/drop/*/results/*/out/',
                '--include=/drop/*/results/*/out/*.json', '--exclude=*', f'{spec}/', f'{folder}/',
                quiet=True, pull=True, said=said)
    if not ok:
        raise QueueError(f"couldn't read the queue at {spec}" + (f': {said[-1]}' if said else ''))
    return View(folder)


# ---------------------------------------------------------------- submitting and collecting

def _cache():
    return Path(os.environ.get('OPENFILMKIT_CACHE') or Path.home() / '.cache' / 'openfilmkit')


def outbox(q):
    """Where packages for queue `q` are made and kept until collected."""
    return _cache() / 'queues' / q['name'] / 'outbox'


def elsewhere(q):
    """Where the descriptions of jobs packed on other machines are fetched to, for `collect --to`.
    Never the outbox: a job with a package there counts as packed here, and its results go
    where its own job.json says."""
    return _cache() / 'queues' / q['name'] / 'elsewhere'


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
    command = ['df', '-Pk', '--', path]
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
    if not _remote(q, 'mkdir', '-p', '--', _path(q, 'jobs'), _path(q, 'drop')):
        raise QueueError(f"couldn't reach the queue at {q['queue']}")
    need = sum(f.stat().st_size for p in packages for f in Path(p).rglob('*') if f.is_file())
    free = _free_bytes(q)
    if free is not None and need > free - SPARE:
        raise QueueError(f"these jobs need {need / 1e9:.1f} GB and the queue's machine has {free / 1e9:.1f} GB free; "
                         f'queue fewer at a time (collected jobs free their space)')
    # Each job.json goes last, in a pass of its own: it's what offers the job to workers, and rsync
    # would send it before the photo, so a worker looking meanwhile would fetch a job without one.
    sources, dest = [str(Path(p)) for p in packages], f"{q['queue'].rstrip('/')}/jobs/"
    if not (_rsync(q['ssh_command'], '--exclude=/*/job.json', *sources, dest)
            and _rsync(q['ssh_command'], '--include=/*/', '--include=/*/job.json', '--exclude=*', *sources, dest)):
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
            failure = view.failed[jid][0]
            row['state'], row['worker'], row['error'] = 'failed', failure['worker'], failure.get('error')
            row['tries'], row['final'] = failure.get('tries', 1), failure.get('final', True)
        elif jid in view.checkpoints:
            row['state'], row['worker'] = 'stopped', view.checkpoints[jid][0]
        else:
            row['state'] = 'waiting'
        rows.append(row)
    for jid in sorted(view.present - set(view.jobs)):
        rows.append({'id': jid, 'photo': None, 'output': None, 'memory_gb': None, 'version': None, 'state': 'damaged',
                     'error': "its job.json is missing or can't be read (an upload cut short?): queue the photo "
                              'again, or take this out with `queue remove`'})
    workers = [dict(info, name=name, seen_ago_s=round(now - info.get('seen', 0))) for name, info in view.workers.items()]
    for w in workers:   # a worker at work reports at least every RENEW seconds, an idle one every `poll`
        w['silent'] = w['seen_ago_s'] > LEASE
    for row in rows:
        if row['state'] == 'waiting':
            row['why_waiting'] = _why_waiting(view.jobs[row['id']][0], row['memory_gb'], workers)
    return {'queue': q['name'], 'jobs': rows, 'workers': workers}


def ago(seconds):
    """A length of time in a few words: "40 s", "12 minutes", "37 hours", "5 days 3 hours"."""
    seconds = int(seconds)
    if seconds < 120:
        return f'{seconds} s'
    if seconds < 7200:
        return f'{seconds // 60} minutes'
    days, hours = divmod(seconds // 3600, 24)
    return f'{days * 24 + hours} hours' if days < 2 else f'{days} days' + (f' {hours} hours' if hours else '')


def _why_waiting(job, need, workers):
    """Why no worker can take a waiting job as things stand, or None when one can and it only
    waits its turn. Working hours aren't counted: they're in each worker's own time, and they pass."""
    if not workers:
        return 'no worker has reported to this queue'
    whys = []
    for w in workers:
        if w['silent']:
            whys.append(f"{w['name']} has not reported for {ago(w['seen_ago_s'])}")
        elif (w.get('version'), w.get('build')) != (job.get('openfilmkit'), job.get('build')):
            whys.append(f"{w['name']} runs another build of OpenFilmKit ({w.get('version')}) than packed it")
        elif need > w.get('memory_gb', 0):
            whys.append(f"{w['name']} takes jobs up to {w.get('memory_gb')} GB and this one needs {need}")
        else:
            return None
    return '; '.join(whys)


def _reject(q, view, jid, worker, package):
    """Mark a worker's result for a job as turned down, where collecting may not delete it (the
    worker clears it, and the job runs again)."""
    recipe = next((package / 'out').glob('*.json'), None)
    run = (_read_json(recipe) or {}).get('job', {}).get('finished_utc') if recipe else None   # that run alone
    turned_down = [{'worker': w, 'finished_utc': f} for w, f in view.rejected.get(jid, [])]
    turned_down.append({'worker': worker, 'finished_utc': run})
    _note(q, jid, 'rejected.json', turned_down)


def _note(q, jid, name, data):
    """Put a small file for the workers to read into a job's folder in the queue."""
    note = _cache() / 'queues' / q['name'] / 'notes' / jid / name
    _write_json(note, data)
    return _rsync(q['ssh_command'], str(note), f"{q['queue'].rstrip('/')}/jobs/{jid}/", quiet=True)


def _clear(q, view, jid):
    """Take a job out of the queue: its package, the workers' files for it where they can be
    deleted from here (otherwise each worker clears its own), and the package kept here."""
    if not _remote(q, 'rm', '-rf', '--', _path(q, 'jobs', jid)):
        # Still queued, then: its result stays with it (or a worker would only do it again), and so
        # does what's kept here, which is what says the job is ours.
        return False
    paths = [_path(q, 'drop', w, sub, jid) for w in view.drops for sub in ('results', 'checkpoints', 'failed')]
    if paths:
        _remote(q, 'rm', '-rf', '--', *paths, quiet=True)
    for kept in (outbox(q), elsewhere(q), _cache() / 'queues' / q['name'] / 'notes'):
        shutil.rmtree(kept / jid, ignore_errors=True)
    return True


def _named(view, wanted):
    """The jobs in the queue that `wanted` names, each by its ID or the start of it."""
    out = []
    for start in wanted:
        match = [jid for jid in sorted(view.present) if start and jid.startswith(start.lower())]
        if len(match) != 1:
            raise QueueError(f'several jobs start with {start!r}: give more of the ID' if match
                             else f'no job in the queue starts with {start!r}')
        out.append(match[0])
    return list(dict.fromkeys(out))


def retry(q, wanted=None, say=print):
    """Have the workers try failed jobs again: those named (by ID, or the start of one), or all of
    them. Each failure is set aside by a note in the job's folder, which counts from then on; the
    worker it belongs to clears it away on its next look and starts its tries over. Returns the
    jobs' IDs."""
    view = pull_view(q['queue'], q['ssh_command'], _cache() / 'queues' / q['name'] / 'view')
    chosen = _named(view, wanted) if wanted else sorted(jid for jid in view.failed if jid in view.jobs)
    for jid in chosen:
        if jid not in view.failed:
            raise QueueError(f'job {jid[:12]} has not failed')
    for jid in chosen:
        set_aside = [{'worker': w, 'at': at} for w, at in view.retried.get(jid, [])]
        set_aside += [{'worker': f['worker'], 'at': f.get('at')} for f in view.failed[jid]]
        if not _note(q, jid, 'retry.json', set_aside):
            raise QueueError(f"couldn't write to {q['queue']}")
        say(f"{jid[:12]}: free to be tried again (it failed on {', '.join(f['worker'] for f in view.failed[jid])})")
    return chosen


def remove(q, wanted, say=print):
    """Take the named jobs (by ID, or the start of one) out of the queue, whatever their state. A
    worker in the middle of one finishes it and throws the result away. Returns the jobs' IDs."""
    view = pull_view(q['queue'], q['ssh_command'], _cache() / 'queues' / q['name'] / 'view')
    chosen = _named(view, wanted)
    for jid in chosen:
        if not _clear(q, view, jid):
            raise QueueError(f"couldn't remove job {jid[:12]} from {q['queue']}")
        say(f"{jid[:12]}: removed ({Path(view.jobs[jid][1].get('source') or '?').name if jid in view.jobs else '?'})")
    return chosen


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
            package = elsewhere(q) / jid
            (package / 'out').mkdir(parents=True, exist_ok=True)
            _rsync(q['ssh_command'], f"{q['queue'].rstrip('/')}/jobs/{jid}/job.json", f'{package}/',
                   quiet=True, pull=True)
        try:
            jobs.read(package)   # the results are judged by it: without it (not fetched, say), leave them be
        except jobs.JobError as e:
            out.append({'id': jid, 'error': str(e)})
            continue
        record, unfetched = None, []
        for worker in view.results[jid]:
            shutil.rmtree(package / 'out', ignore_errors=True)
            (package / 'out').mkdir(parents=True)
            src = f"{q['queue'].rstrip('/')}/drop/{worker}/results/{jid}/out/"
            if not _rsync(q['ssh_command'], src, f'{package}/out/', quiet=True, pull=True):
                unfetched.append(worker)   # a dropped connection says nothing about the result: it is left alone
                continue
            candidate = jobs.done(package)
            if candidate and _dict(candidate.get('job')).get('id') == jid \
                    and candidate.get('source_sha256') == view.jobs[jid][0]['input'].get('sha256'):
                record = candidate
                break
            say(f'{jid[:12]}: the result from {worker} does not check out; turning it down')
            if not _remote(q, 'rm', '-rf', '--', _path(q, 'drop', worker, 'results', jid), quiet=True):
                _reject(q, view, jid, worker, package)
        if record is None:
            out.append({'id': jid, 'error': f"couldn't fetch the result from {' or '.join(unfetched)}; collect again"
                        if unfetched else 'no result checked out; the job is free to run again'})
            continue
        try:
            records = jobs.collect(package, to)   # one per destination
        except (jobs.JobError, OSError) as e:    # one job's trouble (a folder that can't be written): on to the next
            out.append({'id': jid, 'error': str(e)})
            continue
        if not _clear(q, view, jid):
            say(f"{jid[:12]}: collected, but couldn't be cleared from {q['queue']}; collecting again will clear it")
        for record in records:
            say(f"{jid[:12]}: {record['output']} (from {record.get('job', {}).get('ran_on')})")
        out += records
    if view.there and box.is_dir():
        # A finished package kept here for a job the queue no longer holds: collected before, when clearing
        # it reported a failure that wasn't one (the answer was lost). It holds a copy of the photo.
        for package in box.glob('*'):
            try:
                if JOB_ID.match(package.name) and package.name not in view.present and jobs.done(package):
                    shutil.rmtree(package, ignore_errors=True)
            except jobs.JobError:
                pass
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
    # A setting of the wrong kind is said here, once, as the worker starts. Left to be met at each look
    # at the queue, it stopped a service every minute that had just reported in as if at work.
    for key in ('queue', 'name', 'ssh_command'):
        if not isinstance(s[key], str):
            raise QueueError(f'the worker setting {key} is text, in quotes: not {s[key]!r}')
    if not NAME.match(s['name']):
        raise QueueError(f'worker names use letters, digits, ".", "_" and "-": {s["name"]!r}')
    s['queue'] = _spec(s['queue'])
    # An empty drop counts as none given: taken as it stood, the worker's files were synced to "/".
    s['drop'] = _spec(s['drop']) if s.get('drop') and isinstance(s['drop'], str) else f"{s['queue'].rstrip('/')}/drop/{s['name']}"
    s['state'] = Path(s.get('state') or _cache() / 'worker')
    for key, least in (('memory_gb', None), ('yield_mb', 0), ('lease', 0), ('renew', 0), ('settle', 0), ('poll', 0),
                       ('check', 0)):
        try:
            s[key] = float(s[key]) if key == 'memory_gb' or isinstance(s[key], str) else s[key] + 0
            wrong = isinstance(s[key], bool) or not math.isfinite(s[key]) or (s[key] <= 0 if least is None else s[key] < least)
        except (TypeError, ValueError):
            wrong = True
        if wrong:
            raise QueueError(f"the worker setting {key} is a number{' above 0' if least is None else ', 0 or more'}: "
                             f'not {s[key]!r}')
    for key in ('yield_to', 'busy_when'):
        if isinstance(s[key], str):
            s[key] = [s[key]]
        if not isinstance(s[key], list) or not all(isinstance(v, str) for v in s[key]):
            raise QueueError(f'the worker setting {key} is a command, or a list of them, in quotes: not {s[key]!r}')
    working_hours(s['hours'])
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


HOUR = re.compile(r'(\d{1,2})\s*(am|pm)?', re.I)


def working_hours(hours):
    """(start, end) of working hours written "START-END" in whole local hours, on the 24-hour clock
    ("22-7") or with am and pm ("10pm-7am"); END before START wraps past midnight, so both of those
    are overnight. None when no hours are set. QueueError for anything else."""
    if hours is None or hours == '':
        return None
    ends = [HOUR.fullmatch(part.strip()) for part in str(hours).split('-')]
    if len(ends) == 2 and all(ends):
        span = []
        for m in ends:
            h, half = int(m[1]), (m[2] or '').lower()
            if half:
                span.append(h % 12 + (12 if half == 'pm' else 0) if 1 <= h <= 12 else None)
            else:
                span.append(h if h <= 24 else None)
        if None not in span and span[0] < 24 and span[0] != span[1]:
            return tuple(span)
    raise QueueError(f'working hours are START-END in whole hours, like "22-7" or "10pm-7am": not {hours!r}')


def in_hours(hours, at=None):
    """True inside working hours (see working_hours); always true when no hours are set."""
    span = working_hours(hours)
    if not span:
        return True
    start, end = span
    h = time.localtime(at).tm_hour
    return start <= h < end if start <= end else (h >= start or h < end)


def keeps_off(failure, now):
    """Whether a worker's own failure of a job keeps it from trying the job now: for good when the
    job can't be done or its tries are used up, and otherwise until the wait is over."""
    if failure.get('final', True):
        return True
    wait = RETRY[min(failure.get('tries', 1), len(RETRY)) - 1]
    return now - failure.get('at', 0) < wait


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
    there raises JobError with the reason it gave (AlwaysFails, if it said so)."""
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
                failure = json.load(report)[0]
                reason, always = failure['error'], failure.get('always_fails')
            except (ValueError, LookupError, TypeError):   # no report (it crashed): its log says why
                reason, always = f'job run ended with status {proc.returncode}', False
            raise (jobs.AlwaysFails if always else jobs.JobError)(reason)


class Worker:
    def __init__(self, settings, run=run_in_subprocess, now=_now, say=print):
        self.s, self.run, self.now, self.say = settings, run, now, say
        self.state = settings['state']
        self.mirror = self.state / 'drop'         # what this worker writes, pushed to its drop folder
        self.current = None
        self._pushed = True

    # -- writing to the drop folder
    def _push(self):
        # Only this code's own half-written files (.NAME.PID.tmp) are held back. Every name with a dot
        # in front was, and a result called .hidden-clean-v1.png was never published, the job redone for good.
        ok = _rsync(self.s['ssh_command'], '--exclude=.*.tmp', '--exclude=/results/', f'{self.mirror}/',
                    f"{self.s['drop']}/", quiet=True)
        # A result always replaces what's there: a redone one can match a turned-down one in size
        # and (to the second) in time, which rsync would otherwise take for the same file.
        (self.mirror / 'results').mkdir(parents=True, exist_ok=True)
        ok &= _rsync(self.s['ssh_command'], '--ignore-times', '--exclude=.*.tmp', f'{self.mirror}/results/',
                     f"{self.s['drop']}/results/", quiet=True)
        ok &= _rsync(self.s['ssh_command'], '--delete', '--exclude=.*.tmp', f'{self.mirror}/leases/',
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
        mine = set()   # failed here, and not to be tried now
        for jid, failures in view.failed.items():
            if any(f['worker'] == self.s['name'] and keeps_off(f, now) for f in failures):
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
        """Clear this worker's drop folder of jobs that are gone from the queue (collected), of
        its results that collecting turned down and of its failures that `queue retry` set aside,
        and first its own copies of them here: the next push would put a copy left in the mirror
        back, and a job's package holds its photo."""
        if not view.there:
            # No queue to be seen (a local one whose drive isn't mounted leaves an empty folder): that is
            # not its jobs having been collected, and a finished result still to be pushed must stay.
            return
        held = view.mine.get(self.s['name'], {})
        gone = {jid for kind in ('results', 'checkpoints', 'failed') for jid in held.get(kind, [])
                if jid not in view.present}
        refused, retried = set(held.get('rejected', [])), set(held.get('retried', []))
        for folder in (self.mirror / 'results', self.mirror / 'checkpoints', self.mirror / 'failed', self.state / 'jobs'):
            for p in folder.glob('*'):
                if JOB_ID.match(p.name) and p.name not in view.present:
                    shutil.rmtree(p, ignore_errors=True)
        for jid in refused:
            shutil.rmtree(self.mirror / 'results' / jid, ignore_errors=True)
        for jid in retried:
            shutil.rmtree(self.mirror / 'failed' / jid, ignore_errors=True)
        for kind, more in (('results', refused), ('checkpoints', set()), ('failed', retried)):
            for jid in sorted(gone | more):
                self._empty(kind, jid)

    def _empty(self, kind, jid):
        """Take drop/<kind>/<jid>/ away: emptied by syncing an empty folder into it (which works with
        every rsync), then removed through its parent. Left there empty, a checkpoints folder still
        counted as held, was emptied again at every look, and made a job queued again show as stopped."""
        empty = self.state / 'empty'
        empty.mkdir(parents=True, exist_ok=True)
        _rsync(self.s['ssh_command'], '-r', '--delete', f'{empty}/', f"{self.s['drop']}/{kind}/{jid}/", quiet=True)
        _rsync(self.s['ssh_command'], '--delete', f'--include=/{jid}/', '--exclude=*', f'{empty}/',
               f"{self.s['drop']}/{kind}/", quiet=True)

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
                   f'{got}/', quiet=True, pull=True)
            if best is None or len(list(got.glob('*.npz'))) > len(list(best.glob('*.npz'))):
                best = got
        if best is not None:
            into = package / 'work' / 'checkpoints'
            into.mkdir(parents=True, exist_ok=True)
            for f in best.glob('*.npz'):
                shutil.copyfile(f, into / f.name)
        shutil.rmtree(self.state / 'seed', ignore_errors=True)

    def do(self, jid, view):
        package = self.state / 'jobs' / jid
        package.parent.mkdir(parents=True, exist_ok=True)
        if not _rsync(self.s['ssh_command'], f"{self.s['queue'].rstrip('/')}/jobs/{jid}", f'{package.parent}/',
                      quiet=True, pull=True):
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
            why = self.mirror / 'failed' / jid / 'why.json'
            tries = (_read_json(why) or {}).get('tries', 0) + 1
            final = isinstance(e, jobs.AlwaysFails) or tries >= TRIES
            self.say(f'{jid[:12]}: failed: {e}' + ('' if final else f' (try {tries} of {TRIES})'))
            if tries > 1:
                self._empty('failed', jid)   # the last try's note, which rsync could take for this one (same size)
            _write_json(why, {'job': jid, 'error': str(e), 'at': self.now(), 'tries': tries, 'final': final})
            self.current = None
            self.release(jid)
            if final:
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
