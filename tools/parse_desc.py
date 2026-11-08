"""Read SANE .desc files (doc/descriptions*) into scanner records.

Usage: python3 -I parse_desc.py <sane-source-dir> <out.json> [commit]
"""
import json
import re
import sys
from pathlib import Path

TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|(:[A-Za-z0-9_-]+)|(;.*)')

FILM_NAME = re.compile(
    r'opticfilm|coolscan|\bls-\d|dimage scan|scan elite|scan dual|scan multi|'
    r'primefilm|reflecta|crystalscan|proscan|film ?scan|slide|filmscanner|'
    r'\bpf ?\d{4}|\bcs-?\d|polaroid sprintscan|sprintscan|quickscan 35|'
    r'nikon ls|filmcopy|35 ?mm', re.I)
FILM_COMMENT = re.compile(
    r'film|slide|negative|transparen|\btpu\b|\btma\b|\bta\b|35 ?mm', re.I)


def tokens(text):
    for line in text.splitlines():
        for m in TOKEN.finditer(line):
            if m.group(3):
                break
            if m.group(2):
                yield ('key', m.group(2))
            else:
                yield ('str', m.group(1).replace('\\"', '"'))


def parse(path, external):
    backend = {'file': path.name, 'external': external}
    devices = []
    mfg = None
    dev = None
    devicetype = None
    key = None
    for kind, val in tokens(path.read_text(errors='replace')):
        if kind == 'key' and key in ('status', 'devicetype'):
            # Symbol value, e.g. ":status :good".
            if key == 'status' and dev is not None:
                dev['status'] = val[1:]
            elif key == 'devicetype':
                devicetype = val[1:]
            key = None
            continue
        if kind == 'key':
            key = val[1:]
            if key == 'mfg':
                dev = None
            continue
        # String value for the current key.
        if key in ('backend', 'version', 'manpage', 'new'):
            backend[key] = val
        elif key == 'mfg':
            mfg = val
        elif key == 'model':
            dev = {'backend': backend.get('backend', path.stem),
                   'external': external, 'devicetype': devicetype,
                   'mfg': mfg, 'model': val, 'status': None,
                   'interface': None, 'usbid': [], 'comment': [], 'url': []}
            devices.append(dev)
        elif dev is not None and key == 'usbid':
            dev['usbid'].append(val.lower())
        elif dev is not None and key == 'interface':
            dev['interface'] = val
        elif dev is not None and key in ('comment', 'url'):
            dev[key].append(val)
        # Keep the key: :usbid takes two strings in a row.
    for d in devices:
        d['comment'] = ' '.join(d['comment'])
        u = d.pop('usbid')
        d['usb'] = f'{u[0][2:]}:{u[1][2:]}' if len(u) == 2 else None
        text = f"{d['mfg']} {d['model']}"
        if FILM_NAME.search(text):
            d['film'] = 'dedicated'
        elif FILM_COMMENT.search(d['comment']):
            d['film'] = 'mentioned'
        else:
            d['film'] = None
    return devices


def collect(src):
    devices = []
    for sub, external in (('descriptions', False), ('descriptions-external', True)):
        for p in sorted((Path(src) / 'doc' / sub).glob('*.desc')):
            devices.extend(parse(p, external))
    return devices


def write(src, out, commit=None):
    devices = collect(src)
    Path(out).write_text(json.dumps({'format': 1, 'sane_commit': commit, 'devices': devices},
                                    separators=(',', ':')) + '\n')
    return devices


def main():
    devices = write(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print(len(devices), 'devices')


if __name__ == '__main__':
    main()
