"""Parse `scanimage --all-options` listings into structured options.

The driver is the authority on what a scanner can do; this reads its answer.
"""
import re

OPTION = re.compile(r'^\s{2,6}(-{1,2}[A-Za-z0-9][\w-]*)(\[=\((.*?)\)\])?(?:\s+(.*))?$')
UNIT = re.compile(r'^(.*?)(dpi|mm|%|us|ms|pel|bit)?$')
RANGE = re.compile(r'^(-?[\d.]+)\.\.(-?[\d.]+)(dpi|mm|%|us|ms|pel)?(?:\s*\(in steps of ([\d.]+)\))?$')


def _number(text):
    return float(text) if '.' in text else int(text)


def _split_current(rest):
    """'7200|3600dpi [7200] [advanced]' -> ('7200|3600dpi', '7200', ['advanced'])."""
    flags = []
    while True:
        m = re.search(r'\s*\[(advanced|hardware|software|emulated|automatic)\]\s*$', rest)
        if not m:
            break
        flags.insert(0, m.group(1))
        rest = rest[:m.start()]
    m = re.search(r'\s*\[([^\[\]]*)\]\s*$', rest)
    if not m:
        return rest.strip(), None, flags
    return rest[:m.start()].strip(), m.group(1), flags


def parse(text):
    """Return {name: option} for every option in the listing, in order."""
    options = {}
    for line in text.splitlines():
        m = OPTION.match(line)
        if not m:
            continue
        name = m.group(1).lstrip('-')
        if m.group(2):                       # --preview[=(yes|no)] [no]
            values, current, flags = _split_current(m.group(4) or '')
            choices = m.group(3).split('|')
            opt = {'kind': 'bool', 'choices': choices}
        else:
            values, current, flags = _split_current(m.group(4) or '')
            opt = {'kind': 'button' if not values and current is None else None}
            r = RANGE.match(values)
            if r:
                opt.update(kind='range', min=_number(r.group(1)), max=_number(r.group(2)),
                           unit=r.group(3), step=_number(r.group(4)) if r.group(4) else None)
            elif values.startswith('<') and values.endswith('>'):
                opt.update(kind='text')
            elif values.endswith(',...'):
                opt.update(kind='array')
            elif values:
                body, unit = UNIT.match(values).groups()
                choices = body.split('|')
                if unit and not all(re.fullmatch(r'-?[\d.]+', c) for c in choices):
                    body, unit = values, None
                    choices = values.split('|')
                if all(re.fullmatch(r'-?[\d.]+', c) for c in choices):
                    choices = [_number(c) for c in choices]
                opt.update(kind='list', choices=choices, unit=unit)
        opt['inactive'] = current == 'inactive'
        opt['current'] = None if current in (None, 'inactive') else current
        opt['flags'] = flags
        options[name] = opt
    return options


def film_sources(options):
    """Source choices that look like a film/transparency path."""
    source = options.get('source')
    if not source or source.get('kind') != 'list':
        return []
    return [c for c in source['choices']
            if re.search(r'transparen|film|negative|slide|\bTPU\b|\bTMA\b', str(c), re.I)]


def resolutions(options):
    r = options.get('resolution')
    if not r:
        return []
    if r['kind'] == 'list':
        return sorted(int(c) for c in r['choices'])
    if r['kind'] == 'range':
        return []  # any value in range; check with allows_resolution
    return []


def allows_resolution(options, dpi):
    r = options.get('resolution')
    if not r:
        return False
    if r['kind'] == 'list':
        return dpi in [int(c) for c in r['choices']]
    if r['kind'] == 'range':
        return r['min'] <= dpi <= r['max']
    return False
