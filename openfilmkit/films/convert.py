"""Stock-aware viewing copies from linear RGB16 negatives and slides; masters stay unchanged.

Reads the kit's own masters and the raw scans SilverFast (48-bit HDR, 64-bit HDRi) and VueScan (48-bit raw,
64-bit RGBI raw) save, so scans made with those on any system convert the same way.

Ported from the author's earlier film-scan tool (film_convert.py). The LUTs and rendering are unchanged, so output
matches film-scan byte for byte; capture records may be film-scan's or the kit's, and roll
choices come from the roll folder's roll.json.
"""
from pathlib import Path
import datetime,hashlib,json,math,struct
import numpy as np
import tifffile
from PIL import Image,ImageCms
from . import density as film_density, stocks as film_stocks
from .. import roll as rolls
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
            page=next((p for p in tif.pages if not p.subfiletype&5),None)  # 1: reduced-size copy, 4: mask/infrared
            if page is None:raise ValueError('TIFF has no full-size image.')
            spp,h,w=page.samplesperpixel,page.imagelength,page.imagewidth
            if page.photometric!=tifffile.PHOTOMETRIC.RGB or page.bitspersample!=16 or page.sampleformat!=1 or spp not in (3,4):
                raise ValueError(f'Conversion needs a linear RGB scan at 16 bits per channel (48-bit, or 64-bit with infrared); '
                                 f'this TIFF is {page.photometric.name} with {spp} x {page.bitspersample}-bit samples.')
            planar=page.planarconfig==tifffile.PLANARCONFIG.SEPARATE;shape=(spp,h,w) if planar else (h,w,spp)
            if page.compression==tifffile.COMPRESSION.NONE and page.is_contiguous:
                offset=page.dataoffsets[0]
                if offset+h*w*spp*2>Path(path).stat().st_size:raise ValueError('TIFF pixel data is truncated.')
                a=np.memmap(path,dtype=tif.byteorder+'u2',mode='r',offset=offset,shape=shape)
            else:a=page.asarray().reshape(shape)
    except tifffile.TiffFileError as e:raise ValueError(f'Not a readable TIFF: {e}') from e
    except KeyError as e:raise ValueError(f'Unsupported TIFF compression: {e}') from e
    return np.moveaxis(a[:3],0,-1) if planar else a[:,:,:3]

OUTPUTS=('.jpg','.jpeg','.png','.tif','.tiff')

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

def render(raw,lut,contrast=1.3,gamma=1.0,warmth=0.0,top=255):
    """Viewing pixels from raw RGB16 through the LUT; top: 255 for 8-bit output, 65535 for 16-bit."""
    if not (.1<=contrast<=4 and .1<=gamma<=4 and -1<=warmth<=1):raise ValueError('Contrast/gamma must be 0.1–4; warmth must be -1–1.')
    a=np.stack([lut[raw[:,:,c],c] for c in range(3)],2).astype(float)/65535
    weights=np.array([.2126,.7152,.0722]);L=np.sum(a*weights,2,keepdims=True)
    if warmth:
        strength=np.clip((.98-L)/.18,0,1);a*=1+strength*(np.array([.08,0,-.12])*warmth)
        a*=L/np.maximum(np.sum(a*weights,2,keepdims=True),1e-10)
    v=np.clip(L,0,1);up=v**contrast;down=(1-v)**contrast;target=(up/(up+down))**(1/gamma)
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
    saved_roll=(rolls.load(roll_dir).get('film_choice') or {}) if roll_dir else {}
    if meta.get('film')=='bw-negative':raise ValueError('This converter handles color negatives and slides; capture metadata identifies a black-and-white negative.')
    chosen=film_stocks.selection(args.stock,saved_roll,meta,args.profile,root,roll=roll_dir.name if roll_dir else None)
    if meta.get('film')=='slide' and chosen['stock_id']=='unknown':chosen=film_stocks.selection('unknown-slide',None,{},args.profile,root)
    film=chosen['film'];model=film_stocks.profile(chosen['profile_id'],root)
    if meta.get('film') in ('slide','color-negative') and meta['film']!=film:raise ValueError(f"Capture metadata says {meta['film']}, but the selected stock is a {film}.")
    if model['kind']!='generic' and model.get('film','color-negative')!=film:raise ValueError(f"Profile {model['id']} is for {model.get('film','color-negative')} film, not {film}.")
    if args.contrast is None:args.contrast=1.0 if film=='slide' else 1.3  # a slide already carries its own contrast
    if not (.1<=args.contrast<=4 and .1<=args.gamma<=4 and -1<=args.warmth<=1):raise ValueError('Contrast/gamma must be 0.1–4; warmth must be -1–1.')
    digest=sha(source)
    if meta.get('sha256') and meta['sha256']!=digest:raise ValueError('Source TIFF does not match its capture checksum.')
    raw=raw_open(source);h,w=raw.shape[:2];box=args.crop or [0,0,w,h];x0,y0,x1,y1=box
    if not (0<=x0<x1<=w and 0<=y0<y1<=h):raise ValueError('Crop lies outside the source TIFF.')
    area=raw[y0:y1,x0:x1]
    if args.levels_from:
        prior_path=Path(args.levels_from).expanduser().resolve();prior=film_stocks.read(prior_path)
        if prior['film_selection']['profile_id']!=chosen['profile_id'] or prior['film_selection']['stock_id']!=chosen['stock_id']:
            raise ValueError('Shared levels must use the same stock and response profile.')
        lo,hi=validate_levels(prior['transmission_low_rgb'],prior['transmission_high_rgb']);levels_basis=dict(type='shared',source=str(prior_path),sha256=sha(prior_path))
    else:
        lo,hi=fit_levels(area);levels_basis=dict(type='per-frame',percentiles=[.2,99.8],note='May vary between frames; use --levels-from for a shared starting correction.')
    calibrated=None
    calibration_path=getattr(args,'calibration',None) or saved_roll.get('calibration_file')
    if calibration_path:
        if film=='slide':raise ValueError('Density calibration plans are for color negatives.')
        if args.levels_from:raise ValueError('Choose scene calibration or shared transmission endpoints, not both.')
        plan_path=Path(calibration_path).expanduser().resolve();plan=film_stocks.read(plan_path)
        scenes=plan['scenes'];scene_name=getattr(args,'scene',None) or plan.get('capture_scenes',{}).get(digest)
        if scene_name is None and len(scenes)!=1:raise ValueError('--scene is required for a calibration with several scenes.')
        matches=[r for r in scenes if scene_name is None or r['scene']==scene_name]
        if len(matches)!=1:raise ValueError('Unknown or duplicate calibration scene.')
        scene=matches[0]
        lut=film_density.density_lut(model,plan['film_base_transmission_rgb'],scene['density_offsets_rgb'],scene['density_black'],scene['density_white'],profile_strength=plan.get('profile_strength',.4),mid=scene.get('density_mid'),mid_target=scene.get('density_mid_target',.58))
        calibrated=dict(plan=str(plan_path),plan_sha256=sha(plan_path),scene=scene,film_base_transmission_rgb=plan['film_base_transmission_rgb'],recover_clipped_blue=bool(saved_roll.get('recover_clipped_blue',False) if getattr(args,'recover_clipped_blue',None) is None else args.recover_clipped_blue),note='Measured viewing correction; missing blue estimates are not restored capture detail.')
    else:lut=slide_lut(model,lo,hi) if film=='slide' else make_lut(model,lo,hi)
    stride=1 if args.max_side==0 else max(1,math.ceil(max(area.shape[:2])/args.max_side));view=area[::stride,::stride];hh,ww=view.shape[:2]
    dest.parent.mkdir(parents=True,exist_ok=True)
    pixels=np.memmap(scratch,mode='w+',dtype='uint16' if deep else 'uint8',shape=(hh,ww,3))
    try:
        for start in range(0,hh,64):
            if calibrated:
                if args.warmth:raise ValueError('Use neutral scene anchors instead of warmth with density calibration.')
                pixels[start:start+64]=film_density.render_density(view[start:start+64],lut,args.contrast,args.gamma,calibrated['recover_clipped_blue'],top)
            else:pixels[start:start+64]=render(view[start:start+64],lut,args.contrast,args.gamma,args.warmth,top)
        pixels.flush();icc=srgb_icc()
        if deep:write_tiff(dest,pixels,icc)
        else:
            im=Image.fromarray(pixels);im.save(dest,icc_profile=icc,**({'quality':95,'subsampling':0} if dest.suffix.lower()!='.png' else {}));del im
    finally:
        del pixels;scratch.unlink(missing_ok=True)
    np.save(lutfile,lut)
    record=dict(created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),status='Starting conversion — requires visual review',source=str(source),source_sha256=digest,source_capture_metadata_sha256=sha(str(source)+'.json') if Path(str(source)+'.json').exists() else None,film_selection=chosen,film=film,profile_sha256=film_stocks.profile_sha256(chosen['profile_id'],root),profile_source=model.get('source'),raw_crop_pixels=box,transmission_low_rgb=lo.tolist(),transmission_high_rgb=hi.tolist(),levels_basis=levels_basis,contrast=args.contrast,gamma=args.gamma,warmth=args.warmth,output_dimensions=[ww,hh],source_dimensions=[w,h],output=str(dest),output_sha256=sha(dest),output_bits_per_channel=16 if deep else 8,input_bits_per_channel=16,lut=str(lutfile),lut_sha256=sha(lutfile),raw_tiffs_modified=False,approved=False,generative_processing=False)
    if calibrated:record['density_calibration']=calibrated
    if sha(source)!=digest:raise RuntimeError('Source checksum changed during conversion.')
    recipe.write_text(json.dumps(record,indent=2)+'\n')
    return record
