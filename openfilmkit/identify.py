"""Identify connected scanners by the driver's internal model ID.

Each scanner is identified two independent ways, using the driver model
table generated from the pinned SANE source (tools/extract_models.py):

1. from the vendor/model strings the driver reports through scanimage;
2. from the USB descriptors, applying the driver's own matching rules,
   including bcdDevice.

The answer is exact only when both agree. A disagreement stops the scan,
with one known exception: genesys's sane_init probes without bcdDevice, so
the device list names the first table entry for a vendor:product (e.g.
OpticFilm 7600i (v1) for a v2 unit). sane_open() re-reads bcdDevice and
switches to the right model before scanning (genesys.cpp, sane_open_impl),
so for revision-split IDs the USB revision decides and the listed name is
only a label.
USB descriptors are also kept as evidence for sidecars and bug reports;
on Plustek GL128 scanners the serial number string is the firmware date.
"""
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .runtime import run_scanimage

TABLE = Path(__file__).parent / 'data' / 'sane-models.json'
LISTING_FORMAT = '%d\t%v\t%m\t%t%n'

EXACT = 'exact'                  # name and USB agree
NAME_ONLY = 'name-only'          # USB descriptors unreadable on this system
UNCONFIRMED = 'unconfirmed'      # USB readable, but the expected device is absent
MISMATCH = 'mismatch'            # name and USB disagree: do not scan
AMBIGUOUS = 'ambiguous'          # more than one candidate device
UNRECOGNIZED = 'unrecognized'    # reported name not in the pinned table
NO_TABLE = 'no-table'            # backend has no generated table yet

SCANNABLE = {EXACT, NAME_ONLY, NO_TABLE}  # NO_TABLE: generic settings, warned


def load_table(path=TABLE):
    return json.loads(Path(path).read_text())


def parse_listing(text):
    """Parse scanimage --formatted-device-list output in LISTING_FORMAT."""
    rows = []
    for line in text.splitlines():
        parts = line.split('\t')
        if len(parts) != 4:
            continue
        device, vendor, model, kind = (p.strip() for p in parts)
        rows.append({'device': device, 'vendor': vendor, 'model': model, 'type': kind,
                     'backend': device.split(':', 1)[0]})
    return rows


def _hex4(value):
    return f'{value:04x}' if isinstance(value, int) else None


def usb_devices_macos(ioreg_xml=None):
    if ioreg_xml is None:
        ioreg_xml = subprocess.check_output(['/usr/sbin/ioreg', '-p', 'IOUSB', '-l', '-a'],
                                            timeout=15)
    found = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get('idVendor'), int):
                location = node.get('locationID')
                found.append({
                    'vendor': _hex4(node['idVendor']),
                    'product': _hex4(node.get('idProduct')),
                    'bcd': _hex4(node.get('bcdDevice')),
                    'serial': node.get('USB Serial Number', node.get('kUSBSerialNumberString')),
                    'vendor_name': node.get('USB Vendor Name', node.get('kUSBVendorString')),
                    'product_name': node.get('USB Product Name', node.get('kUSBProductString')),
                    'location': f'{location:08x}' if isinstance(location, int) else None,
                })
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(plistlib.loads(ioreg_xml))
    return found


def usb_devices_linux(root=Path('/sys/bus/usb/devices')):
    def read(path):
        try:
            return path.read_text().strip()
        except OSError:
            return None

    found = []
    for dev in sorted(Path(root).iterdir()):
        vendor = read(dev / 'idVendor')
        if not vendor:
            continue
        found.append({'vendor': vendor.lower(), 'product': (read(dev / 'idProduct') or '').lower(),
                      'bcd': (read(dev / 'bcdDevice') or '').lower() or None,
                      'serial': read(dev / 'serial'), 'vendor_name': read(dev / 'manufacturer'),
                      'product_name': read(dev / 'product'), 'location': dev.name})
    return found


def usb_devices():
    """USB descriptors, or None when they can't be read on this system."""
    try:
        if sys.platform == 'darwin':
            return usb_devices_macos()
        if sys.platform.startswith('linux'):
            return usb_devices_linux()
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        pass
    return None


def match_usb(entries, usb):
    """The table entry the driver should pick for a USB device, bcdDevice included."""
    for e in entries:
        k = e['usb']
        if k['vendor'] == usb['vendor'] and k['product'] == usb['product'] and \
                (k['bcd'] is None or usb['bcd'] is None or k['bcd'] == usb['bcd']):
            return e
    return None


def identify(listing, usb, table):
    """Identify each scanner in `listing` (parse_listing rows).

    `usb` is a list of USB descriptor dicts, or None if unavailable.
    Returns (results, unclaimed): one result per listed scanner, and USB
    devices the driver tables know but no listed scanner accounts for.
    """
    results = []
    claimed = set()
    for row in listing:
        result = {**row, 'id': None, 'status': None, 'usb': None, 'problems': []}
        results.append(result)
        entries = table['backends'].get(row['backend'])
        if entries is None:
            result['status'] = NO_TABLE
            continue
        named = [e for e in entries if (e['vendor'], e['model']) == (row['vendor'], row['model'])]
        if not named:
            result['status'] = UNRECOGNIZED
            result['problems'].append(
                f"{row['backend']} reports \"{row['vendor']} {row['model']}\", which is not in "
                f"the table for SANE {table.get('sane_commit') or 'unknown'}; the driver and "
                'the table come from different SANE versions')
            continue
        entry = named[0]
        key = entry['usb']
        revision_split = key['bcd'] is not None
        result['id'] = f"{row['backend']}/{entry['id']}"
        if usb is None:
            if revision_split:
                result['status'] = UNCONFIRMED
                result['problems'].append(
                    f"{key['vendor']}:{key['product']} has several hardware revisions and the "
                    'USB revision cannot be read here, so the listed name may be wrong')
            else:
                result['status'] = NAME_ONLY
                result['problems'].append('USB descriptors unavailable; identified by name only')
            continue
        candidates = [i for i, u in enumerate(usb)
                      if (u['vendor'], u['product']) == (key['vendor'], key['product'])]
        if not candidates:
            result['status'] = UNCONFIRMED
            result['problems'].append(
                f"the driver reports {entry['model']}, but no USB device "
                f"{key['vendor']}:{key['product']} is connected")
            continue
        if len(candidates) > 1:
            result['status'] = AMBIGUOUS
            result['problems'].append(
                f"{len(candidates)} USB devices share {key['vendor']}:{key['product']}; "
                'they cannot be told apart')
            claimed.update(candidates)
            continue
        index = candidates[0]
        claimed.add(index)
        result['usb'] = usb[index]
        by_usb = match_usb(entries, usb[index])
        if by_usb is not None and by_usb['id'] == entry['id']:
            result['status'] = EXACT
        elif by_usb is not None and revision_split and by_usb['usb']['bcd'] is not None:
            # Same vendor:product, other revision: the listing's first-entry
            # label. The driver picks by_usb when it opens the scanner.
            result['id'] = f"{row['backend']}/{by_usb['id']}"
            result['status'] = EXACT
            result['problems'].append(
                f"listed as {entry['model']}, but USB revision {usb[index]['bcd']} is "
                f"{by_usb['model']}; the driver re-checks the revision when it opens the "
                'scanner, so it will use the right settings')
        else:
            result['status'] = MISMATCH
            seen = by_usb['model'] if by_usb else 'no table entry'
            result['problems'].append(
                f"the driver identified this scanner as {entry['model']}, but its USB revision "
                f"{usb[index]['bcd']} matches {seen}; scanning would use the wrong settings")

    # Two listed scanners resolving to one model can't be told apart either.
    ids = [r['id'] for r in results if r['id']]
    for r in results:
        if r['id'] and ids.count(r['id']) > 1 and r['status'] in SCANNABLE:
            r['status'] = AMBIGUOUS
            r['problems'].append(f"{ids.count(r['id'])} connected scanners are {r['id']}")

    unclaimed = []
    for i, u in enumerate(usb or []):
        if i in claimed:
            continue
        for backend, entries in table['backends'].items():
            e = match_usb(entries, u)
            if e:
                unclaimed.append({'usb': u, 'id': f"{backend}/{e['id']}",
                                  'problem': f"{backend} supports {e['vendor']} {e['model']} "
                                             'but did not list it: another program may have '
                                             'it open, the backend may be disabled, or USB '
                                             'permissions may be missing'})
    return results, unclaimed


def list_scanners(scanimage='scanimage', env=None):
    out = run_scanimage([scanimage, f'--formatted-device-list={LISTING_FORMAT}'], env)
    return parse_listing(out.stdout)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Identify connected scanners.')
    parser.add_argument('--scanimage', default=os.environ.get('SCANIMAGE', 'scanimage'))
    parser.add_argument('--table', default=str(TABLE))
    args = parser.parse_args(argv)
    table = load_table(args.table)
    results, unclaimed = identify(list_scanners(args.scanimage), usb_devices(), table)
    json.dump({'sane_commit': table.get('sane_commit'), 'scanners': results,
               'unclaimed': unclaimed}, sys.stdout, indent=1)
    print()
    return 0 if all(r['status'] in SCANNABLE for r in results) else 3


if __name__ == '__main__':
    sys.exit(main())
