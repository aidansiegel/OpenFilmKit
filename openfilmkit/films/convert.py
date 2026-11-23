"""Stock-aware viewing copies from linear RGB16 negatives and slides; masters stay unchanged.

Reads the kit's own masters and the raw scans SilverFast (48-bit HDR, 64-bit HDRi) and VueScan (48-bit raw,
64-bit RGBI raw) save, so scans made with those on any system convert the same way.

Ported from the author's earlier film-scan tool (film_convert.py). The LUTs and rendering are unchanged, so a
full-size conversion matches film-scan byte for byte; a smaller copy averages each block of scan pixels where
film-scan kept one of them (less grain, no aliasing, small specks kept). Capture records may be film-scan's or the
kit's, and roll choices come from the roll folder's roll.json. Each conversion records what the scan's channels
hold (see health.py), so a clamped or stretched channel is reported, not silently converted.
"""
from pathlib import Path
import datetime,errno,hashlib,json,math,os,struct,threading,time,traceback
import numpy as np
import tifffile
from PIL import Image,ImageCms
from . import auto as film_auto, density as film_density, stocks as film_stocks
from .. import files, health as scan_health, roll as rolls
from ..pixels import tiff_page
Image.MAX_IMAGE_PIXELS=None
CHANNELS=('Red','Green','Blue')

def sha(path):
    with open(path,'rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def raw_open(path):
    """The scan's full-size image as a (height, width, 3) uint16 array: memory-mapped when stored plainly, decoded otherwise.

    Either byte order, chunky or planar samples, and any compression tifffile (with imagecodecs) reads. A fourth,
    infrared sample (VueScan RGBI) is dropped; thumbnails and SilverFast's separate infrared page are skipped."""
    try:
        with tifffile.TiffFile(path) as tif:
            page=tiff_page(tif)   # the full-size image, not a reduced copy or an infrared page; and all of it there
            spp,h,w=page.samplesperpixel,page.imagelength,page.imagewidth
            if page.photometric!=tifffile.PHOTOMETRIC.RGB or page.bitspersample!=16 or page.sampleformat!=1 or spp not in (3,4):
                raise ValueError(f'Conversion needs a linear RGB scan at 16 bits per channel (48-bit, or 64-bit with infrared); '
                                 f'this TIFF is {getattr(page.photometric,"name",page.photometric)} with {spp} x {page.bitspersample}-bit samples.')
            planar=page.planarconfig==tifffile.PLANARCONFIG.SEPARATE;shape=(spp,h,w) if planar else (h,w,spp)
            if page.compression==tifffile.COMPRESSION.NONE and page.is_contiguous:
                offset=page.dataoffsets[0]
                if offset+h*w*spp*2>Path(path).stat().st_size:raise ValueError('TIFF pixel data is truncated.')
                a=np.memmap(path,dtype=tif.byteorder+'u2',mode='r',offset=offset,shape=shape)
            else:a=page.asarray().reshape(shape)
    except tifffile.TiffFileError as e:raise ValueError(f'Not a readable TIFF: {e}') from e
    except (ValueError,OSError):raise
    except KeyError as e:raise ValueError(f'Unsupported TIFF compression: {e}') from e
    except Exception as e:raise ValueError(f'Not a readable TIFF: {str(e) or type(e).__name__}') from e   # a damaged file: the decoders raise whatever comes of it
    return np.moveaxis(a[:3],0,-1) if planar else a[:,:,:3]

OUTPUTS=('.jpg','.jpeg','.png','.tif','.tiff')

def shrink(a,f):
    """Every f x f block of raw RGB16 averaged into one pixel (blocks in the last row and column may be smaller).
    The raw scan is linear, so this averages the light itself: a smaller copy keeps every pixel's share instead of
    one pixel in f*f, which left full-size grain, aliased it, and let specks smaller than a block disappear."""
    if f==1:return np.array(a)   # a copy in memory, never `a` itself: a memory-mapped scan kept by the caller would hold its file open, which Windows won't then rename or delete
    h,w=a.shape[:2];rows,cols=np.arange(0,h,f),np.arange(0,w,f)
    total=np.add.reduceat(np.add.reduceat(np.asarray(a,np.uint32),rows,axis=0),cols,axis=1)
    n=np.minimum(f,h-rows)[:,None]*np.minimum(f,w-cols)[None,:]
    return np.rint(total/n[:,:,None]).astype(np.uint16)

def glance(raw,side=1800):
    """A small copy of the whole scan for measuring, and the block size it was shrunk by. Done a band at a time:
    shrinking a full 8200i SE scan in one go held about 850 MB of sums."""
    look=max(1,math.ceil(max(raw.shape[:2])/side));band=look*64
    return np.concatenate([shrink(raw[i:i+band],look) for i in range(0,raw.shape[0],band)]),look

EMPTY=.9   # every channel's median above this share of full scale: no film in the holder, the light went straight through

def looks_empty(pixels):
    """True for a scan of no film (an empty holder slot, past the end of the strip): nothing to measure or turn."""
    step=max(1,math.ceil(max(pixels.shape[:2])/600))
    return bool(np.median(pixels[::step,::step,:3].reshape(-1,3),axis=0).min()>EMPTY*65535)

ROLL_BALANCE='auto-balance.cache'   # JSON, but not named .json: the kit keeps NAME.json for the record of NAME

_ROLL_LOCKS,_ROLL_DONE={},{}   # one measuring of a roll at a time; how far it has got: {roll: (done, total)}

def _roll_lock(roll_dir):
    return _ROLL_LOCKS.setdefault(str(roll_dir),threading.Lock())

def roll_balance(roll_dir,chosen,model,measure=True,progress=None):
    """The automatic balance of every master on a roll, {name: gains}, for auto.settle. Each is measured the way
    convert measures its own (picture box, levels, curve, balance) on a small copy, and kept in the roll's
    auto-balance.cache with the master's size and time and the film choice, so a roll is read through once and
    then only when a frame is added or rescanned or the film choice changes. Frames that can't be read are skipped.
    measure=False: only what's already kept, measuring nothing (the app's thumbnails, which can't wait a minute).
    progress(done, total, seconds_left) is called as frames are measured (seconds_left None until 3 are done); one measuring of a roll runs at a time, and a second
    caller waits for it (hearing its progress) and then finds the frames already kept."""
    if measure:
        lock=_roll_lock(Path(roll_dir).resolve())
        while not lock.acquire(timeout=.5):
            if progress:progress(*_ROLL_DONE.get(str(Path(roll_dir).resolve()),(0,0,None)))
        try:return _roll_balance(roll_dir,chosen,model,True,progress)
        finally:lock.release()
    return _roll_balance(roll_dir,chosen,model,False,None)

def _roll_balance(roll_dir,chosen,model,measure,progress):
    roll_dir=Path(roll_dir);store=roll_dir/ROLL_BALANCE;key=f"{chosen['stock_id']}/{chosen['profile_id']}/2"   # /2: empty slots left out (measured again once)
    try:known=json.loads(store.read_text()) if store.is_file() else {}
    except ValueError:known={}
    if not isinstance(known,dict):known={}
    table,changed={},False
    masters=[m for m in sorted((roll_dir/'masters').glob('*.tif')) if not m.name.startswith('.') and m.is_file()]
    where=str(roll_dir.resolve());todo=[]
    for master in masters:
        st=master.stat();seen=known.get(master.name)
        if isinstance(seen,dict) and seen.get('size')==st.st_size and seen.get('mtime_ns')==st.st_mtime_ns and seen.get('film')==key:
            table[master.name]=seen['gains']
        elif measure:todo.append((master,st))
    started=time.monotonic()
    for n,(master,st) in enumerate(todo):
        # time left: the average so far, once 3 frames are done (the first reads are slow: the disk wakes up)
        left=round((time.monotonic()-started)/n*(len(todo)-n)) if n>=3 else None
        _ROLL_DONE[where]=(n,len(todo),left)
        if progress:progress(n,len(todo),left)
        try:
            small,_=glance(raw_open(master))
            if looks_empty(small):gains=None   # an empty slot says nothing about the roll's color; kept, so it isn't read again
            else:
                x0,y0,x1,y1=film_auto.picture_box(small);small=small[y0:y1,x0:x1]
                gains=film_auto.measure(small,make_lut(model,*fit_levels(small)))['gains']
        except (ValueError,OSError):continue
        table[master.name]=None if gains is None else [round(float(g),5) for g in gains]
        known[master.name]=dict(size=st.st_size,mtime_ns=st.st_mtime_ns,film=key,gains=table[master.name]);changed=True
    _ROLL_DONE.pop(where,None)
    if changed:
        tmp=store.with_name(f'.{store.name}.{os.getpid()}.{threading.get_ident()}.tmp')   # the app measures from two threads
        try:tmp.write_text(json.dumps(known,indent=1)+'\n');tmp.replace(store)
        except OSError as e:
            if e.errno==errno.ENOSPC:raise OSError(e.errno,e.strerror or os.strerror(e.errno),e.filename or str(tmp)) from e   # said, naming the place
            # otherwise a roll on a read-only disk or share: measured again next time
        finally:tmp.unlink(missing_ok=True)
    return {n:g for n,g in table.items() if g is not None}

def write_tiff(dest,pixels,icc):
    """A 16-bit (or 8-bit) RGB TIFF with the sRGB profile embedded, losslessly compressed (deflate with
    a horizontal predictor), and nothing time-dependent in it, so the same pixels give the same file."""
    tifffile.imwrite(dest,np.ascontiguousarray(pixels),photometric='rgb',compression='zlib',predictor=True,
                     iccprofile=icc,software=None,metadata=None)

def srgb_icc():
    """The embedded sRGB profile, with a fixed creation date so a conversion is byte-reproducible.

    film-scan embedded the current time (header bytes 24-35), so its files differed between runs."""
    icc=bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
    if any(icc[84:100]):raise RuntimeError('sRGB profile carries an ID checksum; cannot fix its date')
    icc[24:36]=struct.pack('>6H',2026,1,1,0,0,0);return bytes(icc)

def validate_levels(low,high):
    lo,hi=np.asarray(low,float),np.asarray(high,float)
    if lo.shape!=(3,) or hi.shape!=(3,) or not np.isfinite([lo,hi]).all() or np.any(lo<0) or np.any(hi>1) or np.any(hi-lo<1/65535):
        raise ValueError('Invalid or degenerate transmission endpoints.')
    return lo,hi

def named_file(path,what,keys):
    """What's in a JSON file named by an option or a roll's settings. ValueError naming the file when it has moved,
    is damaged, or isn't that kind of file (it lacks one of `keys`), where a traceback used to be."""
    try:data=json.loads(Path(path).read_text())
    except FileNotFoundError:raise ValueError(f'{what}: {path} is not there (moved or deleted?)') from None
    except ValueError as e:raise ValueError(f'{what}: {path} is damaged ({e})') from None
    if not isinstance(data,dict) or any(k not in data for k in keys):raise ValueError(f"{what}: {path} is not that kind of file (it has no {' or no '.join(keys)})")
    return data

def fit_levels(area):
    """Per-frame transmission endpoints: 0.2/99.8 percentiles of the film, ignoring a 4% border and clipped samples."""
    step=max(1,math.ceil(max(area.shape[:2])/1800));sample=area[::step,::step].astype(float)/65535
    sh,sw=sample.shape[:2];my,mx=max(1,int(sh*.04)),max(1,int(sw*.04));interior=sample[my:sh-my,mx:sw-mx] if sh>2*my and sw>2*mx else sample
    valid=(interior.min(2)>.001)&(interior.max(2)<.99)
    if valid.sum()<4:raise ValueError('Too little usable film area to fit levels; provide a photograph crop or shared levels.')
    return validate_levels(*np.percentile(interior[valid],[.2,99.8],axis=0))

def make_lut(model,low,high):
    """Negatives: scan transmission to linear scene exposure, between the frame's clearest (high) and densest (low) film.

    Data-sheet profiles keep the published density curve: density is read from the scan relative to the clearest
    film, which is taken as the curve's lowest density, exposure is looked up on the curve, and the densest film
    in the frame becomes white. DoRF profiles have no absolute scale; their response is normalized opacity, so
    the frame's opacity range is mapped onto it."""
    lo,hi=validate_levels(low,high);raw=np.arange(65536)/65535;table=np.empty((65536,3),np.uint16);t=np.maximum(raw,1/65535)
    for c,key in enumerate(CHANNELS):
        if model['kind']!='generic' and model.get('physical_traces'):
            trace=model['physical_traces'][key];L,D=np.asarray(trace['log_exposure'],float),np.asarray(trace['status_m_density'],float)
            if len(L)<3 or len(L)!=len(D) or not np.isfinite([L,D]).all() or np.any(np.diff(L)<0) or np.any(np.diff(D)<0) or D[-1]<=D[0]:raise ValueError('Invalid profile curve: '+key)
            keep=np.r_[True,np.diff(D)>0];L,D=L[keep],D[keep]
            exposure=lambda transmission:10**np.interp(D[0]+np.log10(hi[c]/transmission),D,L)
            z=np.clip(exposure(t)/exposure(max(lo[c],1/65535)),0,1)
        elif model['kind']!='generic':
            curve=model['curves'][key];response=np.asarray(curve['response'],float);exposure=np.asarray(curve['irradiance'],float)
            if len(response)<2 or len(response)!=len(exposure) or not np.isfinite([response,exposure]).all() or np.any(np.diff(response)<0) or np.any(np.diff(exposure)<0) or response[-1]<=response[0] or np.any(exposure<0) or np.any(exposure>1):
                raise ValueError('Invalid profile curve: '+key)
            z=np.clip((1/t-1/hi[c])/(1/max(lo[c],1/65535)-1/hi[c]),0,1)
            z=np.interp(z,response,exposure)
        else:
            # Generic density inversion between the measured channel endpoints.
            transmission=np.clip(raw,max(lo[c],1/65535),hi[c]);D=-np.log10(transmission)
            z=np.clip((D+np.log10(hi[c]))/np.log10(hi[c]/max(lo[c],1/65535)),0,1)
            z=(10**(1.05*z)-1)/(10**1.05-1)
        y=np.where(z<=.0031308,12.92*z,1.055*np.maximum(z,0)**(1/2.4)-.055)
        table[:,c]=np.rint(np.clip(y,0,1)*65535).astype(np.uint16)
    return table

def slide_lut(model,low,high):
    """Positive transparencies: keep the slide's look, but neutralize the differences between its dye curves.

    The clearest film in the frame (the high level) is taken as minimum density. Each channel's density goes back
    to exposure through that channel's own curve, then forward through the average of the three curves, so a gray
    in the scene comes out gray while the slide keeps its contrast. Generic: a plain stretch between the levels."""
    lo,hi=validate_levels(low,high);raw=np.arange(65536)/65535;table=np.empty((65536,3),np.uint16)
    if model['kind']=='generic':linear=np.clip((raw[:,None]-lo)/(hi-lo),0,1)
    else:
        traces=[(np.asarray(model['physical_traces'][k]['log_exposure'],float),np.asarray(model['physical_traces'][k]['density'],float)) for k in CHANNELS]
        if any(len(L)<3 or len(L)!=len(D) or np.any(np.diff(L)<0) or np.any(np.diff(D)>0) or D[-1]>=D[0] for L,D in traces):raise ValueError('Invalid slide profile curve')
        grid=np.linspace(min(L[0] for L,_ in traces),max(L[-1] for L,_ in traces),2048)
        reference=np.mean([np.interp(grid,L,D) for L,D in traces],axis=0)
        linear=np.empty((65536,3))
        for c,(L,D) in enumerate(traces):
            density=D[-1]+np.log10(hi[c]/np.maximum(raw,1/65535))
            exposure=np.interp(density,D[::-1],L[::-1])
            linear[:,c]=10**-(np.interp(exposure,grid,reference)-reference[-1])
    srgb=np.where(linear<=.0031308,12.92*linear,1.055*np.maximum(linear,0)**(1/2.4)-.055)
    table[:]=np.rint(np.clip(srgb,0,1)*65535).astype(np.uint16);return table

def render(raw,lut,contrast=1.3,gamma=1.0,warmth=0.0,top=255,saturation=1.0,gains=None,exposure=1.0,tone=None):
    """Viewing pixels from raw RGB16 through the LUT; top: 255 for 8-bit output, 65535 for 16-bit.
    saturation scales each pixel's color away from (above 1) or toward (below 1) its own gray, in
    linear light at constant luminance, before the tone curve; 1 leaves the pixels as they were.
    gains (per channel) and exposure come from auto.measure(): the frame's color balance and brightness,
    applied in linear light first. tone, also from auto.measure(), is the frame's own black point, pivot and
    contrast; with it, highlights roll off toward white instead of clipping, and `contrast` scales the frame's
    own (1.3 leaves it as measured), so the Contrast slider still works the same way."""
    if not (.1<=contrast<=4 and .1<=gamma<=4 and -1<=warmth<=1):raise ValueError('Contrast/gamma must be 0.1–4; warmth must be -1–1.')
    if not 0<=saturation<=3:raise ValueError('Saturation must be 0–3.')
    a=np.stack([lut[raw[:,:,c],c] for c in range(3)],2).astype(float)/65535
    if gains is not None or exposure!=1 or tone:
        lin=film_auto.decode(a)*(np.ones(3) if gains is None else np.asarray(gains,float))*exposure
        a=film_auto.encode(film_auto.shoulder(lin) if tone else np.clip(lin,0,1))
    weights=np.array([.2126,.7152,.0722]);L=np.sum(a*weights,2,keepdims=True)
    if warmth:
        strength=np.clip((.98-L)/.18,0,1);a*=1+strength*(np.array([.08,0,-.12])*warmth)
        a*=L/np.maximum(np.sum(a*weights,2,keepdims=True),1e-10)
    if saturation!=1:
        a=np.maximum(L+(a-L)*saturation,0)
        a*=L/np.maximum(np.sum(a*weights,2,keepdims=True),1e-10)   # back to the pixel's luminance
    if tone:target=film_auto.curve(L,tone['black'],tone['pivot'],max(.3,tone['contrast']*contrast/1.3),tone.get('middle'))**(1/gamma)
    else:v=np.clip(L,0,1);up=v**contrast;down=(1-v)**contrast;target=(up/(up+down))**(1/gamma)
    return np.rint(np.clip(a*target/np.maximum(L,1e-10),0,1)*top).astype(np.uint8 if top==255 else np.uint16)

def capture_metadata(source):
    """A capture record in the shape conversion reads: film-scan's as-is, the kit's mapped onto it."""
    meta=film_stocks.read(str(source)+'.json',{})
    if isinstance(meta.get('film'),dict):
        film=meta['film'];meta=dict(film=film.get('film'),stock=film.get('stock'),sha256=meta.get('sha256'))
    return meta

def convert(args,root=film_stocks.DATA):
    source=Path(args.source).expanduser().resolve();dest=Path(args.output).expanduser().resolve()
    if source==dest or dest.suffix.lower() not in OUTPUTS:raise ValueError('Choose a separate JPEG, PNG or TIFF destination.')
    deep=dest.suffix.lower() in ('.tif','.tiff');top=65535 if deep else 255
    recipe=Path(str(dest)+'.json');lutfile=Path(str(dest)+'.lut.npy');scratch=Path(str(dest)+'.rgb.partial')
    for p in [dest,recipe,lutfile,scratch]:
        if p.exists():raise ValueError('Refusing to overwrite '+str(p))
    if args.max_side is None:args.max_side=0 if deep else 1800   # a TIFF is a master: full size by default
    if args.max_side<0:raise ValueError('--max-side must be zero (full size) or positive')
    meta=capture_metadata(source)
    roll_dir=Path(args.roll).expanduser().resolve() if args.roll else rolls.containing(source)
    if roll_dir and not rolls.is_roll(roll_dir):raise ValueError(f'{roll_dir} is not a roll folder')
    about_roll=rolls.load(roll_dir) if roll_dir else {};saved_roll=about_roll.get('film_choice') or {}
    # A scan another program made has no capture record to say its film: what its roll was started as stands in.
    if not meta.get('film') and about_roll.get('film') not in (None,'','unknown'):meta['film']=about_roll['film']
    if meta.get('film')=='bw-negative':raise ValueError('This converter handles color negatives and slides; capture metadata identifies a black-and-white negative.')
    chosen=film_stocks.selection(args.stock,saved_roll,meta,args.profile,root,roll=roll_dir.name if roll_dir else None,roll_stock=about_roll.get('stock'))
    if meta.get('film')=='slide' and chosen['stock_id']=='unknown':chosen=film_stocks.selection('unknown-slide',None,{},args.profile,root)
    film=chosen['film'];model=film_stocks.profile(chosen['profile_id'],root)
    if meta.get('film') in ('slide','color-negative') and meta['film']!=film:raise ValueError(f"Capture metadata says {meta['film']}, but the selected stock is a {film}.")
    if model['kind']!='generic' and model.get('film','color-negative')!=film:raise ValueError(f"Profile {model['id']} is for {model.get('film','color-negative')} film, not {film}.")
    if args.contrast is None:args.contrast=1.0 if film=='slide' else 1.3  # a slide already carries its own contrast
    if not (.1<=args.contrast<=4 and .1<=args.gamma<=4 and -1<=args.warmth<=1):raise ValueError('Contrast/gamma must be 0.1–4; warmth must be -1–1.')
    saturation=getattr(args,'saturation',1.0)
    if saturation is None:saturation=1.0
    if not 0<=saturation<=3:raise ValueError('Saturation must be 0–3.')
    rotate=getattr(args,'rotate',None)
    if rotate is not None and int(rotate) not in (0,90,180,270):raise ValueError('Rotation must be 0, 90, 180 or 270 degrees clockwise.')
    auto=getattr(args,'auto',True) is not False
    dust=bool(getattr(args,'dust',False))
    digest=sha(source)
    if meta.get('sha256') and meta['sha256']!=digest:raise ValueError('Source TIFF does not match its capture checksum.')
    raw=raw_open(source);h,w=raw.shape[:2]
    small_copy,look=glance(raw)   # a small copy of the whole scan, for measuring
    if looks_empty(small_copy) and not args.levels_from:raise ValueError('this frame looks empty (no film in the holder), so there is no picture to save')
    found=None
    if args.crop:box=list(args.crop)
    elif auto:
        b=film_auto.picture_box(small_copy);found=[min(w,b[0]*look),min(h,b[1]*look),min(w,b[2]*look),min(h,b[3]*look)]
        box=found if found!=[0,0,min(w,small_copy.shape[1]*look),min(h,small_copy.shape[0]*look)] else [0,0,w,h]
    else:box=[0,0,w,h]
    x0,y0,x1,y1=box
    if not (0<=x0<x1<=w and 0<=y0<y1<=h):raise ValueError('Crop lies outside the source TIFF.')
    area=raw[y0:y1,x0:x1];health=scan_health.check(area)
    # Where color and levels are measured: the picture, unless the app's crop editor chose the box (a manual crop,
    # or none), when they're measured on the automatic crop all the same: moving the box changes what's in the
    # picture, not how it looks, and the saved picture matches the app's preview.
    mx0,my0,mx1,my1=box
    if auto and getattr(args,'crop_source',None) in ('manual','none'):
        b=film_auto.picture_box(small_copy);mx0,my0,mx1,my1=[min(w,b[0]*look),min(h,b[1]*look),min(w,b[2]*look),min(h,b[3]*look)]
    measured=area if (mx0,my0,mx1,my1)==tuple(box) else raw[my0:my1,mx0:mx1]
    if args.levels_from:
        prior_path=Path(args.levels_from).expanduser().resolve();prior=named_file(prior_path,'--levels-from',('film_selection','transmission_low_rgb','transmission_high_rgb'));selected=prior['film_selection'] or {}
        if selected.get('profile_id')!=chosen['profile_id'] or selected.get('stock_id')!=chosen['stock_id']:
            raise ValueError('Shared levels must use the same stock and response profile.')
        lo,hi=validate_levels(prior['transmission_low_rgb'],prior['transmission_high_rgb']);levels_basis=dict(type='shared',source=str(prior_path),sha256=sha(prior_path))
    else:
        lo,hi=fit_levels(measured);levels_basis=dict(type='per-frame',percentiles=[.2,99.8],note='May vary between frames; use --levels-from for a shared starting correction.')
    calibrated=None
    calibration_path=getattr(args,'calibration',None) or saved_roll.get('calibration_file')
    if calibration_path:
        if film=='slide':raise ValueError('Density calibration plans are for color negatives.')
        if args.levels_from:raise ValueError('Choose scene calibration or shared transmission endpoints, not both.')
        plan_path=Path(calibration_path).expanduser().resolve();plan=named_file(plan_path,'calibration file',('scenes',))
        scenes=plan['scenes'];scene_name=getattr(args,'scene',None) or plan.get('capture_scenes',{}).get(digest)
        if scene_name is None and len(scenes)!=1:raise ValueError('--scene is required for a calibration with several scenes.')
        matches=[r for r in scenes if scene_name is None or r['scene']==scene_name]
        if len(matches)!=1:raise ValueError('Unknown or duplicate calibration scene.')
        scene=matches[0]
        lut=film_density.density_lut(model,plan['film_base_transmission_rgb'],scene['density_offsets_rgb'],scene['density_black'],scene['density_white'],profile_strength=plan.get('profile_strength',.4),mid=scene.get('density_mid'),mid_target=scene.get('density_mid_target',.58))
        calibrated=dict(plan=str(plan_path),plan_sha256=sha(plan_path),scene=scene,film_base_transmission_rgb=plan['film_base_transmission_rgb'],recover_clipped_blue=bool(saved_roll.get('recover_clipped_blue',False) if getattr(args,'recover_clipped_blue',None) is None else args.recover_clipped_blue),note='Measured viewing correction; missing blue estimates are not restored capture detail.')
    else:lut=slide_lut(model,lo,hi) if film=='slide' else make_lut(model,lo,hi)
    lut_source=None
    if getattr(args,'lut',None):
        # An approved per-channel curve (raw code -> output, 65536 x 3 uint16), e.g. one a keeper was graded with.
        if calibrated:raise ValueError('Choose a density calibration or --lut, not both.')
        lut_path=Path(args.lut).expanduser().resolve()
        try:given=np.load(lut_path,allow_pickle=False)
        except (OSError,ValueError) as e:raise ValueError(f'--lut {lut_path}: not a NumPy array file ({e})')
        if given.shape!=(65536,3) or given.dtype!=np.uint16:raise ValueError(f'--lut {lut_path}: want a 65536 x 3 uint16 array, got {given.shape} {given.dtype}')
        lut=given;lut_source=dict(path=str(lut_path),sha256=sha(lut_path),note='Applied as given; the levels below were measured but not used.')
    gains,exposure,turn_reason=None,1.0,'given' if rotate is not None else None;roll_check=None;tone=None
    finishing=None
    if auto:
        small=small_copy[my0//look:-(-my1//look),mx0//look:-(-mx1//look)]
        if film!='slide' and not calibrated and not lut_source:   # an approved curve or a calibration is already the grade
            others=[g for n,g in roll_balance(roll_dir,chosen,model).items() if (roll_dir/'masters'/n).resolve()!=source] if roll_dir else None
            m=film_auto.measure(small,lut,others);gains,exposure,roll_check,tone=m['gains'],m['exposure'],m['roll_check'],m['tone']
        if rotate is None:
            rotate,turn_reason=film_auto.orientation(render(small,lut,args.contrast,args.gamma,args.warmth,255,saturation,gains,exposure,tone))
        finishing=dict(balance_rgb=None if gains is None else [round(float(g),5) for g in gains],exposure=round(float(exposure),5),tone=tone,
                       roll_check=roll_check,
                       crop_found=found,rotation_cw=rotate,rotation_reason=turn_reason,
                       note='Automatic: per-frame balance (checked against the roll\'s), exposure and tone (black point, contrast, highlights rolled off), the picture box, which way up. --no-auto turns it off.')
    rotate=int(rotate or 0);mirror=bool(getattr(args,'mirror',False))
    stride=1 if args.max_side==0 else max(1,math.ceil(max(area.shape[:2])/args.max_side));hh,ww=-(-area.shape[0]//stride),-(-area.shape[1]//stride)
    dest.parent.mkdir(parents=True,exist_ok=True)
    # The picture, its LUT and its recipe are each written under another name and given their own only once all are
    # whole, the recipe last. A run stopped partway (Ctrl-C, a full disk) then leaves nothing; it used to leave files
    # that looked finished, with no recipe, which the same command again refused to replace.
    tag=f'.{os.getpid()}.tmp';made={dest:dest.with_name(f'.{dest.stem}{tag}{dest.suffix}'),lutfile:lutfile.with_name(f'.{lutfile.stem}{tag}.npy'),recipe:recipe.with_name(f'.{recipe.name}{tag}')};placed=[]
    try:
        pixels=np.memmap(scratch,mode='w+',dtype='uint16' if deep else 'uint8',shape=(hh,ww,3))
        try:
            for start in range(0,hh,64):
                strip=shrink(area[start*stride:(start+64)*stride],stride)
                if calibrated:
                    if args.warmth:raise ValueError('Use neutral scene anchors instead of warmth with density calibration.')
                    if saturation!=1:raise ValueError('Saturation is not available with density calibration yet.')
                    pixels[start:start+64]=film_density.render_density(strip,lut,args.contrast,args.gamma,calibrated['recover_clipped_blue'],top)
                else:pixels[start:start+64]=render(strip,lut,args.contrast,args.gamma,args.warmth,top,saturation,gains,exposure,tone)
            dust_step=None
            if dust:   # on the finished picture, before it's turned: like clean's dust step (see clean.remove_dust)
                from .. import clean as cleaning
                dust_step=cleaning.remove_dust(pixels)[1]   # it hands back `pixels` too: kept, it held the scratch file mapped past `del pixels`, and Windows wouldn't delete it
            pixels.flush();icc=srgb_icc()
            turned=np.rot90(pixels,-rotate//90) if rotate else pixels   # clockwise, after the whole frame is rendered
            if mirror:turned=turned[:,::-1]   # then left to right, as the picture is seen
            if deep:write_tiff(made[dest],np.ascontiguousarray(turned) if rotate or mirror else pixels,icc)
            else:
                im=Image.fromarray(np.ascontiguousarray(turned) if rotate or mirror else pixels)
                try:im.save(made[dest],icc_profile=icc,**({'quality':95,'subsampling':0} if dest.suffix.lower()!='.png' else {}))
                finally:del im
        finally:
            turned=None;del pixels
        scratch.unlink(missing_ok=True)
        np.save(made[lutfile],lut)
        record=dict(created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),status='Starting conversion — requires visual review',source=str(source),source_sha256=digest,source_capture_metadata_sha256=sha(str(source)+'.json') if Path(str(source)+'.json').exists() else None,film_selection=chosen,film=film,profile_sha256=film_stocks.profile_sha256(chosen['profile_id'],root),profile_source=model.get('source'),raw_crop_pixels=box,transmission_low_rgb=lo.tolist(),transmission_high_rgb=hi.tolist(),levels_basis=levels_basis,contrast=args.contrast,gamma=args.gamma,warmth=args.warmth,saturation=saturation,lut_source=lut_source,automatic=finishing,crop_choice=getattr(args,'crop_source',None) or ('given' if args.crop else 'automatic' if auto else 'whole'),dust=dust_step,rotation_cw=rotate,mirror=mirror,output_dimensions=[hh,ww] if rotate in (90,270) else [ww,hh],source_dimensions=[w,h],downscale=dict(method='block average',block=stride) if stride>1 else None,channel_health=health,output=str(dest),output_sha256=sha(made[dest]),output_bits_per_channel=16 if deep else 8,input_bits_per_channel=16,lut=str(lutfile),lut_sha256=sha(made[lutfile]),raw_tiffs_modified=False,approved=False)
        if calibrated:record['density_calibration']=calibrated
        if sha(source)!=digest:raise ValueError('Source checksum changed during conversion.')
        made[recipe].write_text(json.dumps(record,indent=2)+'\n')
        made_sha={dest:record['output_sha256'],lutfile:record['lut_sha256'],recipe:sha(made[recipe])}
        for final,part in made.items():   # the picture, the LUT, then the recipe; never over a file that took a name meanwhile
            placed.append(final)   # noted first: stopped just after it took the name, it is still this run's to take away
            try:files.place(part,final)
            except FileExistsError:placed.pop();raise ValueError('Refusing to overwrite '+str(final)) from None
    except BaseException as e:
        # What was stopped can still hold the scratch file mapped, or a file open, in the frames of its traceback:
        # they are let go of first, since Windows deletes neither. (Deleted in a `finally` before, the scratch file
        # raised its own error there, which took the place of the Ctrl-C, and was left behind to block the next run.)
        traceback.clear_frames(e.__traceback__)
        # Only what is this run's goes: a name noted as placed that a stop left on someone else's file stays theirs.
        ours=[f for f in placed if files.mine(made[f],f) or (not made[f].exists() and f.exists() and sha(f)==made_sha[f])]
        for leftover in (*ours,*made.values(),scratch):
            try:leftover.unlink(missing_ok=True)
            except OSError:pass
        if isinstance(e,OSError):raise ValueError(f"couldn't write the conversion in {dest.parent}: {e.strerror or e}") from e
        raise
    return record
