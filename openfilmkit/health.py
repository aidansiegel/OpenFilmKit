"""Whether each channel of a scan holds what a scanner should deliver.

A file can be perfectly formed and still be damaged: the GL128 driver's blue offset bug put blue at
exactly 0 over much of a frame, and every structural check passed. This looks at the values: how
much of each channel is pinned at 0 or at full scale (detail lost there), and how many codes inside
its range never occur, which a digital stretch or remapping leaves behind (fewer real levels than
16 bits claim). It measures a sample of the pixels, so a 73 MP master takes a moment.
"""
import math

SAMPLES = 4_000_000     # pixels looked at, at most
AT_ZERO = 0.001         # share of a channel at exactly 0 that is reported (a scanner's black sits above 0)
AT_FULL = 0.001         # share at full scale that is reported
MISSING = 0.05          # share of codes never used, where they'd be expected, that is reported
DENSE = 20              # samples a code is expected to get before its absence counts
NAMES = ('red', 'green', 'blue')


def _missing_codes(hist):
    """The share of codes, where neighboring codes are well used, that never occur. Codes beside
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
    expected = (total >= DENSE * window) & (nearby.max(axis=1) <= total / 2)
    if not expected.any():
        return None
    return float(np.mean(hist[expected] == 0))


def check(pixels, top=65535):
    """Per-channel findings for an (h, w, 3) array of 16-bit samples (a memory map is fine: only a
    sample is read). Returns {'channels': {name: {...}}, 'warnings': [plain sentences]}."""
    import numpy as np
    h, w = pixels.shape[:2]
    step = max(1, math.ceil(math.sqrt(h * w / SAMPLES)))
    sample = np.asarray(pixels[::step, ::step])
    channels, warnings = {}, []
    for c, name in enumerate(NAMES):
        v = sample[..., c].ravel()
        hist = np.bincount(v, minlength=top + 1)
        zero, full = float(hist[0] / v.size), float(hist[top] / v.size)
        missing = _missing_codes(hist)
        channels[name] = {'min': int(v.min()), 'median': int(np.median(v)), 'max': int(v.max()),
                          'at_zero': round(zero, 5), 'at_full_scale': round(full, 5),
                          'missing_codes': None if missing is None else round(missing, 4)}
        if zero > AT_ZERO:
            warnings.append(f'{name} is at exactly 0 in {zero:.1%} of the scan: its black level is clamped, '
                            'so detail in the densest parts is lost (a scanner offset fault, like the GL128 blue bug)')
        if full > AT_FULL:
            warnings.append(f'{name} is at full scale in {full:.1%} of the scan: detail in the clearest parts is lost')
        if missing is not None and missing > MISSING:
            warnings.append(f'{name} never uses {missing:.0%} of the codes in its range: it was stretched or remapped '
                            'digitally, so it holds fewer real levels than 16 bits')
    return {'sampled_every': step, 'channels': channels, 'warnings': warnings}


def describe(report):
    """Lines for a person: one per channel, then the warnings."""
    lines = []
    for name, ch in report['channels'].items():
        missing = '—' if ch['missing_codes'] is None else f"{ch['missing_codes']:.0%}"
        lines.append(f"  {name:<5} {ch['min']:>5}–{ch['max']:<5} median {ch['median']:>5}   "
                     f"at 0: {ch['at_zero']:.2%}   at full: {ch['at_full_scale']:.2%}   codes unused: {missing}")
    return lines + [f'! {w}' for w in report['warnings']]
