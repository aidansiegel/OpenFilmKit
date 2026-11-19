"""The front end a genesys scan used, from the GL128 lines in its log, and what of that reaches the terminal."""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openfilmkit import capture  # noqa: E402

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(name):
    return (FIXTURES / f'gl128-{name}.scan.log').read_text()


class Frontend(unittest.TestCase):
    def test_a_search(self):
        self.assertEqual(capture.frontend(fixture('search-3600')), {
            'search': True,
            'warmup_reads': 2,
            'wide_strip_gain0': [31898, 46204, 36510],
            'gains': {'white': [27, 12, 26], 'image': [27, 12, 26]},
            'offsets': {'white': [30, 51, 30], 'dark': [24, 44, 24]},
            'dark': {'even': [878, 1044, 968], 'odd': [878, 1044, 968]},
            'white': [{'min': 43786, 'mean': 50376, 'max': 52823, 'at_adc_ceiling': 0, 'columns': 5184},
                      {'min': 48267, 'mean': 55462, 'max': 57740, 'at_adc_ceiling': 0, 'columns': 5184},
                      {'min': 48667, 'mean': 56013, 'max': 58456, 'at_adc_ceiling': 0, 'columns': 5184}],
        })
        self.assertEqual(capture.frontend_problems(capture.frontend(fixture('search-3600'))), [])

    def test_fixed_codes_that_clip_the_white_reference(self):
        fe = capture.frontend(fixture('fixed-3600'))
        self.assertIs(fe['search'], False)
        self.assertEqual(fe['gains'], {'white': [39, 32, 39], 'image': [41, 35, 42]})
        self.assertEqual(fe['offsets'], {'white': [56, 69, 61], 'dark': [50, 62, 55]})
        self.assertEqual([w['at_adc_ceiling'] for w in fe['white']], [4382, 5184, 5184])
        for absent in ('wide_strip_gain0', 'warmup_reads', 'warnings', 'image_gain'):
            self.assertNotIn(absent, fe)
        self.assertEqual(capture.frontend_problems(fe),
                         ['White reference clipped in red, green and blue: the front end is set too hot.'])

    def test_7200_dpi(self):
        fe = capture.frontend(fixture('search-7200'))
        self.assertEqual(fe['offsets'], {'white': [29, 48, 28], 'dark': [23, 41, 22]})
        self.assertEqual({w['columns'] for w in fe['white']}, {10372})
        self.assertEqual(fe['dark']['odd'], [869, 1029, 931])

    def test_nothing_logged_is_nothing_recorded(self):
        self.assertEqual(capture.frontend(''), {})
        self.assertEqual(capture.frontend('Progress: 100.0%\r[1:2:3.4] [genesys] sane_init_impl: hello\n'), {})
        self.assertEqual(capture.frontend_problems(None), [])

    def test_only_what_was_logged(self):
        fe = capture.frontend('[20:00:02.527104] [genesys] GL128 shading white ch1 min=1 mean=2 max=3 '
                              'at_adc_ceiling=52/5184\n')
        self.assertEqual(fe, {'white': [None, {'min': 1, 'mean': 2, 'max': 3, 'at_adc_ceiling': 52,
                                               'columns': 5184}]})
        self.assertEqual(capture.frontend_problems(fe),
                         ['White reference clipped in green: the front end is set too hot.'])
        just_over = capture.frontend('[genesys] GL128 shading white ch0 min=1 mean=2 max=3 at_adc_ceiling=51/5184\n')
        self.assertEqual(capture.frontend_problems(just_over), [])   # 51 of 5184 is under 1%

    def test_warm_up_reads_without_the_summary_line(self):
        log = ('[genesys] GL128 AFE warm-up read 2: (1,2,3)\n'
               '[genesys] GL128 AFE warm-up read 3: (1,2,3) settled\n')
        self.assertEqual(capture.frontend(log), {'warmup_reads': 3})

    def test_search_warnings(self):
        log = ('[x] [genesys] GL128 AFE search: ch1 optical black barely moved (40 -> 41); offset 23\n'
               '[x] [genesys] GL128 AFE search: ch2 optical black barely moved (40 -> 41); offset 19\n'
               '[x] [genesys] GL128 AFE warm-up: wide strip still moving after 8 reads; using the last\n'
               '[x] [genesys] GL128 AFE search: wide strip too dark (lamp not lit?); fixed codes\n')
        fe = capture.frontend(log)
        self.assertIs(fe['search'], True)
        self.assertEqual(len(fe['warnings']), 4)
        self.assertTrue(fe['warnings'][0].startswith('GL128 AFE search: ch1 optical black barely moved'))
        self.assertEqual(capture.frontend_problems(fe), [
            "The front-end search couldn't set green's black offset and used a fixed one.",
            "The front-end search couldn't set blue's black offset and used a fixed one.",
            'The lamp was still warming up when the front-end gains were set.',
            'The front-end search found the white strip too dark (is the lamp lit?) and kept the fixed codes.'])

    def test_the_reworked_patchs_lines(self):
        """patches/0003 (branch gl128-upstream-ready): the final-offsets line also gives the dark offsets,
        new fallback lines, and the cold warm-up line gives the strip's means (ignored either way)."""
        log = ('[x] [genesys] GL128 cold lamp warm-up 3s: peak=41000\n'
               '[x] [genesys] GL128 cold lamp warm-up 3s: (31000,46000,36000)\n'
               '[x] [genesys] GL128 AFE search: black at 0x80 (2781,2612,2826), at 0xff (5274,5138,5348) '
               '-> pre-offsets (-11,-1,-12)\n'
               '[x] [genesys] GL128 AFE search: ch1 lamp-on strip too dark (5000); lamp not lit?\n'
               '[x] [genesys] GL128 AFE warm-up read 8: (31898,5000,36510)\n'
               '[x] [genesys] GL128 AFE warm-up: lamp-on strip still moving after 8 reads; using the last\n'
               '[x] [genesys] GL128 AFE search: wide strip at gain 0 (31898,5000,36510) after 8 read(s), '
               'targets (50518,55518,56428) -> gains (27,63,26)\n'
               '[x] [genesys] GL128 AFE search: black at 0x80 (4298,3126,4253), at 0xff (8201,6164,8116) '
               '-> offsets (30,51,30), dark (24,44,24)\n'
               '[x] [genesys] GL128 AFE search: failed; fixed codes\n'
               '[x] [genesys] GL128 AFE codes: gains white (39,32,39) image (41,35,42), offsets white (56,69,61) '
               'dark (50,62,55)\n')
        fe = capture.frontend(log)
        self.assertIs(fe['search'], False)
        self.assertEqual(fe['warmup_reads'], 8)
        self.assertEqual(fe['wide_strip_gain0'], [31898, 5000, 36510])
        self.assertEqual(fe['gains'], {'white': [39, 32, 39], 'image': [41, 35, 42]})
        self.assertEqual(fe['offsets'], {'white': [56, 69, 61], 'dark': [50, 62, 55]})
        self.assertEqual(len(fe['warnings']), 3)
        self.assertEqual(capture.frontend_problems(fe), [
            'The front-end search found the lamp-on strip too dark in green: is the lamp lit?',
            'The lamp was still warming up when the front-end gains were set.',
            'The front-end search failed and kept the fixed codes.'])
        self.assertIs(capture.frontend('[x] [genesys] GL128 AFE search: failed; fixed codes\n'
                                       '[x] [genesys] GL128 AFE search: black at 0x80 (1,2,3), at 0xff (4,5,6) '
                                       '-> offsets (1,2,3), dark (1,2,3)\n')['search'], False)

    def test_image_gain(self):
        fe = capture.frontend('[genesys] GL128 image gain: asked (41,35,42), got (40,35,42): '
                              'gains (40,35,42), offsets (56,69,-61)\n')
        self.assertEqual(fe, {'image_gain': {'asked': [41, 35, 42], 'got': [40, 35, 42],
                                             'gains': [40, 35, 42], 'offsets': [56, 69, -61]}})

    def test_a_later_calibration_counts(self):
        first = '[genesys] GL128 AFE codes: gains white (1,1,1) image (1,1,1), offsets white (1,1,1) dark (1,1,1)\n'
        self.assertEqual(capture.frontend(first + fixture('fixed-3600'))['gains']['white'], [39, 32, 39])


SAID = ('[19:59:58.105973] [sanei_debug] Setting debug level of genesys to 4.\n'
        '[19:59:58.394927] [genesys] Genesys_Settings{\n'
        '    xres: 3600 yres: 3600\n'
        '    depth: 16\n'
        '}\n'
        'Progress: 0.0%\r'
        '[20:00:44.852427] [genesys] genesys_read_ordered_data: frontend requested 1048576 bytes\n'
        'Progress: 99.6%\r'
        '[genesys] an older build, with no time\n'
        'Progress: 100.0%\r'
        'scanimage: sane_read: Error during device I/O\n')
SHOWN = 'Progress: 0.0%\rProgress: 99.6%\rProgress: 100.0%\rscanimage: sane_read: Error during device I/O\n'


class Display(unittest.TestCase):
    def shown(self, size):
        display = capture._Display()
        out = [display.feed(SAID[i:i + size]) for i in range(0, len(SAID), size)]
        return ''.join(out) + display.feed('', end=True)

    def test_only_what_scanimage_itself_says(self):
        for size in (1, 7, 1024):
            self.assertEqual(self.shown(size), SHOWN, size)

    def test_a_last_line_with_no_end_is_shown_at_the_end(self):
        display = capture._Display()
        self.assertEqual(display.feed('scanimage: done'), '')
        self.assertEqual(display.feed('', end=True), 'scanimage: done')


class Environment(unittest.TestCase):
    def runtime(self, **env):
        return types.SimpleNamespace(env=lambda: dict(env, SANE_CONFIG_DIR='x'))

    def test_genesys_logs_at_level_4(self):
        self.assertEqual(capture.scan_env(self.runtime(), 'genesys')['SANE_DEBUG_GENESYS'], '4')
        self.assertEqual(capture.scan_env(self.runtime(), 'genesys:libusb:000:001')['SANE_DEBUG_GENESYS'], '4')

    def test_a_level_already_set_stays(self):
        env = capture.scan_env(self.runtime(SANE_DEBUG_GENESYS='1'), 'genesys')
        self.assertEqual(env['SANE_DEBUG_GENESYS'], '1')

    def test_other_backends_are_left_alone(self):
        self.assertNotIn('SANE_DEBUG_GENESYS', capture.scan_env(self.runtime(), 'test:0'))


STUB = '''#!{python}
import os, sys
if '--version' in sys.argv:
    print('stub')
    sys.exit(0)
sys.stderr.write('level=' + os.environ.get('SANE_DEBUG_GENESYS', 'none') + chr(10))
sys.stderr.write(open({log!r}).read())
for n in range(3):
    sys.stderr.write(f'Progress: {{n}}%' + chr(13))
sys.stdout.buffer.write(bytes(2000))
'''


@unittest.skipIf(os.name != 'posix', 'the stand-in for scanimage is a script')
class Capture(unittest.TestCase):
    """capture() with a stand-in for scanimage that talks like the patched genesys driver at level 4."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.program = self.root / 'scanimage'
        self.program.write_text(STUB.format(python=sys.executable, log=str(FIXTURES / 'gl128-fixed-3600.scan.log')))
        self.program.chmod(0o755)
        self.out = self.root / 'frame.tif'
        info = {'width': 10, 'height': 10, 'dpi_x': 75.0, 'dpi_y': 75.0, 'bits_per_channel': 16, 'channels': 3}
        patch = mock.patch.multiple(capture, MIN_FREE=0, MARGIN=0, tiffinfo=lambda: 'tiffinfo',
                                    inspect_tiff=lambda path: info, check_tiff=lambda *a: None,
                                    channel_health=lambda path: {'channels': {}, 'warnings': []})
        patch.start()
        self.addCleanup(patch.stop)

    def scan(self, device, env=None):
        rt = types.SimpleNamespace(scanimage=self.program, summary=lambda: {}, version=lambda: 'stub',
                                   env=lambda: dict(os.environ, **(env or {})))
        shown = io.StringIO()
        with mock.patch.dict(os.environ):
            os.environ.pop('SANE_DEBUG_GENESYS', None)
            record = capture.capture(rt, self.out, device, {'source': None, 'mode': 'Color', 'depth': 16, 'dpi': 75},
                                     None, {}, {}, '', progress=shown)
        return record, shown.getvalue()

    def test_the_record_says_what_the_front_end_was(self):
        record, shown = self.scan('genesys')
        self.assertEqual(shown, 'level=4\nProgress: 0%\rProgress: 1%\rProgress: 2%\r')   # no debug lines
        self.assertEqual(record['frontend'], capture.frontend(fixture('fixed-3600')))
        self.assertEqual(json.loads(Path(str(self.out) + '.json').read_text())['frontend'], record['frontend'])
        log = Path(str(self.out) + '.scan.log').read_text()
        self.assertIn('GL128 AFE codes', log)                              # the log has all of it
        self.assertIn('Progress: 2%', log)

    def test_a_debug_level_already_chosen_is_kept(self):
        _, shown = self.scan('genesys', {'SANE_DEBUG_GENESYS': '2'})
        self.assertTrue(shown.startswith('level=2\n'))

    def test_another_backend_isnt_asked_for_debug_lines(self):
        _, shown = self.scan('test:0')
        self.assertTrue(shown.startswith('level=none\n'))


if __name__ == '__main__':
    unittest.main()
