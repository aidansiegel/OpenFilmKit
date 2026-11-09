"""Scene-shared density inversion and exposure-matched approximate film responses.

Published curves are viewing adaptations, not a scanner characterization. Preserve
source TIFFs; record measured film-base estimates and any assumed neutral patch.
"""
import numpy as np
CHANNELS=('Red','Green','Blue')

def density(transmission,base):
    t=np.asarray(transmission,dtype=float);b=np.asarray(base,dtype=float)
    if b.shape!=(3,) or not np.isfinite(b).all() or np.any(b<=0) or np.any(b>1):raise ValueError('Invalid film base')
    return np.log10(b/np.maximum(t,1/65535))

def matched_response(model,x,channel,pivot=.5):
    """Match log-exposure at three density anchors; keep tone shape, discard arbitrary scale."""
    x=np.asarray(x,float)
    if model['kind']=='generic':return x
    if model.get('physical_traces'):
        trace=model['physical_traces'][channel];d=np.asarray(trace['status_m_density'],float);loge=np.asarray(trace['log_exposure'],float)
        d=d-d[0]
    else:
        curve=model['curves'][channel];r=np.asarray(curve['response'],float);e=np.asarray(curve['irradiance'],float)
        # DoRF omits absolute density limits. This is an explicit viewing adaptation.
        d=-np.log10(np.maximum(1-r*(1-10**-2.5),1e-8));loge=np.log10(np.maximum(e,1e-8))
    if len(d)<3 or d.shape!=loge.shape or not np.isfinite([d,loge]).all() or np.any(np.diff(d)<0) or np.any(np.diff(loge)<0):raise ValueError('Invalid monotone profile')
    keep=np.r_[np.diff(d)>1e-9,True];d=d[keep];loge=loge[keep]
    lo,hi=d[0]+.05*(d[-1]-d[0]),d[0]+.95*(d[-1]-d[0]);mid=lo+pivot*(hi-lo)
    anchors=np.interp([lo,mid,hi],d,loge)
    if np.any(np.diff(anchors)<1e-8):raise ValueError('Degenerate profile exposure anchors')
    value=np.interp(lo+np.clip(x,0,1)*(hi-lo),d,loge)
    y=np.where(x<=pivot,pivot*(value-anchors[0])/(anchors[1]-anchors[0]),pivot+(1-pivot)*(value-anchors[1])/(anchors[2]-anchors[1]))
    # Keep out-of-range density available to the final shoulder/toe, rather than clipping it here.
    return np.where(x<0,x,np.where(x>1,x,y))

def density_lut(model,base,offsets,black,white,profile_strength=.4,mid=None,mid_target=.58):
    if model.get('film')=='slide':raise ValueError('Density calibration is for color negatives, not slide profiles')
    black=np.broadcast_to(np.asarray(black,float),(3,));white=np.broadcast_to(np.asarray(white,float),(3,))
    if not (np.isfinite([black,white]).all() and np.all(white-black>.05)):raise ValueError('Invalid scene density range')
    if mid is not None:
        mid=np.asarray(mid,float)
        if mid.shape!=(3,) or not np.isfinite(mid).all() or np.any(mid<=black+.01) or np.any(mid>=white-.01) or not 0<mid_target<1:raise ValueError('Invalid neutral density anchor')
    if not 0<=profile_strength<=1:raise ValueError('Invalid profile strength')
    offsets=np.asarray(offsets,float)
    if offsets.shape!=(3,) or not np.isfinite(offsets).all():raise ValueError('Invalid density offsets')
    t=np.arange(65536)/65535;D=density(t[:,None]*np.ones((1,3)),base)-offsets
    table=np.empty((65536,3),np.uint16)
    for c,key in enumerate(CHANNELS):
        if mid is None:x=(D[:,c]-black[c])/(white[c]-black[c]);pivot=.5
        else:
            x=np.where(D[:,c]<=mid[c],mid_target*(D[:,c]-black[c])/(mid[c]-black[c]),mid_target+(1-mid_target)*(D[:,c]-mid[c])/(white[c]-mid[c]));pivot=mid_target
        profile=matched_response(model,x,key,pivot);x=x+profile_strength*(profile-x)
        linear=np.maximum((np.exp2(np.clip(4*x,-32,24))-1)/15,0)
        # Smooth highlight shoulder with value/slope continuity; no early RGB clipping.
        linear=np.where(linear>.7,.7+.3*(-np.expm1(-np.maximum(linear-.7,0)/.3)),linear)
        srgb=np.where(linear<=.0031308,12.92*linear,1.055*linear**(1/2.4)-.055)
        table[:,c]=np.rint(np.clip(srgb,0,1)*65535).astype(np.uint16)
    return table


def render_density(raw,lut,contrast=1.1,gamma=1.,recover_clipped_blue=False,top=255):
    """Optional conservative blue estimate where scanner samples are missing; never restores raw detail.
    top: 255 for 8-bit output, 65535 for 16-bit."""
    a=np.stack([lut[raw[:,:,c],c] for c in range(3)],2).astype(float)/65535
    if recover_clipped_blue:
        blend=np.clip((64-raw[:,:,2].astype(float))/56,0,1)
        linear=np.where(a<=.04045,a/12.92,((a+.055)/1.055)**2.4)
        estimate=(linear[:,:,0]+linear[:,:,1])/2
        linear[:,:,2]=linear[:,:,2]*(1-blend)+estimate*blend
        a=np.where(linear<=.0031308,linear*12.92,1.055*np.maximum(linear,0)**(1/2.4)-.055)
    L=np.sum(a*np.array([.2126,.7152,.0722]),2,keepdims=True);v=np.clip(L,0,1)
    up=v**contrast;down=(1-v)**contrast;target=(up/(up+down))**(1/gamma)
    return np.rint(np.clip(a*target/np.maximum(L,1e-10),0,1)*top).astype(np.uint8 if top==255 else np.uint16)
