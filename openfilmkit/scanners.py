"""Scanner profiles and capture plans.

A profile (scanners/<backend>/<model-id>.json) holds what SANE doesn't know:
tested settings, frame size, required patches, the hardware record. What a
scanner *can* do always comes from the driver's option listing; a plan that
the driver doesn't allow is refused, whatever the profile says.
"""
import json
from pathlib import Path

from . import options as opts

PROFILES = Path(__file__).parent / 'scanners'


class PlanError(Exception):
    pass


def profile(scanner_id):
    if not scanner_id:
        return None
    backend, _, model = scanner_id.partition('/')
    path = PROFILES / backend / f'{model}.json'
    return json.loads(path.read_text()) if path.is_file() else None


def all_profiles():
    return [json.loads(p.read_text()) for p in sorted(PROFILES.glob('*/*.json'))]


def full_area(options):
    """The driver's whole scan area as scanimage geometry (mm), or None if it has no geometry.

    Deliberately not genesys's --ignore-internal-offsets: that moves the window to the scanner's
    physical origin without enlarging it, so a film scan would miss the frame."""
    geo = {}
    for name, end in (('l', 'min'), ('t', 'min'), ('x', 'max'), ('y', 'max')):
        o = options.get(name)
        if not o or o.get('kind') != 'range' or o.get('inactive'):
            return None
        geo[name] = o[end]
    return geo


def plan(options, prof=None, dpi=None, source=None, runtime_patches=(), whole_area=False):
    """Choose source, mode, depth and resolution for a 48-bit linear master.

    With whole_area, also the driver's full scan area (plan['geometry']) instead of whatever
    area it selects by default. Returns (plan, warnings). Raises PlanError if the driver can't do it.
    """
    warnings = []
    cap = (prof or {}).get('capture', {})
    choose = []   # what must be chosen: all said at once, not one run each

    sources = options.get('source', {}).get('choices') if 'source' in options else None
    source = source or cap.get('source')
    if source is None and sources is not None:
        film = opts.film_sources(options)
        if len(film) == 1:
            source = film[0]
        else:
            choose.append(f"--source from: {', '.join(map(str, sources))}")
    if source is not None and sources is not None and source not in sources:
        raise PlanError(f"the driver offers sources {sources}, not '{source}'")

    mode = cap.get('mode', 'Color')
    modes = options.get('mode', {}).get('choices', [])
    if mode not in modes:
        raise PlanError(f"the driver offers modes {modes}, not '{mode}'")

    depth = cap.get('depth', 16)
    depths = options.get('depth', {}).get('choices', [])
    if depth not in depths:
        raise PlanError(f'16-bit capture is required for masters; the driver offers depths {depths}')

    dpi = dpi or cap.get('default_dpi')
    if dpi is None:
        listed = opts.resolutions(options)
        hint = f" ({', '.join(map(str, listed))})" if listed else ''
        choose.append(f'--dpi{hint}')
    if choose:
        raise PlanError(('no profile for this scanner; ' if prof is None else '')
                        + 'choose ' + '; and '.join(choose))
    if not opts.allows_resolution(options, dpi):
        raise PlanError(f'the driver does not offer {dpi} dpi')
    if cap.get('dpi') and dpi not in cap['dpi']:
        warnings.append(f"{dpi} dpi is allowed by the driver but untested here "
                        f"(tested: {', '.join(map(str, cap['dpi']))})")

    missing = [p for p in (prof or {}).get('requires_patches', []) if p not in runtime_patches]
    if missing:
        warnings.append(f"this runtime lacks patches the profile needs: {', '.join(missing)}")
    if prof is None:
        warnings.append('no kit profile for this scanner: driver defaults only, no frame-size check')

    result = {'source': source, 'mode': mode, 'depth': depth, 'dpi': dpi}
    if whole_area:
        geometry = full_area(options)
        if geometry is None:
            raise PlanError('the driver reports no adjustable scan area, so it always scans its full area; '
                            'drop --full-area')
        result['geometry'] = geometry
    return result, warnings


def scan_area_mm(options, prof=None):
    frame = (prof or {}).get('frame_mm')
    if frame:
        return frame['width'], frame['height']
    x, y = options.get('x'), options.get('y')
    if x and y and x.get('kind') == 'range' and y.get('kind') == 'range':
        return float(x['max']), float(y['max'])
    return None


def estimate_bytes(options, prof, dpi, depth=16, channels=3, geometry=None):
    area = ((geometry['x'] - geometry['l'], geometry['y'] - geometry['t']) if geometry
            else scan_area_mm(options, prof))
    if not area:
        return None
    w, h = (int(mm / 25.4 * dpi) for mm in area)
    return w * h * channels * depth // 8
