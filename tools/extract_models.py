"""Generate the driver model table from a pinned SANE source tree.

Reads backend/genesys/tables_model.cpp and writes every USB entry with the
driver's internal model name, the vendor/model strings it reports, and the
USB key (vendor, product, optional bcdDevice) it matches on.

The build fails, rather than writing a table, if the reported strings or
USB keys stop mapping one-to-one onto internal names, or if the parser
misses an entry. Identification depends on both.

Usage: python3 -I extract_models.py <sane-source-dir> <out.json> [--commit HASH]
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

GENESYS_TABLE = Path('backend/genesys/tables_model.cpp')

RESET = re.compile(r'\bmodel\s*=\s*Genesys_Model\(\)\s*;')
FIELD = re.compile(r'\bmodel\.(name|vendor|model)\s*=\s*"([^"]*)"\s*;')
ENTRY = re.compile(r'emplace_back\(\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*'
                   r'(?:,\s*(0x[0-9a-fA-F]+)\s*)?,\s*model\s*\)')


class TableError(Exception):
    pass


def parse_genesys(text):
    """Return entries in table order, as the driver searches them."""
    entries = []
    current = {}
    for number, line in enumerate(text.splitlines(), 1):
        if RESET.search(line):
            current = {}
        for field, value in FIELD.findall(line):
            current[field] = value
        m = ENTRY.search(line)
        if m:
            missing = [f for f in ('name', 'vendor', 'model') if not current.get(f)]
            if missing:
                raise TableError(f'line {number}: entry without {", ".join(missing)}')
            entries.append({
                'id': current['name'],
                'vendor': current['vendor'],
                'model': current['model'],
                'usb': {'vendor': f'{int(m.group(1), 16):04x}',
                        'product': f'{int(m.group(2), 16):04x}',
                        'bcd': f'{int(m.group(3), 16):04x}' if m.group(3) else None},
                'line': number,
            })
    found = text.count('emplace_back(')
    if found != len(entries):
        raise TableError(f'parsed {len(entries)} of {found} emplace_back entries; '
                         'the table format changed')
    return entries


def check_genesys(entries):
    """Raise unless strings and USB keys each map to exactly one model."""
    problems = []
    by_strings = {}
    for e in entries:
        by_strings.setdefault((e['vendor'], e['model']), set()).add(e['id'])
    for (vendor, model), ids in sorted(by_strings.items()):
        if len(ids) > 1:
            problems.append(f'"{vendor} {model}" is reported by {sorted(ids)}')

    by_usb = {}
    for e in entries:
        u = e['usb']
        by_usb.setdefault((u['vendor'], u['product']), []).append(e)
    for (vendor, product), group in sorted(by_usb.items()):
        if len(group) == 1:
            continue
        # Several entries can share an ID only if each names its own bcdDevice.
        bcds = [e['usb']['bcd'] for e in group]
        if None in bcds or len(set(bcds)) != len(bcds):
            problems.append(f'{vendor}:{product} matches {[e["id"] for e in group]} '
                            'without distinct bcdDevice values')
    if problems:
        raise TableError('identification would be ambiguous:\n  ' + '\n  '.join(problems))


def build(source, commit=None):
    path = Path(source) / GENESYS_TABLE
    raw = path.read_bytes()
    entries = parse_genesys(raw.decode('utf-8'))
    check_genesys(entries)
    return {
        'format': 1,
        'sane_commit': commit,
        'sources': {str(GENESYS_TABLE): hashlib.sha256(raw).hexdigest()},
        'backends': {'genesys': entries},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('out')
    parser.add_argument('--commit')
    args = parser.parse_args()
    try:
        table = build(args.source, args.commit)
    except TableError as e:
        sys.exit(f'extract_models: {e}')
    Path(args.out).write_text(json.dumps(table, indent=1) + '\n')
    print(f"{len(table['backends']['genesys'])} genesys models")


if __name__ == '__main__':
    main()
