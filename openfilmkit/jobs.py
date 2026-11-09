"""Job packages: self-contained, repeatable units of work, the way OpenFilmKit runs on another machine.

A package is a folder named after its job ID:

  job.json   {'job': what to do, 'about': where it came from}
  input/     the file to work on (and its recipe, if it has one)
  out/       the result; its recipe is written last and marks the job done
  work/      temporary files, safe to delete; a crashed run leaves its junk here

The job ID is the SHA-256 of job.json's 'job' part: the command, its options, the input's
checksum, the OpenFilmKit version and the models' checksums. That is everything that decides the
output and nothing else, so packing the same work twice gives the same package, from any folder
on any machine, and running a finished job again does nothing. The 'about' part (the original
path, the suggested output name, when and where it was packed) is kept but not hashed.

A job runs only on the OpenFilmKit version that packed it, since another version can give other
pixels. Run results are written under temporary names and renamed into out/, the recipe last, so
a run that stops partway leaves the job unfinished rather than half done, and two machines that
run the same job end with the same file.
"""
import datetime
import hashlib
import inspect
import json
import os
import shutil
import socket
from pathlib import Path

from . import __version__

FORMAT = 1
CLEAN_OPTIONS = ('crop', 'dust', 'protect_faces', 'color', 'straighten', 'lift_shadows', 'red_eye',
                 'scratches', 'spare_long_lines', 'spots')


class JobError(Exception):
    pass


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def job_id(job):
    return hashlib.sha256(json.dumps(job, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write_json(path, data):
    """Write JSON under a temporary name, then rename: readers see the old file or the new one."""
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(tmp, path)


def _copy(src, dest):
    """Hard link if the file system allows it (inputs are only read), else copy."""
    try:
        os.link(src, dest)
    except OSError:
        shutil.copyfile(src, dest)


def pack_clean(source, into, output=None, **options):
    """A package in folder `into` for `clean` of `source` with clean()'s keyword `options`.
    `output` names the result (default: the name clean would give it beside the source); a
    .tif, .jpg or .png suffix there chooses the format. Returns the package's path; packing
    work that is already packed returns the existing package."""
    from . import clean
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise JobError(f'{source} is not a file')
    unknown = set(options) - set(CLEAN_OPTIONS)
    if unknown:
        raise JobError(f'unknown clean options: {", ".join(sorted(unknown))}')
    name = Path(output).name if output else clean.default_output(source).name
    suffix = Path(name).suffix.lower()
    if suffix not in clean.OUTPUTS:
        raise JobError('choose a .jpg, .png or .tif output')
    defaults = inspect.signature(clean.clean).parameters   # unset options: clean's own defaults
    opts = {k: options.get(k, defaults[k].default) for k in CLEAN_OPTIONS}
    opts['spots'] = [list(s) for s in opts['spots'] or []]
    job = {'format': FORMAT, 'command': 'clean', 'options': opts, 'output_suffix': suffix,
           'input': {'sha256': sha(source)}, 'openfilmkit': __version__, 'models': {}}
    if opts['scratches']:
        from . import models
        job['models'] = {k: m['sha256'] for k, m in models.MODELS.items()}
    about = {'source': str(source), 'output_name': name, 'input_name': source.name,
             'packed_utc': _now(), 'packed_on': socket.gethostname()}
    return _write_package(Path(into).expanduser(), job, about, [source, Path(str(source) + '.json')])


def _write_package(into, job, about, files):
    jid = job_id(job)
    dest = into / jid
    if (dest / 'job.json').exists():
        if read(dest)[0] != job:
            raise JobError(f'{dest} holds a different job under the same ID')
        return dest
    into.mkdir(parents=True, exist_ok=True)
    tmp = into / f'.{jid}.{os.getpid()}.packing'
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / 'input').mkdir(parents=True)
    for f in files:
        if f.is_file():
            _copy(f, tmp / 'input' / f.name)
    if sha(tmp / 'input' / about['input_name']) != job['input']['sha256']:
        shutil.rmtree(tmp)
        raise JobError('the input changed while it was being packed')
    _write_json(tmp / 'job.json', {'job': job, 'about': about})
    try:
        os.rename(tmp, dest)
    except OSError:              # packed meanwhile by someone else: theirs is the same job
        shutil.rmtree(tmp)
        if not (dest / 'job.json').exists():
            raise
    return dest


def read(package):
    """(job, about) from a package, after checking the folder's name is the job's ID."""
    package = Path(package)
    try:
        data = json.loads((package / 'job.json').read_text())
        job, about = data['job'], data['about']
    except (OSError, ValueError, KeyError) as e:
        raise JobError(f'{package} is not a job package: {e}') from e
    if job.get('format') != FORMAT:
        raise JobError(f'{package}: package format {job.get("format")}; this OpenFilmKit reads {FORMAT}')
    if package.name != job_id(job):
        raise JobError(f'{package}: job.json does not match the package ID (it was changed)')
    return job, about


def _result(package, about):
    return Path(package) / 'out' / about['output_name']


def done(package):
    """The finished job's recipe, or None: the recipe is written last, and the file it describes
    must still match it."""
    job, about = read(package)
    out = _result(package, about)
    recipe = Path(str(out) + '.json')
    if not recipe.is_file() or not out.is_file():
        return None
    record = json.loads(recipe.read_text())
    return record if record.get('output_sha256') == sha(out) else None


def run(package, say=None):
    """Run a package's job (a finished one is left as it is) and return its recipe. `say`
    reports model downloads."""
    package = Path(package).expanduser().resolve()
    job, about = read(package)
    finished = done(package)
    if finished:
        return finished
    if job['openfilmkit'] != __version__:
        raise JobError(f'this job was packed for OpenFilmKit {job["openfilmkit"]}; this is {__version__}')
    source = package / 'input' / about['input_name']
    if not source.is_file() or sha(source) != job['input']['sha256']:
        raise JobError(f'{package}: the input is missing or does not match its checksum')
    if job['command'] != 'clean':
        raise JobError(f'unknown command {job["command"]!r}')
    from . import clean, models
    if job['models'] and job['models'] != {k: m['sha256'] for k, m in models.MODELS.items()}:
        raise JobError('this job needs other models than this OpenFilmKit uses')
    tmp = package / 'work' / f'run-{socket.gethostname()}-{os.getpid()}'
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        produced = tmp / about['output_name']
        record = clean.clean(source, produced, say=say, **job['options'])
        final = _result(package, about)
        record.update(source=about['source'], output=str(final))
        record['job'] = {'id': package.name, 'ran_on': socket.gethostname(), 'finished_utc': _now()}
        (package / 'out').mkdir(exist_ok=True)
        _write_json(Path(str(produced) + '.json'), record)
        os.replace(produced, final)
        os.replace(Path(str(produced) + '.json'), Path(str(final) + '.json'))   # last: the job is done
    except clean.CleanError as e:
        raise JobError(str(e)) from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return record


def collect(package, to=None):
    """Copy a finished job's result and recipe out of the package: into folder `to`, or beside
    the original source. Collecting again is harmless; a different file already at the
    destination is never replaced. Returns the recipe as written there."""
    package = Path(package).expanduser().resolve()
    job, about = read(package)
    record = done(package)
    if not record:
        raise JobError(f'{package}: the job has not finished')
    folder = Path(to).expanduser().resolve() if to else Path(about['source']).parent
    dest = folder / about['output_name']
    recipe = Path(str(dest) + '.json')
    if dest.exists() and sha(dest) != record['output_sha256']:
        raise JobError(f'refusing to replace {dest}, a different file')
    folder.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        tmp = dest.with_name(f'.{dest.name}.{os.getpid()}.tmp')
        shutil.copyfile(_result(package, about), tmp)
        os.replace(tmp, dest)
    record = dict(record, output=str(dest))
    _write_json(recipe, record)
    return record


def show(package):
    """A summary of a package: its ID, state and what it does."""
    package = Path(package).expanduser().resolve()
    job, about = read(package)
    work = package / 'work'
    running = sorted(p.name for p in work.glob('run-*')) if work.is_dir() else []
    state = 'done' if done(package) else ('started' if running else 'packed')
    return {'id': package.name, 'state': state, 'runs_in_work': running, 'job': job, 'about': about}
