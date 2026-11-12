"""Film identity, response-profile choices, and per-roll operator settings.

Ported from the author's earlier film-scan tool (film_stocks.py). Profiles ship as compact gzipped JSON
(data/profiles/*.json.gz, lossless); per-roll choices live in the roll
folder's roll.json ("film_choice") instead of film-scan's roll-stocks.json.
"""
from pathlib import Path
import datetime,gzip,hashlib,json,re
DATA=Path(__file__).resolve().parents[1]/'data'

def read(path,default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default

def stocks(root=DATA):
    return read(Path(root)/'film-stocks.json')['stocks']

def stock(value,root=DATA,strict=False):
    normalized=re.sub(r'[^a-z0-9]','',str(value or '').lower())
    for entry in stocks(root):
        if any(normalized==re.sub(r'[^a-z0-9]','',v.lower()) for v in [entry['id'],entry['name'],*entry.get('aliases',[])]):
            return dict(entry)
    if not normalized:return next(dict(s) for s in stocks(root) if s['id']=='unknown')
    if strict:raise ValueError('Unknown film selection. Run `openfilmkit stocks` for available IDs.')
    return dict(id='custom',name=str(value),iso=None,film='color-negative',default_profile='generic',candidate_profiles=[],identity_note='Operator-supplied label; no verified stock-specific response selected.')

def profile_bytes(ident,root=DATA):
    """The profile's compact JSON, or None if there is no such profile."""
    p=Path(root)/'profiles'/(ident+'.json.gz')
    if p.exists():return gzip.decompress(p.read_bytes())
    p=Path(root)/'profiles'/(ident+'.json')
    return p.read_bytes() if p.exists() else None

def profile_ids(root=DATA):
    # Ordered as film-scan listed its <id>.json files. Hidden files include the ._ sidecars
    # macOS writes on exFAT drives.
    return sorted({p.name.split('.')[0] for p in (Path(root)/'profiles').glob('*.json*') if not p.name.startswith('.')},key=lambda i:i+'.json')

def profile(ident,root=DATA):
    if ident=='generic':return dict(id='generic',name='Generic negative conversion',kind='generic',calibration='No stock-specific response curve; starting point for visual review.')
    if not re.fullmatch(r'[a-z0-9-]+',ident):raise ValueError('Invalid profile ID')
    data=profile_bytes(ident,root)
    if data is None:raise ValueError('Unknown response profile: '+ident)
    return json.loads(data)

def profile_sha256(ident,root=DATA):
    """Checksum of the profile's content, independent of compression."""
    data=profile_bytes(ident,root)
    return hashlib.sha256(json.dumps(json.loads(data),separators=(',',':')).encode()).hexdigest() if data is not None else None

def selection(stock_value=None,saved=None,metadata=None,profile_id=None,root=DATA,roll=None,roll_stock=None):
    """`saved` is a roll's film_choice; `roll` names the roll for the record. `roll_stock` is the stock the roll was
    started with (roll new): it stands in when nothing else says, as for a scan another program made, which has no
    capture record."""
    metadata=metadata or {};saved=saved or {}
    previous=metadata.get('film_selection') or {}
    recorded=saved.get('stock_id') or previous.get('stock_id') or metadata.get('stock')
    chosen=stock_value or recorded or roll_stock
    entry=stock(chosen,root)
    response=profile_id or (saved.get('profile_id') if not stock_value else None) or (previous.get('profile_id') if not stock_value and not saved else None) or entry['default_profile']
    model=profile(response,root)
    return dict(stock_id=entry['id'],stock=entry['name'],film=entry.get('film','color-negative'),iso_speed=entry['iso'],roll=roll,profile_id=response,profile_name=model['name'],profile_status=model['calibration'],identity_note=entry['identity_note'],candidate_profiles=entry.get('candidate_profiles',[]),made_in=saved.get('made_in'),year_hint=saved.get('year_hint'),edge_markings=saved.get('edge_markings',''),selection_basis='explicit stock option' if stock_value else ('saved roll choice' if saved else ("the roll's stock" if roll_stock and not recorded else 'capture metadata or unknown')),year_does_not_determine_emulsion=True)

def roll_choice(stock_value,profile_id=None,made_in=None,year_hint=None,edge_markings='',calibration_file=None,recover_clipped_blue=None,root=DATA):
    """Validate a roll's film choice; roll.set_choice() stores it."""
    if year_hint is not None and not 1900<=year_hint<=2100:raise ValueError('Year hint must be between 1900 and 2100')
    entry=stock(stock_value,root,strict=True);response=profile_id or entry['default_profile'];profile(response,root)
    if calibration_file is not None:calibration_file=str(Path(calibration_file).expanduser().resolve())
    value=dict(stock_id=entry['id'],profile_id=response,made_in=made_in,year_hint=year_hint,edge_markings=edge_markings,updated_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),source='Operator selection; year/country are identification clues, not proof of an emulsion')
    if calibration_file:value['calibration_file']=calibration_file
    if recover_clipped_blue is not None:value['recover_clipped_blue']=bool(recover_clipped_blue)
    return value
