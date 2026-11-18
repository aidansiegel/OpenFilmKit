"""Whether each channel of a scan holds what a scanner should deliver.

A file can be perfectly formed and still be damaged: the GL128 driver's blue offset bug put blue at
exactly 0 over much of a frame, and every structural check passed. This looks at the values: how
much of each channel is pinned at 0 or at full scale (detail lost there), and how many codes inside
its range never occur (fewer real levels than 16 bits claim). A digital gain leaves those gaps,
whether it's applied in later processing or by the scanner's own calibration: the OpticFilm
8200i SE's green skips 10-60% of its codes, changing from scan to scan with the same calibration
(it rose over the first 40 minutes of a session, then held), which costs it up to about 1.4 of
its 16 bits. Gaps that cost less than COSTLY bits are a note, not a warning. Codes are
counted on every pixel, read in bands, since a sample leaves codes empty that the full scan uses
(a sample of a 3600 dpi frame doubled the share); a 73 MP master takes a few seconds.
"""
import math

AT_ZERO = 0.001         # share of a channel at exactly 0 that is reported (a scanner's black sits above 0)
AT_FULL = 0.001         # share at full scale that is reported
MISSING = 0.05          # share of codes never used, where they'd be expected, that is reported
COSTLY = 2.0            # bits lost to unused codes from which they're a warning, not a note
DENSE = 20              # samples a code is expected to get before its absence counts
BAND = 256              # rows read at a time for the histograms
NAMES = ('red', 'green', 'blue')


def _expected(hist):
    """Codes whose neighborhood is well used, so that their absence means something. Codes beside
    one that holds most of its neighborhood (a channel stuck at one value, or at 0) don't count:
    a stretch leaves many codes used evenly with gaps between, not a spike."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    window = 65
    hist = hist.copy()
    hist[[0, -1]] = 0                                   # clamping is reported on its own
    padded = np.pad(hist, window // 2)
    nearby = sliding_window_view(padded, window)
    total = nearby.sum(axis=1)
    return hist, (total >= DENSE * window) & (nearby.max(axis=1) <= total / 2)


def _missing_codes(hist):
    """The share of codes, where neighboring codes are well used, that never occur."""
    import numpy as np
    hist, expected = _expected(hist)
    if not expected.any():
        return None
    return float(np.mean(hist[expected] == 0))


def _histograms(pixels, top):
    """Per-channel histograms of every pixel, read a band of rows at a time (a memory map stays a
    memory map)."""
    import numpy as np
    hists = np.zeros((3, top + 1), np.int64)
    for y in range(0, pixels.shape[0], BAND):
        band = np.asarray(pixels[y:y + BAND])
        for c in range(3):
            hists[c] += np.bincount(band[..., c].ravel(), minlength=top + 1)[:top + 1]
    return hists


def _median(hist):
    import numpy as np
    return int(np.searchsorted(np.cumsum(hist), hist.sum() / 2))


def check(pixels, top=65535):
    """Per-channel findings for an (h, w, 3) array of 16-bit samples (a memory map is fine: it's read
    a band at a time). Returns {'channels': {name: {...}}, 'warnings': [plain sentences],
    'notes': [plain sentences about what's there but costs little]}."""
    import numpy as np
    hists = _histograms(pixels, top)
    channels, warnings, notes = {}, [], []
    for c, name in enumerate(NAMES):
        hist = hists[c]
        count = int(hist.sum())
        used = np.flatnonzero(hist)
        zero, full = float(hist[0] / count), float(hist[top] / count)
        missing = _missing_codes(hist)
        ch = {'min': int(used[0]), 'median': _median(hist), 'max': int(used[-1]),
              'at_zero': round(zero, 5), 'at_full_scale': round(full, 5),
              'missing_codes': None if missing is None else round(missing, 4)}
        channels[name] = ch
        if zero > AT_ZERO:
            warnings.append(f'{name} is at exactly 0 in {zero:.1%} of the scan: its black level is clamped, '
                            'so detail in the densest parts is lost (a scanner offset fault, like the GL128 blue bug)')
        if full > AT_FULL:
            warnings.append(f'{name} is at full scale in {full:.1%} of the scan: detail in the clearest parts is lost')
        if missing is None or missing <= MISSING:
            continue
        bits = -math.log2(1 - missing)                   # levels lost, in bits of the 16
        ch['missing_bits'] = round(bits, 2)
        said = (f'{name} never uses {missing:.0%} of the codes in its range, so it holds about {bits:.1f} bits '
                'fewer than 16: a digital gain leaves gaps like these, in later processing or in the '
                "scanner's own calibration")
        (warnings if bits >= COSTLY else notes).append(said)
    return {'channels': channels, 'warnings': warnings, 'notes': notes}


def describe(report):
    """Lines for a person: one per channel, then the warnings."""
    lines = []
    for name, ch in report['channels'].items():
        missing = '—' if ch['missing_codes'] is None else f"{ch['missing_codes']:.0%}"
        lines.append(f"  {name:<5} {ch['min']:>5}–{ch['max']:<5} median {ch['median']:>5}   "
                     f"at 0: {ch['at_zero']:.2%}   at full: {ch['at_full_scale']:.2%}   codes unused: {missing}")
    return lines + [f'! {w}' for w in report['warnings']] + [f'  {n}' for n in report.get('notes', [])]
