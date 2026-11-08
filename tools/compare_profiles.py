"""Score the kit's conversions of a negative or slide against the lab print of the same frame.

A print is the same frame rendered independently by a lab, so it is a reference that needs no new film.
Prints fade and labs made their own corrections: judge the pattern across many frames, never one.

  python tools/compare_profiles.py score pairs.json --out DIR          compare conversions with each pair's print
  python tools/compare_profiles.py pairs --plans DIR --negatives DIR --prints DIR --out pairs.json
                                                             build pairs from negative-publication plans

pairs.json lists {"negative": RGB16 TIFF, "print": print scan, "stock"?: id, "roll"?: roll folder, "crop"?: [l,t,r,b],
"label"?: text}. Each conversion is registered to its print (SIFT features, RANSAC homography, mirror tried),
then compared where both are valid. Lightness is matched first, so the scores are about color:

  dE      mean CIEDE2000 difference from the print
  dE_bal  the same after one overall color balance (a gain per channel). What remains is color that changes
          with brightness: what a film profile is meant to fix and a white-balance tweak cannot.
  gray    mean chroma of the conversion where the print is neutral, after that balance. Lower is better.
  dL      mean lightness difference after a single exposure match: tone, which the color scores leave out.

Needs OpenCV and scikit-image, which the kit itself does not. Ported from the author's earlier film-scan tool.
"""
from pathlib import Path
import argparse,hashlib,json,math,os,re,sys
import numpy as np
import cv2
from PIL import Image,ImageDraw,ImageOps
from skimage.color import deltaE_ciede2000,rgb2lab
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from openfilmkit import roll as rolls
from openfilmkit.films import convert as film_convert,stocks as film_stocks
Image.MAX_IMAGE_PIXELS=None
cv2.setNumThreads(1)
WORK=1200  # longest side of the working images

def convert(raw,model,film,lo,hi,contrast):
    lut=film_convert.slide_lut(model,lo,hi) if film=='slide' else film_convert.make_lut(model,lo,hi)
    return film_convert.render(raw,lut,contrast,1.0,0.0)

def features(gray):
    points,desc=cv2.SIFT_create(2000).detectAndCompute(cv2.createCLAHE(2,(8,8)).apply(gray),None)
    if desc is None or len(points)<12:return None
    return np.float32([p.pt for p in points]),np.sqrt(desc/(desc.sum(1,keepdims=True)+1e-7)).astype(np.float32)

def register(reference,image):
    """Homography taking `image` onto `reference` (both RGB uint8), trying it mirrored; None if unconvincing."""
    rf=features(cv2.cvtColor(reference,cv2.COLOR_RGB2GRAY));best=None
    for mirror in (False,True):
        source=image[:,::-1] if mirror else image;sf=features(cv2.cvtColor(np.ascontiguousarray(source),cv2.COLOR_RGB2GRAY))
        if rf is None or sf is None:continue
        good=[p[0] for p in cv2.BFMatcher().knnMatch(sf[1],rf[1],k=2) if len(p)==2 and p[0].distance<.75*p[1].distance]
        if len(good)<12:continue
        matrix,mask=cv2.findHomography(sf[0][[g.queryIdx for g in good]],rf[0][[g.trainIdx for g in good]],cv2.RANSAC,3)
        if matrix is None or best and mask.sum()<=best['inliers']:continue
        best=dict(matrix=matrix,mirror=mirror,inliers=int(mask.sum()),inlier_ratio=round(float(mask.mean()),3))
    if best is None or best['inliers']<40 or best['inlier_ratio']<.3:return None
    return best

def linear(srgb):
    return np.where(srgb<=.04045,srgb/12.92,((srgb+.055)/1.055)**2.4)

def encode(lin):
    lin=np.clip(lin,0,1);return np.where(lin<=.0031308,12.92*lin,1.055*lin**(1/2.4)-.055)

def score(conversion,target,valid):
    """Color agreement of a conversion with the registered print over `valid` pixels (see module notes)."""
    c,t=conversion[valid].astype(float)/255,target[valid].astype(float)/255;cl,tl=linear(c),linear(t)
    # Balance: one gain per channel, fitted on mid-tones where neither image is clipped.
    mid=(c.min(1)>.08)&(c.max(1)<.92)&(t.min(1)>.08)&(t.max(1)<.92)
    gains=np.median(tl[mid]/np.maximum(cl[mid],1e-4),axis=0) if mid.sum()>50 else np.ones(3)
    y=lambda a:a@np.array([.2126,.7152,.0722]);exposure=np.median(y(tl[mid]))/max(np.median(y(cl[mid])),1e-4) if mid.sum()>50 else 1.
    out={'dL':round(float(np.abs(rgb2lab(encode(cl*exposure)[None])[0][:,0]-rgb2lab(t[None])[0][:,0]).mean()),3)}
    for name,rgb in (('dE',c),('dE_bal',encode(cl*gains))):
        lab=rgb2lab(rgb[None])[0];ref=rgb2lab(t[None])[0]
        # Match lightness with a quantile map, so contrast and exposure differences do not count as color.
        q=np.linspace(0,100,257);lab[:,0]=np.interp(lab[:,0],np.percentile(lab[:,0],q),np.percentile(ref[:,0],q))
        out[name]=round(float(deltaE_ciede2000(lab,ref).mean()),3)
        if name=='dE_bal':
            neutral=(np.hypot(ref[:,1],ref[:,2])<6)&(ref[:,0]>15)&(ref[:,0]<92)
            out['gray']=round(float(np.hypot(lab[neutral,1],lab[neutral,2]).mean()),3) if neutral.sum()>200 else None
    out['balance_gains']=[round(float(g),3) for g in gains];return out

def variants(chosen,root,extra):
    names=['generic',chosen['profile_id'],*chosen.get('candidate_profiles',[]),*extra]
    out=[]
    for n in names:
        if n in out:continue
        m=film_stocks.profile(n,root)
        if m['kind']=='generic' or m.get('film','color-negative')==chosen['film']:out.append(n)
    return out

def score_pairs(args):
    pairs=json.loads(Path(args.pairs).read_text());out=Path(args.out);out.mkdir(parents=True,exist_ok=True);(out/'views').mkdir(exist_ok=True)
    extra=[p for p in (args.profiles or '').split(',') if p];results=[]
    for i,pair in enumerate(pairs):
        label=pair.get('label') or Path(pair['negative']).stem;entry=dict(label=label,negative=pair['negative'],print=pair['print'])
        try:
            meta=film_convert.capture_metadata(pair['negative']);saved=(rolls.load(pair['roll']).get('film_choice') or {}) if pair.get('roll') else {}
            chosen=film_stocks.selection(pair.get('stock'),saved,meta,None,args.root)
            if meta.get('film')=='slide' and chosen['stock_id']=='unknown':chosen=film_stocks.selection('unknown-slide',None,{},None,args.root)
            raw=film_convert.raw_open(pair['negative']);h,w=raw.shape[:2];x0,y0,x1,y1=pair.get('crop') or [0,0,w,h];area=raw[y0:y1,x0:x1]
            lo,hi=film_convert.fit_levels(area);stride=max(1,math.ceil(max(area.shape[:2])/WORK));view=np.asarray(area[::stride,::stride])
            film=chosen['film'];contrast=args.contrast if args.contrast is not None else (1.0 if film=='slide' else 1.3)
            names=variants(chosen,args.root,extra);images={n:convert(view,film_stocks.profile(n,args.root),film,lo,hi,contrast) for n in names}
            with Image.open(pair['print']) as im:p=ImageOps.exif_transpose(im).convert('RGB');p.thumbnail((1600,1600));printed=np.asarray(p)
            reg=register(images['generic'],printed)
            if reg is None:raise ValueError('print could not be registered to the negative')
            src=np.ascontiguousarray(printed[:,::-1] if reg['mirror'] else printed);hh,ww=images['generic'].shape[:2]
            target=cv2.warpPerspective(src,reg['matrix'],(ww,hh));valid=cv2.warpPerspective(np.full(src.shape[:2],255,np.uint8),reg['matrix'],(ww,hh))==255
            valid[:int(hh*.06)]=valid[int(hh*.94):]=False;valid[:,:int(ww*.06)]=valid[:,int(ww*.94):]=False
            valid=cv2.erode(valid.astype(np.uint8),np.ones((9,9),np.uint8)).astype(bool)
            if valid.mean()<.3:raise ValueError('too little of the print overlaps the negative')
            entry.update(stock=chosen['stock_id'],film=film,contrast=contrast,registration=dict(inliers=reg['inliers'],inlier_ratio=reg['inlier_ratio'],mirror=reg['mirror'],overlap=round(float(valid.mean()),3)),
                         scores={n:score(images[n],target,valid) for n in names})
            sheet(out/'views'/f'{i:03d}-{re.sub(r"[^A-Za-z0-9._-]+","_",label)}.jpg',target,valid,images)
        except Exception as e:entry['error']=str(e)
        results.append(entry);print(label,'→',entry.get('error') or {n:s['dE_bal'] for n,s in entry['scores'].items()},flush=True)
    (out/'results.json').write_text(json.dumps(results,indent=1)+'\n');summary=summarize(results);(out/'summary.txt').write_text(summary);print(summary)

def sheet(path,target,valid,images,height=360):
    """Print (where it overlaps) beside each conversion, for looking at as well as measuring."""
    tiles=[('print',np.where(valid[...,None],target,target//3))]+list(images.items());row=[]
    for name,img in tiles:
        im=Image.fromarray(img);im=im.resize((max(1,round(im.width*height/im.height)),height));ImageDraw.Draw(im).text((6,6),name,fill='white',stroke_width=2,stroke_fill='black');row.append(im)
    out=Image.new('RGB',(sum(i.width for i in row)+4*len(row),height),'white');x=0
    for im in row:out.paste(im,(x,0));x+=im.width+4
    out.save(path,quality=90)

def summarize(results):
    ok=[r for r in results if 'scores' in r];names=sorted({n for r in ok for n in r['scores']},key=lambda n:(n!='generic',n))
    lines=[f'{len(ok)} of {len(results)} pairs scored. Medians; "beats generic" counts frames where the profile is lower.','',f"{'conversion':32s} {'frames':>6s} {'dE':>6s} {'dE_bal':>7s} {'gray':>6s} {'dL':>6s} {'beats generic (dE_bal, gray, dL)':>34s}"]
    for n in names:
        rows=[r for r in ok if n in r['scores']];med=lambda k:np.median([r['scores'][n][k] for r in rows if r['scores'][n][k] is not None] or [np.nan])
        both=[r for r in rows if 'generic' in r['scores']]
        beat=lambda k:sum(r['scores'][n][k] is not None and r['scores']['generic'][k] is not None and r['scores'][n][k]<r['scores']['generic'][k] for r in both)
        wins='' if n=='generic' else f"{beat('dE_bal')}/{len(both)}, {beat('gray')}/{len(both)}, {beat('dL')}/{len(both)}"
        lines.append(f"{n:32s} {len(rows):6d} {med('dE'):6.2f} {med('dE_bal'):7.2f} {med('gray'):6.2f} {med('dL'):6.2f} {wins:>34s}")
    failed=[r for r in results if 'error' in r]
    if failed:lines+=['','Not scored:']+[f"  {r['label']}: {r['error']}" for r in failed]
    return '\n'.join(lines)+'\n'

def sha(path):
    with open(path,'rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def build_pairs(args):
    """Pairs from negative-publication plan.json files: the original negative and the print it was matched to."""
    def index(root,suffixes):
        found={}
        for d,_,files in os.walk(root):
            for f in files:
                if f.lower().endswith(suffixes):found.setdefault(f,[]).append(os.path.join(d,f))
        return found
    negatives=index(args.negatives,('.tif','.tiff'));prints=index(args.prints,('.jpg','.jpeg','.png','.tif','.tiff'));pairs=[];missing=[];wanted=set()
    for plan in sorted(Path(args.plans).rglob('plan.json')):
        for r in (json.loads(plan.read_text()) or {}).get('rescans',[]):
            name=r['original']['name'];neg=[p for p in negatives.get(name,[]) if os.path.getsize(p)==r['original']['bytes']]
            if args.verify:neg=[p for p in neg if sha(p)==r['original']['sha256']]
            # Prefer the original print scan: the cleaned print may also have had faded color restored.
            original=next((f for f in r.get('original_files',[]) if f.endswith('-oriented.jpg')),None)
            pr=[(p,'original print scan') for p in prints.get(original,[])] if original else []
            pr=pr or [(p,'cleaned print') for p in prints.get(r['print_file'],[]) if not args.verify or sha(p)==r['print_sha256']]
            if not pr:wanted.add(original or r['print_file'])
            if not neg or not pr:missing.append(f"{plan.parent.name} {r['pid']}: {'negative '+name if not neg else ''} {'print '+(original or r['print_file']) if not pr else ''}".strip());continue
            roll=re.search(r'R0*(\d+)',name,re.I)
            pairs.append(dict(label=f"{plan.parent.name}-{r['pid']}",negative=neg[0],print=pr[0][0],print_kind=pr[0][1],**({'roll':int(roll[1])} if roll else {})))
    Path(args.out).write_text(json.dumps(pairs,indent=1)+'\n')
    if wanted:Path(args.out).with_suffix('.prints-needed.txt').write_text(''.join(f+'\n' for f in sorted(wanted)))
    print(f'{len(pairs)} pairs written to {args.out}; {len(missing)} not found locally'+(f'; print names to fetch in {Path(args.out).with_suffix(".prints-needed.txt")}' if wanted else ''))
    for m in missing[:20]:print('  missing',m)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter);sub=parser.add_subparsers(dest='action',required=True)
    s=sub.add_parser('score');s.add_argument('pairs');s.add_argument('--out',required=True);s.add_argument('--root',type=Path,default=film_stocks.DATA,help='data folder whose stocks and profiles to use')
    s.add_argument('--profiles',help='Extra profiles to compare, comma-separated');s.add_argument('--contrast',type=float,help='Default 1.3 for negatives, 1.0 for slides')
    b=sub.add_parser('pairs');b.add_argument('--plans',required=True);b.add_argument('--negatives',required=True);b.add_argument('--prints',required=True);b.add_argument('--out',required=True)
    b.add_argument('--verify',action='store_true',help='Also check SHA-256 of every file (slow for large TIFFs)')
    args=parser.parse_args();(score_pairs if args.action=='score' else build_pairs)(args)
