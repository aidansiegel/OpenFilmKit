import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

from openfilmkit import identify as ident  # noqa: E402
import extract_models  # noqa: E402

# Same shape and order as the generated table: v1 before v2, as in genesys.
TABLE = {'sane_commit': 'test', 'backends': {'genesys': [
    {'id': 'plustek-opticfilm-7600i-v1', 'vendor': 'PLUSTEK', 'model': 'OpticFilm 7600i (v1)',
     'usb': {'vendor': '07b3', 'product': '0c3b', 'bcd': '0400'}},
    {'id': 'plustek-opticfilm-7600i-v2', 'vendor': 'PLUSTEK', 'model': 'OpticFilm 7600i (v2)',
     'usb': {'vendor': '07b3', 'product': '0c3b', 'bcd': '0605'}},
    {'id': 'plustek-opticfilm-8200i', 'vendor': 'PLUSTEK', 'model': 'OpticFilm 8200i',
     'usb': {'vendor': '07b3', 'product': '130d', 'bcd': None}},
    {'id': 'plustek-opticfilm-8200i-se', 'vendor': 'PLUSTEK', 'model': 'OpticFilm 8200i SE',
     'usb': {'vendor': '07b3', 'product': '1825', 'bcd': None}},
]}}


def row(device, vendor, model):
    return f'{device}\t{vendor}\t{model}\tflatbed scanner\n'


def usb(product, bcd='0100', serial=None):
    return {'vendor': '07b3', 'product': product, 'bcd': bcd, 'serial': serial,
            'vendor_name': 'Plustek', 'product_name': 'OpticFilm', 'location': None}


def run(listing, usb_list):
    return ident.identify(ident.parse_listing(listing), usb_list, TABLE)


class Identify(unittest.TestCase):
    def test_8200i_se_exact_and_keeps_firmware_date(self):
        (r,), unclaimed = run(row('genesys:libusb:001:005', 'PLUSTEK', 'OpticFilm 8200i SE'),
                              [usb('1825', serial='20241113')])
        self.assertEqual(r['status'], ident.EXACT)
        self.assertEqual(r['id'], 'genesys/plustek-opticfilm-8200i-se')
        self.assertEqual(r['usb']['serial'], '20241113')
        self.assertEqual(unclaimed, [])

    def test_se_and_original_8200i_never_mix(self):
        (r,), unclaimed = run(row('genesys:libusb:001:005', 'PLUSTEK', 'OpticFilm 8200i'),
                              [usb('1825')])
        # Driver says 8200i, but the only 8200i-family device is the SE's ID.
        self.assertEqual(r['status'], ident.UNCONFIRMED)
        self.assertNotIn(r['status'], ident.SCANNABLE)
        self.assertEqual([u['id'] for u in unclaimed], ['genesys/plustek-opticfilm-8200i-se'])

    def test_v2_unit_listed_as_v1_follows_usb_revision(self):
        # genesys lists the first table entry until sane_open re-checks bcdDevice.
        (r,), _ = run(row('genesys:libusb:001:007', 'PLUSTEK', 'OpticFilm 7600i (v1)'),
                      [usb('0c3b', bcd='0605')])
        self.assertEqual(r['status'], ident.EXACT)
        self.assertEqual(r['id'], 'genesys/plustek-opticfilm-7600i-v2')
        self.assertIn('re-checks the revision', r['problems'][0])

    def test_revision_split_without_usb_evidence_is_unconfirmed(self):
        (r,), _ = run(row('genesys:libusb:001:007', 'PLUSTEK', 'OpticFilm 7600i (v1)'), None)
        self.assertEqual(r['status'], ident.UNCONFIRMED)

    def test_unknown_revision_is_a_mismatch(self):
        (r,), _ = run(row('genesys:libusb:001:007', 'PLUSTEK', 'OpticFilm 7600i (v1)'),
                      [usb('0c3b', bcd='0999')])
        self.assertEqual(r['status'], ident.MISMATCH)
        self.assertNotIn(r['status'], ident.SCANNABLE)

    def test_revisions_agree(self):
        for model, bcd in (('OpticFilm 7600i (v1)', '0400'), ('OpticFilm 7600i (v2)', '0605')):
            (r,), _ = run(row('genesys:libusb:001:007', 'PLUSTEK', model), [usb('0c3b', bcd)])
            self.assertEqual(r['status'], ident.EXACT, model)

    def test_no_usb_evidence_is_name_only(self):
        (r,), _ = run(row('genesys:libusb:001:005', 'PLUSTEK', 'OpticFilm 8200i SE'), None)
        self.assertEqual(r['status'], ident.NAME_ONLY)
        self.assertEqual(r['id'], 'genesys/plustek-opticfilm-8200i-se')

    def test_unknown_name_is_not_guessed(self):
        (r,), _ = run(row('genesys:libusb:001:005', 'PLUSTEK', 'OpticFilm 8200i SE '),
                      [usb('1825')])
        self.assertEqual(r['status'], ident.EXACT)  # padding is trimmed
        (r,), _ = run(row('genesys:libusb:001:005', 'PLUSTEK', 'OpticFilm 8200iSE'),
                      [usb('1825')])
        self.assertEqual(r['status'], ident.UNRECOGNIZED)
        self.assertIsNone(r['id'])

    def test_two_identical_scanners_are_ambiguous(self):
        listing = (row('genesys:libusb:001:005', 'PLUSTEK', 'OpticFilm 8200i SE') +
                   row('genesys:libusb:001:006', 'PLUSTEK', 'OpticFilm 8200i SE'))
        results, _ = run(listing, [usb('1825'), usb('1825')])
        self.assertEqual([r['status'] for r in results], [ident.AMBIGUOUS] * 2)

    def test_two_identical_scanners_without_usb_evidence(self):
        listing = (row('genesys:libusb:001:005', 'PLUSTEK', 'OpticFilm 8200i SE') +
                   row('genesys:libusb:001:006', 'PLUSTEK', 'OpticFilm 8200i SE'))
        results, _ = run(listing, None)
        self.assertEqual([r['status'] for r in results], [ident.AMBIGUOUS] * 2)

    def test_backend_without_table(self):
        (r,), _ = run(row('test:0', 'Noname', 'frontend-tester'), [])
        self.assertEqual(r['status'], ident.NO_TABLE)

    def test_scanner_the_driver_did_not_list(self):
        results, unclaimed = run('', [usb('1825'), {**usb('ffff'), 'vendor': '05ac'}])
        self.assertEqual(results, [])
        self.assertEqual([u['id'] for u in unclaimed], ['genesys/plustek-opticfilm-8200i-se'])

    def test_listing_ignores_noise(self):
        rows = ident.parse_listing('warning: something\n' +
                                   row('genesys:libusb:001:005', 'PLUSTEK', 'OpticFilm 8200i SE'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['backend'], 'genesys')


class UsbReaders(unittest.TestCase):
    def test_macos_registry(self):
        tree = [{'IORegistryEntryName': 'Root', 'IORegistryEntryChildren': [
            {'idVendor': 0x07b3, 'idProduct': 0x1825, 'bcdDevice': 0x0100,
             'USB Serial Number': '20241113', 'USB Product Name': 'OpticFilm 8200i SE',
             'USB Vendor Name': 'Plustek', 'locationID': 0x01100000}]}]
        (d,) = ident.usb_devices_macos(plistlib.dumps(tree))
        self.assertEqual((d['vendor'], d['product'], d['bcd'], d['serial'], d['location']),
                         ('07b3', '1825', '0100', '20241113', '01100000'))

    def test_linux_sysfs(self):
        with tempfile.TemporaryDirectory() as root:
            dev = Path(root, '1-1.2')
            dev.mkdir()
            for name, value in {'idVendor': '07b3\n', 'idProduct': '1825\n',
                                'bcdDevice': '0100\n', 'serial': '20241113\n'}.items():
                (dev / name).write_text(value)
            Path(root, 'usb1').mkdir()  # hub entry without descriptors
            (d,) = ident.usb_devices_linux(Path(root))
        self.assertEqual((d['vendor'], d['product'], d['bcd'], d['serial'], d['location']),
                         ('07b3', '1825', '0100', '20241113', '1-1.2'))


GENESYS_SNIPPET = '''
    Genesys_Model model;
    model = Genesys_Model();
    model.name = "plustek-opticfilm-7600i-v1";
    model.vendor = "PLUSTEK";
    model.model = "OpticFilm 7600i (v1)";
    s_usb_devices->emplace_back(0x07b3, 0x0c3b, 0x0400, model);
    model.name = "plustek-opticfilm-7600i-v2";
    model.model = "OpticFilm 7600i (v2)";
    s_usb_devices->emplace_back(0x07b3, 0x0c3b, 0x0605, model);
    model = Genesys_Model();
    model.name = "plustek-opticfilm-8200i-se";
    model.vendor = "PLUSTEK";
    model.model = "OpticFilm 8200i SE";
    s_usb_devices->emplace_back(0x07b3, 0x1825, model);
'''


class Extract(unittest.TestCase):
    def test_parses_carried_fields_and_bcd(self):
        entries = extract_models.parse_genesys(GENESYS_SNIPPET)
        self.assertEqual([e['id'] for e in entries], ['plustek-opticfilm-7600i-v1',
                                                      'plustek-opticfilm-7600i-v2',
                                                      'plustek-opticfilm-8200i-se'])
        self.assertEqual(entries[1]['vendor'], 'PLUSTEK')  # carried from v1
        self.assertEqual(entries[1]['usb']['bcd'], '0605')
        self.assertIsNone(entries[2]['usb']['bcd'])
        extract_models.check_genesys(entries)

    def test_reset_clears_fields(self):
        text = GENESYS_SNIPPET.replace('    model.vendor = "PLUSTEK";\n    model.model = '
                                       '"OpticFilm 8200i SE";\n', '')
        with self.assertRaises(extract_models.TableError):
            extract_models.parse_genesys(text)

    def test_duplicate_reported_name_fails(self):
        text = GENESYS_SNIPPET.replace('"OpticFilm 7600i (v2)"', '"OpticFilm 7600i (v1)"')
        with self.assertRaises(extract_models.TableError):
            extract_models.check_genesys(extract_models.parse_genesys(text))

    def test_shared_usb_id_without_bcd_fails(self):
        text = GENESYS_SNIPPET.replace('0x0c3b, 0x0605, model', '0x0c3b, model')
        with self.assertRaises(extract_models.TableError):
            extract_models.check_genesys(extract_models.parse_genesys(text))

    def test_unparsed_entry_fails(self):
        text = GENESYS_SNIPPET + '    s_usb_devices->emplace_back(make_entry());\n'
        with self.assertRaises(extract_models.TableError):
            extract_models.parse_genesys(text)

    def test_generated_table_is_consistent(self):
        table = json.loads(ident.TABLE.read_text())
        entries = table['backends']['genesys']
        extract_models.check_genesys(entries)
        se = [e for e in entries if e['id'] == 'plustek-opticfilm-8200i-se']
        self.assertEqual(se[0]['usb'], {'vendor': '07b3', 'product': '1825', 'bcd': None})


if __name__ == '__main__':
    unittest.main()
