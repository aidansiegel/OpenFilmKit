"""Extract characteristic-curve vectors from identified manufacturer PDF figures.

Requires PyMuPDF for development only; the runtime uses the exported JSON arrays.
Figure axes were read from the rendered source pages; B/G/R order is checked against the plot.

  python3 research/digitize_datasheets.py                  rebuild every data-sheet profile
  python3 research/digitize_datasheets.py --find FILE PAGE  list chart frames and tick labels on a page
  python3 research/digitize_datasheets.py --check DIR       write source-page overlays of every trace
"""
from pathlib import Path
import gzip,hashlib,itertools,json,re,sys
import numpy as np
import pymupdf
B=Path(__file__).resolve().parent;OUT=B.parents[1]/'openfilmkit'/'data'/'profiles'
sys.path.insert(0,str(B.parents[1]))
from openfilmkit.films import stocks as film_stocks
SRC=B/'sources'  # fetched by fetch_sources.py
CATALOG=json.loads((B/'datasheets.json').read_text())
URLS={s['file']:s['url'] for s in CATALOG['sources']}
CHANNELS=['Blue','Green','Red']

def bezier(pts):
    t=np.linspace(0,1,33)[:,None]
    if len(pts)==2:return pts[0]*(1-t)+pts[1]*t
    return (1-t)**3*pts[0]+3*(1-t)**2*t*pts[1]+3*(1-t)*t*t*pts[2]+t**3*pts[3]

def listed_groups(page,s):
    """Original method: named drawings whose consecutive items form the three traces."""
    groups=[]
    for n in s['drawings']:
        for item in page.get_drawings()[n]['items']:
            if item[0] not in ('l','c'):continue
            points=np.array(item[1:],float)
            if not groups or np.linalg.norm(groups[-1][-1][-1]-points[0])>.1:groups.append([])
            groups[-1].append(points)
    assert len(groups)==3
    # The source plots label the upper trace B, middle G, lower R.
    return sorted(groups,key=lambda g:g[-1][-1][1])

def style(d):
    """Colour and dash shape of a stroked path: how a legend tells otherwise identical curves apart."""
    dashes=re.findall(r'[\d.]+',(d.get('dashes') or '').split(']')[0])
    return tuple(round(v,1) for v in d['color'])+(len(dashes),)

def frame_segments(page,rect,margin=1.5,styled=False,callouts=False):
    """Stroked line/curve pieces inside the plot frame, without grid lines, the frame or tick marks."""
    x0,y0,x1,y1=rect;out=[]
    labels=[pymupdf.Rect(w[:4])+(-5,-5,5,5) for w in page.get_text('words') if re.fullmatch(r'[BGR],?|Blue|Green|Red',w[4])] if callouts else []
    for d in page.get_drawings():
        r=d['rect']
        if d.get('color') is None or r.x0<x0-margin or r.x1>x1+margin or r.y0<y0-margin or r.y1>y1+margin:continue
        for item in d['items']:
            if item[0] not in ('l','c'):continue
            pts=np.array([[p.x,p.y] for p in item[1:]],float);dx,dy=np.ptp(pts[:,0]),np.ptp(pts[:,1])
            # On slide charts, a long straight line ending at a printed channel label is a callout, not part of a curve.
            if item[0]=='l' and np.hypot(dx,dy)>6 and any(lab.contains(pt) for pt in item[1:] for lab in labels):continue
            # Grid lines, the frame and tick marks are straight and run into a frame edge; flat curve toes start inside it.
            if item[0]=='l' and dy<.05 and (dx>.8*(x1-x0) or min(abs(pts[:,0]-x0).min(),abs(pts[:,0]-x1).min())<2):continue
            if item[0]=='l' and dx<.05 and (dy>.8*(y1-y0) or min(abs(pts[:,1]-y0).min(),abs(pts[:,1]-y1).min())<2):continue
            pts=pts if pts[0,0]<=pts[-1,0] else pts[::-1];out.append((pts,style(d)) if styled else pts)
    return out

def chain(segments,gap):
    groups=[]
    for s in sorted(segments,key=lambda s:s[0,0]):
        ends=[(np.linalg.norm(g[-1][-1]-s[0]),g) for g in groups]
        near=min(ends,key=lambda e:e[0],default=(gap,None))
        (near[1].append(s) if near[0]<gap else groups.append([s]))
    return groups

def located_groups(page,s):
    """Chain every stroked piece in the frame; the three widest chains are the B/G/R traces."""
    x0,y0,x1,y1=s['rect'];styles=[]
    if s.get('film')=='slide':
        # Slide curves meet near clear film; join pieces only within one line style so traces cannot swap there.
        pieces=frame_segments(page,s['rect'],styled=True,callouts=True);groups=[]
        for st in sorted({st for _,st in pieces}):
            for g in chain([q for q,t in pieces if t==st],s.get('join_gap',1.)):groups.append(g);styles.append(st)
    else:groups=chain(frame_segments(page,s['rect']),s.get('join_gap',1.));styles=[None]*len(groups)
    wide=[np.ptp(np.vstack([bezier(p) for p in g])[:,0])>s.get('min_width',.4)*(x1-x0) for g in groups]
    groups=[g for g,w in zip(groups,wide) if w];styles=[t for t,w in zip(styles,wide) if w]
    if len(groups)!=3:raise ValueError(f"{s['id']}: found {len(groups)} wide traces, expected 3")
    xy=[np.vstack([bezier(p) for p in g]) for g in groups];xy=[a[np.argsort(a[:,0])] for a in xy]
    lo,hi=max(a[0,0] for a in xy),min(a[-1,0] for a in xy)
    if s.get('film')=='slide':return [groups[i] for i in slide_order(page,s,xy,lo,hi,styles)]
    heights=np.array([[np.interp(x,a[:,0],a[:,1]) for x in np.linspace(lo,hi,7)] for a in xy])
    order=np.argsort(heights[:,3])
    # Colour negative masks keep blue density above green above red across the whole plot.
    if not (np.diff(heights[order],axis=0)>0).all():raise ValueError(s['id']+': traces cross; channel order is ambiguous')
    labels=sorted((w[1],w[4][0]) for w in page.get_text('words') if x0<w[0]<x1 and y0<w[1]<y1 and re.fullmatch(r'B|G|R|Blue|Green|Red',w[4]))
    if len(labels)>=2 and [c for _,c in labels]!=[c for c in 'BGR' if c in {c for _,c in labels}]:raise ValueError(s['id']+': printed B/G/R labels disagree with trace order')
    return [groups[i] for i in order]

def slide_order(page,s,xy,lo,hi,styles):
    """Indices of the blue, green and red traces of a slide chart.

    Slide curves converge towards clear film, so vertical order alone is unreliable. A legend's sample lines give
    each channel's line style; otherwise each printed R/G/B label is matched to the nearest trace; or the spec gives
    `order`: the channels top to bottom where the traces spread most."""
    x0,y0,x1,y1=s['rect'];grid=np.linspace(lo,hi,41)
    heights=np.array([[np.interp(x,a[:,0],a[:,1]) for x in grid] for a in xy]);widest=int(np.argmax(np.ptp(heights,axis=0)))
    if 'order' in s:
        rank=list(np.argsort(heights[:,widest]));return [rank[s['order'].index(c)] for c in 'BGR']
    if 'dash_styles' in s:  # legend not extractable: the spec names each channel's dash shape (0 solid, 2 dashed, 4 dash-dot)
        dashes=[t[-1] for t in styles]
        if sorted(dashes)!=sorted(s['dash_styles'].values()):raise ValueError(s['id']+': traces do not match the given dash styles')
        return [dashes.index(s['dash_styles'][c]) for c in 'BGR']
    labels=[((w[0]+w[2])/2,(w[1]+w[3])/2,w[4][0]) for w in page.get_text('words') if x0<w[0]<x1 and y0<w[1]<y1 and re.fullmatch(r'B|G|R|Blue|Green|Red',w[4])]
    legend={}
    for d in page.get_drawings():
        r=d['rect']
        if d.get('color') is None or r.height>1 or not 10<r.width<40:continue
        for w in page.get_text('words'):
            if re.fullmatch(r'B|G|R|Blue|Green|Red',w[4]) and 0<w[0]-r.x1<8 and abs((w[1]+w[3])/2-r.y0)<4:legend[w[4][0]]=style(d)
    if len(legend)==3 and len(set(legend.values()))==3 and sorted(styles)==sorted(legend.values()):
        return [styles.index(legend[c]) for c in 'BGR']
    if sorted(c for *_,c in labels)!=['B','G','R']:raise ValueError(s['id']+': slide chart needs printed R, G and B labels, or an order in the spec')
    cost=lambda perm:sum(abs(np.interp(cx,xy[i][:,0],xy[i][:,1])-cy) for (cx,cy,_),i in zip(labels,perm))
    best=min(itertools.permutations(range(3)),key=cost)
    return [best[[c for *_,c in labels].index(ch)] for ch in 'BGR']

def runs(column,gap=1):
    """(first,last) row of each run of ink in a pixel column, bridging gaps narrower than `gap` pixels."""
    out=[]
    for i in np.flatnonzero(column):
        if out and i-out[-1][1]<=gap:out[-1][1]=i
        else:out.append([i,i])
    return out

def grid_lines(ink,axis,count,k=1):
    """Centres of an evenly spaced grid of `count` lines in a scanned chart.

    The spacing whose lines carry the most ink is found first; each line is then placed at the median centre of
    the thin runs crossing it, since curves passing through a grid line skew a plain coverage peak. `k` scales the
    pixel sizes for scans at other resolutions."""
    w=int(round(4*k));m=int(round(6*k))
    band=np.convolve(ink.mean(axis=axis),np.ones(3),'same')
    peaks=[i for i in range(1,len(band)-1) if band[i]>=band[i-1] and band[i]>=band[i+1] and band[i]>.5];best=None
    for i,first in enumerate(peaks):
        for last in peaks[i+1:]:
            if last-first<10*(count-1):continue
            pos=[q-w+int(np.argmax(band[q-w:q+w+1])) for q in np.rint(np.linspace(first,last,count)).astype(int)]
            if best is None or sum(band[pos])>best[0]:best=(sum(band[pos]),pos)
    lines=ink.T if axis==1 else ink;out=[]
    for q in best[1]:
        centres=[max(q-m,0)+(s+e)/2 for row in lines[:,max(q-m,0):q+m+1] for s,e in runs(row) if e-s<=2*k]
        out.append(float(np.median(centres)) if centres else float(q))
    return out

def scanned_points(page,s):
    """Follow the three curves of a scanned chart outwards from a column where all three are clear.

    A column is skipped at a vertical grid line, or where a thick blob (lettering over the curve) covers the
    predicted point. The search window never widens, so a trace cannot wander onto a label or a neighbour.
    Pixel sizes suit a ~4 px/pt scan; `scale` in the spec multiplies them for finer or coarser ones."""
    k=s.get('scale',1);line,tol,span,end_gap=7*k,2.5*k,20*k,40*k
    info=page.get_images(full=True)[s['image']];pix=pymupdf.Pixmap(page.parent,info[0]);box=page.get_image_bbox(info)
    v=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,pix.n)[...,0].astype(int);bg=np.median(v)
    # Lines are whatever differs strongly from the background: dark on white or grey, light on black.
    ink=v<bg-64 if bg>=96 else v>bg+64
    v=grid_lines(ink,0,s['grid'][0],k);h=grid_lines(ink,1,s['grid'][1],k)
    x_lo,x_hi,y_lo,y_hi=int(v[0]+3*k),int(v[-1]-2*k),int(h[0]+3*k),int(h[-1]-2*k)
    def column(x):
        if any(abs(x-q)<=2.5*k for q in v):return None
        return [(a+y_lo,b+y_lo) for a,b in runs(ink[y_lo:y_hi,x],max(1,int(k)))]
    def clear(x):
        c=column(x);return [] if c is None else [(a+b)/2 for a,b in c if b-a+1<=line and all(abs((a+b)/2-g)>3*k for g in h)]
    start=int(x_lo+.85*(x_hi-x_lo));start=next((x for d in range(int(.25*(x_hi-x_lo))) for x in (start-d,start+d) if len(clear(x))==3 and min(np.diff(clear(x)))>2*line),None)  # well apart, so a stray sliver beside a curve cannot pass for one
    if start is None:raise ValueError(s['id']+': no column with three clear traces')
    paths=[{start:y} for y in clear(start)]
    for direction in (1,-1):  # the short right-hand run first gives the long left-hand pass a slope to follow
        for path in paths:
            x=start+direction;last=start
            while x_lo<x<x_hi and abs(x-last)<=end_gap:
                c=column(x)
                if c is not None:
                    near=np.array(sorted((k,path[k]) for k in path if abs(k-last)<=span))
                    pred=np.polyval(np.polyfit(near[:,0],near[:,1],1),x) if len(near)>3 else path[last]
                    covering=[r for r in c if r[0]-1<=pred<=r[1]+1]
                    # A run no thicker than a grid line, sitting on a grid row, is the grid line itself.
                    thin=[(a+b)/2 for a,b in c if b-a+1<=line and abs((a+b)/2-pred)<=tol and not (b-a+1<=2*k and any(abs((a+b)/2-g)<=2*k for g in h))]
                    if thin and not (covering and covering[0][1]-covering[0][0]+1>line):path[x]=min(thin,key=lambda y:abs(y-pred));last=x
                x+=direction
    out=[]
    for path in paths:
        q=np.array(sorted(path.items()),float);free=np.flatnonzero([all(abs(y-g)>2*k for g in h) for y in q[:,1]])
        # A trace that ends along a grid row has run off the curve onto the grid line; cut back to the last clear point.
        out.append(q[free[0]:free[-1]+1])
    out.sort(key=lambda q:np.interp(start,q[:,0],q[:,1]))
    for upper,lower in zip(out,out[1:]):
        u,l=dict(map(tuple,upper)),dict(map(tuple,lower))
        if not all(u[x]<l[x]-3*k for x in u.keys()&l.keys()):raise ValueError(s['id']+': traces touch or cross')
    page_xy=lambda q:np.c_[box.x0+(q[:,0]+.5)/pix.width*box.width,box.y0+(q[:,1]+.5)/pix.height*box.height]
    rect=page_xy(np.array([[v[0],h[0]],[v[-1],h[-1]]])).ravel().tolist()
    return [page_xy(q) for q in out],rect

def channel_points(page,s):
    """Blue, green and red trace points in page coordinates, the plot frame, and how they were selected."""
    if 'image' in s:
        points,rect=scanned_points(page,s)
        return points,rect,dict(method='Scanned chart image traced column by column; evenly spaced grid fitted to the printed grid lines; axes and B/G/R order visually verified',pdf_image_index=s['image'],grid_lines=s['grid'],**({'pixel_scale':s['scale']} if 'scale' in s else {}))
    groups=listed_groups(page,s) if 'drawings' in s else located_groups(page,s)
    points=[np.array([xy for pts in g for xy in bezier(pts).tolist()]) for g in groups];points=[p[np.argsort(p[:,0])] for p in points]
    selection=dict(pdf_drawing_indices=s['drawings']) if 'drawings' in s else dict(trace_selection='stroked paths inside figure_rect, grid/frame/ticks excluded, joined end to end; three widest chains ordered top to bottom')
    if s.get('film')=='slide':
        selection['trace_selection']=selection['trace_selection'].replace('ordered top to bottom','assigned to channels by printed labels, legend line styles or the spec order')
        extended=[]
        for i,p in enumerate(points):
            # Some sheets stop drawing a channel where it merges with another; continue it along that curve.
            for j,q in enumerate(points):
                if i!=j and p[-1,0]<q[-1,0]-2 and abs(np.interp(p[-1,0],q[:,0],q[:,1])-p[-1,1])<1.5:
                    points[i]=p=np.vstack([p,q[q[:,0]>p[-1,0]]]);extended.append(f'{CHANNELS[i]} continued along {CHANNELS[j]} beyond the point where the printed curves merge')
                    break
        if extended:selection['merged_trace_extensions']=extended
    return points,s['rect'],dict(method='PDF vector extraction, Bezier sampling; axes and B/G/R order visually verified',**selection)

def digitize(s):
    page=pymupdf.open(SRC/s['file'])[s['page']]
    points,rect,how=channel_points(page,s)
    curves={};traces={};x0,y0,x1,y1=rect;slide=s.get('film')=='slide'
    for channel,trace in zip(CHANNELS,points):
        logH=s['log_exposure'][0]+(trace[:,0]-x0)/(x1-x0)*(s['log_exposure'][1]-s['log_exposure'][0])
        density=s['density'][0]+(y1-trace[:,1])/(y1-y0)*(s['density'][1]-s['density'][0])
        density=np.minimum.accumulate(density) if slide else np.maximum.accumulate(density)
        energy=10**logH;irr=(energy-energy[0])/(energy[-1]-energy[0])
        if slide:
            # A slide clears with exposure: transmission 10**-D rises with it, normalized like the negatives.
            t=10**-density;response=(t-t[0])/(t[-1]-t[0])
        else:
            # Same convention as the DoRF film curves: normalized opacity 10**D against normalized linear exposure.
            # (Normalized transmission instead crams the whole response below 1% exposure.)
            opacity=10**density;response=(opacity-opacity[0])/(opacity[-1]-opacity[0])
        keep=np.r_[True,(np.diff(response)>0)&(np.diff(irr)>0)]
        if not keep[-1]:keep[np.flatnonzero(keep)[-1]]=False;keep[-1]=True  # end on the (1,1) point itself
        response,irr=response[keep],irr[keep]
        # Store the traced physical coordinates and normalized arrays; no handwritten LUT values.
        curves[channel]=dict(name=s['name']+' '+channel,original_format='manufacturer-log-exposure-density',irradiance=irr.tolist(),response=response.tolist())
        traces[channel]=dict(log_exposure=logH.tolist(),density=density.tolist()) if slide else dict(log_exposure=logH.tolist(),status_m_density=density.tolist())
    method=how.pop('method')
    if slide:
        status=s.get('densitometry','Status A')
        return dict(id=s['id'],name=s['name'],kind='normalized-reversal-response',film='slide',densitometry=status,calibration=f'Approximate manufacturer-curve adaptation. {status} densities and scanner RGB are not a calibrated spectral match.',source=URLS[s['file']],source_revision=s['revision'],source_sha256=hashlib.sha256((SRC/s['file']).read_bytes()).hexdigest(),digitization=dict(method=method,pdf_page=s['page']+1,**how,figure_rect=rect,axes={'log_exposure':s['log_exposure'],'density':s['density']},normalization='T=10**-D; response=(T-Tmin)/(Tmax-Tmin), rising with exposure; irradiance normalized from 10**logH; tiny trace reversals flattened monotonically; points that do not strictly increase dropped from curves (physical_traces keep every sample)'),curves=curves,physical_traces=traces)
    return dict(id=s['id'],name=s['name'],kind='normalized-negative-response',calibration='Approximate manufacturer-curve adaptation. Status M densities and scanner RGB are not a calibrated spectral match.',source=URLS[s['file']],source_revision=s['revision'],source_sha256=hashlib.sha256((SRC/s['file']).read_bytes()).hexdigest(),digitization=dict(method=method,pdf_page=s['page']+1,**how,figure_rect=rect,axes={'log_exposure':s['log_exposure'],'density':s['density']},normalization='O=10**D; response=(O-Omin)/(Omax-Omin), the DoRF film convention; irradiance normalized from 10**logH; tiny trace reversals flattened monotonically; points that do not strictly increase dropped from curves (physical_traces keep every sample)'),curves=curves,physical_traces=traces)

def number(text):
    return float(text.replace('−','-').replace('–','-')) if re.fullmatch(r'[-−–]?\d+(\.\d+)?',text) else None

def fit_axis(labels,ticks,barred=False):
    """Value at each end of an axis from printed labels, snapped to the nearest tick mark when there is one."""
    labels=sorted(labels)
    if len(labels)<3:return None
    # Kodak prints negative log exposures with a bar over the digit, which text extraction drops.
    zero=next((i for i,(_,v) in enumerate(labels) if v==0),None)
    if barred and zero is not None:labels=[(c,-abs(v) if i<zero else v) for i,(c,v) in enumerate(labels)]
    pos=np.array([min(ticks,key=lambda t:abs(t-c)) if ticks and min(abs(t-c) for t in ticks)<4 else c for c,_ in labels]);val=np.array([v for _,v in labels])
    slope,offset=np.polyfit(pos,val,1)
    # Label centres drift (split minus signs, outlined digits). Name each tick or grid line from that rough fit,
    # to the nearest half unit, and refit through those exact positions instead.
    named=[(t,round((slope*t+offset)*2)/2) for t in ticks];named=[(t,v) for t,v in named if abs(slope*t+offset-v)<.06]  # tight enough to skip minor ticks
    if len({v for _,v in named})>=3:
        pos,val=np.array([t for t,_ in named]),np.array([v for _,v in named]);slope,offset=np.polyfit(pos,val,1)
    return slope,offset,np.abs(slope*pos+offset-val).max()

def frames(page):
    """Candidate plot frames with axis end values fitted from their tick marks and labels."""
    lines=[];boxes=[];found=[]
    for d in page.get_drawings():
        if d.get('color') is None:continue
        for it in d['items']:
            if it[0]=='re' and it[1].width>90 and it[1].height>70:boxes.append(it[1])
            # Quads list their corners in varying order, so test the corners directly for an axis-aligned box.
            if it[0]=='qu' and it[1].rect.width>90 and it[1].rect.height>70 and all(min(abs(c.x-it[1].rect.x0),abs(c.x-it[1].rect.x1))<.5 and min(abs(c.y-it[1].rect.y0),abs(c.y-it[1].rect.y1))<.5 for c in it[1]):boxes.append(it[1].rect)
            if it[0]=='l':lines.append(np.array([[p.x,p.y] for p in it[1:]]))
    flat=[l for l in lines if np.ptp(l[:,1])<.1 and np.ptp(l[:,0])>90];upright=[l for l in lines if np.ptp(l[:,0])<.1 and np.ptp(l[:,1])>70]
    for top in flat:
        for bottom in flat:
            a,b=sorted(top[:,0]),sorted(bottom[:,0])
            if bottom[0,1]-top[0,1]>70 and abs(a[0]-b[0])<1 and abs(a[1]-b[1])<1 and sum(abs(u[0,0]-x)<1 and min(u[:,1])<top[0,1]+1 and max(u[:,1])>bottom[0,1]-1 for u in upright for x in a)>=2:
                boxes.append(pymupdf.Rect(a[0],top[0,1],a[1],bottom[0,1]))
    seen=[]
    for r in boxes:
        if any(max(abs(r.x0-b.x0),abs(r.y0-b.y0),abs(r.x1-b.x1),abs(r.y1-b.y1))<1 for b in seen):continue
        seen.append(r);words=page.get_text('words')
        xs=[((w[0]+w[2])/2,number(w[4])) for w in words if number(w[4]) is not None and r.y1<w[1]<r.y1+14 and r.x0-15<(w[0]+w[2])/2<r.x1+15]
        tops=[((w[0]+w[2])/2,number(w[4])) for w in words if number(w[4]) is not None and r.y0-14<w[3]<r.y0+1 and r.x0-15<(w[0]+w[2])/2<r.x1+15]
        ys=[((w[1]+w[3])/2,number(w[4])) for w in words if number(w[4]) is not None and r.x0-30<w[2]<r.x0+1 and r.y0-8<(w[1]+w[3])/2<r.y1+8]
        # Tick marks on the bottom/left edges, and grid lines spanning the frame.
        xticks=sorted({round(l[0,0],2) for l in lines if np.ptp(l[:,0])<.1 and r.x0-1<l[0,0]<r.x1+1 and (0<np.ptp(l[:,1])<8 and abs(l[:,1]-r.y1).min()<1.5 or l[:,1].min()<r.y0+3 and l[:,1].max()>r.y1-3)})
        yticks=sorted({round(l[0,1],2) for l in lines if np.ptp(l[:,1])<.1 and r.y0-1<l[0,1]<r.y1+1 and (0<np.ptp(l[:,0])<8 and abs(l[:,0]-r.x0).min()<1.5 or l[:,0].min()<r.x0+3 and l[:,0].max()>r.x1-3)})
        out=dict(rect=[round(v,3) for v in r])
        # Kodak motion-picture sheets print log exposure along the top and camera stops along the bottom.
        for name,labels,ticks,ends in [('log_exposure',xs,xticks,(r.x0,r.x1)),('density',ys,yticks,(r.y1,r.y0)),('log_exposure_top',tops,[],(r.x0,r.x1))]:
            f=fit_axis(labels,ticks,name=='log_exposure')
            if f:out[name]=[round(f[0]*e+f[1],3) for e in ends];out[name+'_fit_error']=round(f[2],4)
        found.append(out)
    return found

def find(file,page_number):
    for frame in frames(pymupdf.open(file)[page_number]):print(json.dumps(frame))

def check(outdir):
    """Draw each stored trace back onto its source page, with the spec's axis grid, so a person can compare them.

    The dotted grid lines must land on the printed tick marks; otherwise the axes in datasheets.json are wrong."""
    outdir=Path(outdir);outdir.mkdir(exist_ok=True)
    for s in CATALOG['profiles']:
        doc=film_stocks.profile(s['id']);page=pymupdf.open(SRC/s['file'])[s['page']]
        x0,y0,x1,y1=doc['digitization']['figure_rect'];(e0,e1),(d0,d1)=doc['digitization']['axes']['log_exposure'],doc['digitization']['axes']['density']
        for v in np.arange(np.ceil(e0),e1+1e-9):x=x0+(v-e0)/(e1-e0)*(x1-x0);page.draw_line((x,y0),(x,y1+6),color=(1,0,1),width=.4,dashes='[1 1] 0')
        for v in np.arange(np.ceil(d0*2)/2,d1+1e-9,.5):y=y1-(v-d0)/(d1-d0)*(y1-y0);page.draw_line((x0-6,y),(x1,y),color=(1,0,1),width=.4,dashes='[1 1] 0')
        for channel,colour in zip(CHANNELS,[(0,0,1),(0,.6,0),(1,0,0)]):
            t=doc['physical_traces'][channel];L,D=np.array(t['log_exposure']),np.array(t.get('status_m_density',t.get('density')))
            x=x0+(L-e0)/(e1-e0)*(x1-x0);y=y1-(D-d0)/(d1-d0)*(y1-y0)
            page.draw_polyline(list(zip(x,y)),color=colour,width=.8)
        page.draw_rect(pymupdf.Rect(x0,y0,x1,y1),color=(1,.5,0),width=.6)
        page.get_pixmap(dpi=150,clip=pymupdf.Rect(x0-40,y0-12,x1+12,y1+24)).save(outdir/(s['id']+'.png'))
    print('Overlays in',outdir)

if __name__=='__main__':
    if sys.argv[1:2]==['--find']:find(sys.argv[2],int(sys.argv[3]))
    elif sys.argv[1:2]==['--check']:check(sys.argv[2])
    else:
        for s in CATALOG['profiles']:
            (OUT/(s['id']+'.json.gz')).write_bytes(gzip.compress(json.dumps(digitize(s),separators=(',',':')).encode(),9,mtime=0))
            print('Digitized',s['id'])
