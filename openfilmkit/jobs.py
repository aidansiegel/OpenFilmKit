"""Job packages: self-contained, repeatable units of work, the way OpenFilmKit runs on another machine.

A package is a folder named after its job ID:

  job.json   {'job': what to do, 'about': where it came from}
  input/     the file to work on (and its recipe, if it has one)
  out/       the result; its recipe is written last and marks the job done
  work/      temporary files, and checkpoints/: what the slow stages of an unfinished run had
             worked out, so the next run, here or on another machine, goes on from there

The job ID is the SHA-256 of job.json's 'job' part: the command, its options, the input's
checksum, the OpenFilmKit version and build (a fingerprint of its code) and the models'
checksums. That is everything that decides the output and nothing else, so packing the same work
twice gives the same package, from any folder on any machine, and running a finished job again
does nothing. The 'about' part is kept but not hashed: where the result goes (one destination for
each photo packed into the job: two identical scans in different folders share one job and both
get the result), and when and where it was packed.

A job runs only on the OpenFilmKit build that packed it, since other code can give other pixels. Run results are written under temporary names and renamed into out/, the recipe last, so
a run that stops partway leaves the job unfinished rather than half done, and two machines that
run the same job end with the same file. A resumed run gives the same file as one that never
stopped; the recipe says which stages were resumed, and from which machine. The checkpoints are
deleted once the job is done.
"""
import datetime
import functools
import hashlib
import inspect
import json
import os
import re
import shutil
import socket
from pathlib import Path

from . import __version__

FORMAT = 1
CLEAN_OPTIONS = ('crop', 'dust', 'protect_faces', 'color', 'straighten', 'lift_shadows', 'red_eye',
                 'scratches', 'spare_long_lines', 'spots')


class JobError(Exception):
    pass


class AlwaysFails(JobError):
    """A job that can't be done however often it's tried (its photo can't be read, say)."""


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


IMAGE_LIBRARIES = ('numpy', 'pillow', 'opencv-python-headless', 'opencv-python', 'tifffile', 'imagecodecs')


@functools.lru_cache(maxsize=1)
def build():
    """A fingerprint of this OpenFilmKit's code (its Python files) and of the versions of the
    libraries that read, compute and write its pixels: two installs with the same one give the
    same files, even between releases, when the version number alone can't tell (a newer Pillow,
    say, can write a JPEG differently). The terminal UI's code is left out: it never touches the
    pixels, so changing it shouldn't turn workers away from jobs."""
    from importlib.metadata import PackageNotFoundError, version
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(root.rglob('*.py')):
        if p.relative_to(root).parts[0] == 'ui':
            continue
        h.update(p.relative_to(root).as_posix().encode() + b'\0' + p.read_bytes())
    for name in IMAGE_LIBRARIES:
        try:
            h.update(f'{name}=={version(name)}\0'.encode())
        except PackageNotFoundError:
            pass
    return h.hexdigest()[:16]


def plain_name(name):
    """True for a bare file name: nothing that could reach outside the folder it's used in."""
    return (isinstance(name, str) and name not in ('', '.', '..') and '/' not in name and '\\' not in name
            and '\0' not in name and Path(name).name == name)


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
    source = Path(source).expanduser().resolve()
    job, about = clean_job(source, output, **options)
    return _write_package(Path(into).expanduser(), job, about, [source, Path(str(source) + '.json')])


def clean_job(source, output=None, **options):
    """(job, about) for `clean` of `source`; see pack_clean."""
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
           'input': {'sha256': sha(source)}, 'openfilmkit': __version__, 'build': build(), 'models': {}}
    if opts['scratches']:
        from . import models
        job['models'] = {k: m['sha256'] for k, m in models.MODELS.items()}
    about = {'source': str(source), 'output_name': name, 'input_name': source.name,
             'destinations': [{'source': str(source), 'output_name': name}],
             'packed_utc': _now(), 'packed_on': socket.gethostname(), 'megapixels': megapixels(source)}
    return job, about


def megapixels(path):
    """The image's size in megapixels, read from its header (for estimating a job's memory)."""
    try:
        if Path(path).suffix.lower() in ('.tif', '.tiff'):
            import tifffile
            with tifffile.TiffFile(path) as tif:
                h, w = tif.pages[0].shape[:2]
        else:
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
        return round(w * h / 1e6, 2)
    except Exception:   # unreadable here: clean will say why when it runs
        return None


def memory_needed(job, about):
    """Estimated peak memory of a job in GB, from its size and options: measured at 73 MP, about
    2.4 GB with --scratches (most of it the scratch model) and 1.4 GB without."""
    mp = about.get('megapixels') or 25
    if job.get('options', {}).get('scratches'):
        return round(1.6 + 0.012 * mp, 2)
    return round(0.3 + 0.015 * mp, 2)


CHECKPOINT_DAYS = 14


def local_checkpoints(job):
    """Where a clean run here keeps its stage checkpoints: a cache folder named after the job's
    ID, so running the same command on the same photo again, after Ctrl-C or a machine that went
    to sleep, picks up the slow work, and nothing else can. Folders left more than
    CHECKPOINT_DAYS untouched are cleared away."""
    base = Path(os.environ.get('OPENFILMKIT_CACHE') or Path.home() / '.cache' / 'openfilmkit') / 'checkpoints'
    cutoff = datetime.datetime.now().timestamp() - CHECKPOINT_DAYS * 86400
    if base.is_dir():
        for old in base.iterdir():
            try:
                if old.is_dir() and old.stat().st_mtime < cutoff:
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass
    return base / job_id(job)


def _write_package(into, job, about, files):
    jid = job_id(job)
    dest = into / jid
    if (dest / 'job.json').exists():
        have, known = read(dest)
        if have != job:
            raise JobError(f'{dest} holds a different job under the same ID')
        destinations = known.setdefault('destinations', [])
        for d in about['destinations']:     # the same photo again, from somewhere else: send it there too
            if d not in destinations:
                destinations.append(d)
                _write_json(dest / 'job.json', {'job': have, 'about': known})
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
    names = [about.get('input_name'), about.get('output_name')]
    names += [d.get('output_name') for d in about.get('destinations', [])]
    if not all(plain_name(n) for n in names):
        raise JobError(f'{package}: job.json names a file outside the package')
    if any(Path(n).suffix.lower() != job.get('output_suffix') for n in names[1:]):
        raise JobError(f'{package}: job.json asks for an output of another kind than the job makes')
    return job, about


def _result(package, about):
    return Path(package) / 'out' / about['output_name']


def done(package):
    """The finished job's recipe, or None: the recipe is written last, it must be whole (one cut
    short in transit doesn't count), and the file it describes must still match it."""
    job, about = read(package)
    out = _result(package, about)
    recipe = Path(str(out) + '.json')
    if not recipe.is_file() or not out.is_file():
        return None
    try:
        record = json.loads(recipe.read_text())
    except (OSError, ValueError):
        return None
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
    if job.get('build') != build():
        raise JobError(f'this job was packed by other OpenFilmKit {__version__} code (build {job.get("build")}; '
                       f'this is {build()}): install the same code here')
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
        record = clean.clean(source, produced, say=say, checkpoints=package / 'work' / 'checkpoints',
                             **job['options'])
        final = _result(package, about)
        record.update(source=about['source'], output=str(final))
        record['job'] = {'id': package.name, 'ran_on': socket.gethostname(), 'finished_utc': _now()}
        (package / 'out').mkdir(exist_ok=True)
        _write_json(Path(str(produced) + '.json'), record)
        os.replace(produced, final)
        os.replace(Path(str(produced) + '.json'), Path(str(final) + '.json'))   # last: the job is done
        shutil.rmtree(package / 'work' / 'checkpoints', ignore_errors=True)
    except clean.CleanError as e:
        # The photo or the options, so the same again next time; except a model that couldn't be
        # fetched, which another try may get.
        fetching = isinstance(e.__cause__, models.ModelError)
        raise (JobError if fetching else AlwaysFails)(str(e)) from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return record


NUMBERED = re.compile(r'(.*-clean-v)(\d+)')   # the names clean gives its copies (clean.default_output)


def _free(dest, digest):
    """`dest`, or where a different file has that name and it is one of clean's numbered names,
    the first number from there on that is free or holds this result already: the number was
    picked when the job was packed, and another copy can have taken it since."""
    while dest.exists() and sha(dest) != digest:
        numbered = NUMBERED.fullmatch(dest.stem)
        if not numbered:
            raise JobError(f'refusing to replace {dest}, a different file')
        dest = dest.with_name(f'{numbered[1]}{int(numbered[2]) + 1}{dest.suffix}')
    return dest


def collect(package, to=None):
    """Copy a finished job's result and recipe out of the package to each of its destinations
    (beside each photo packed into it), or all into folder `to`. Collecting again is harmless; a
    different file already at a destination is never replaced (a copy with one of clean's numbered
    names takes the next number instead). Returns the recipes as written, one per destination."""
    package = Path(package).expanduser().resolve()
    job, about = read(package)
    record = done(package)
    if not record:
        raise JobError(f'{package}: the job has not finished')
    destinations = about.get('destinations') or [{'source': about['source'], 'output_name': about['output_name']}]
    planned = []
    for d in destinations:
        folder = Path(to).expanduser().resolve() if to else Path(d['source']).parent
        planned.append((d, folder, _free(folder / d['output_name'], record['output_sha256'])))
    written = []
    for d, folder, dest in planned:
        folder.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            tmp = dest.with_name(f'.{dest.name}.{os.getpid()}.tmp')
            shutil.copyfile(_result(package, about), tmp)
            os.replace(tmp, dest)
        mine = dict(record, source=d['source'], output=str(dest))
        _write_json(Path(str(dest) + '.json'), mine)
        written.append(mine)
    return written


def remove(package):
    """Delete a package folder, after checking it is one."""
    package = Path(package).expanduser().resolve()
    read(package)
    shutil.rmtree(package)


def show(package):
    """A summary of a package: its ID, state and what it does."""
    package = Path(package).expanduser().resolve()
    job, about = read(package)
    work = package / 'work'
    running = sorted(p.name for p in work.glob('run-*')) if work.is_dir() else []
    saved = sorted(p.stem for p in (work / 'checkpoints').glob('*.npz')) if work.is_dir() else []
    state = 'done' if done(package) else ('started' if running or saved else 'packed')
    return {'id': package.name, 'state': state, 'runs_in_work': running, 'checkpoints': saved,
            'job': job, 'about': about}
