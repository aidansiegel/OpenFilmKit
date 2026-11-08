"""Build reviewable approximate negative response profiles from published source data."""
from pathlib import Path
import gzip,hashlib,json
import numpy as np
B=Path(__file__).resolve().parent;OUT=B.parent/'openfilmkit'/'data'/'profiles';OUT.mkdir(exist_ok=True)
def write_profile(ident,doc):
    """Profiles ship as compact gzipped JSON; mtime=0 keeps the file deterministic."""
    (OUT/(ident+'.json.gz')).write_bytes(gzip.compress(json.dumps(doc,separators=(',',':')).encode(),9,mtime=0))
SRC=B/'research'/'sources'/'dorfCurves.txt'  # fetched by research/fetch_sources.py
DORF='https://www.cs.columbia.edu/CAVE/software/softlib/dorf.php'
s=SRC.read_text().splitlines();database={s[i]:{'name':s[i],'original_format':s[i+1],'irradiance':np.fromstring(s[i+3],sep=' ').tolist(),'response':np.fromstring(s[i+5],sep=' ').tolist()} for i in range(0,len(s)-5,6)}
selected={
'agfa-hdc100':'agfacolor-hdc-100-plus','agfa-hdc200':'agfacolor-hdc-200-plus','agfa-hdc400':'agfacolor-hdc-400-plus',
'agfa-vista100':'agfacolor-vista-100','agfa-vista200':'agfacolor-vista-200','agfa-vista400':'agfacolor-vista-400','agfa-vista800':'agfacolor-vista-800',
'agfa-futura100':'agfacolor-futura-100','agfa-futura200':'agfacolor-futura-200','agfa-futura400':'agfacolor-futura-400',
'kodak-gold100':'Gold-100','kodak-gold200':'Gold-200','kodak-max800':'Max-Zoom-800',
'kodak-portra160nc':'Portra-160NC','kodak-portra400nc':'Portra-400NC','kodak-portra800':'Portra-800',
'kodak-advantix100':'Advantix-100','kodak-advantix200':'Advantix-200','kodak-advantix400':'Advantix-400'}
labels={'agfa-hdc100':'Agfa HDC Plus 100','agfa-hdc200':'Agfa HDC Plus 200','agfa-hdc400':'Agfa HDC Plus 400','kodak-max800':'Kodak Max Zoom 800'}
for ident,prefix in selected.items():
 curves={key:database[prefix+'CD'+key] for key in ['Red','Green','Blue']}
 assert all(c['original_format']=='graph-log-log-neg' for c in curves.values())
 doc=dict(id=ident,name=labels.get(ident,prefix.replace('agfacolor-','Agfa ').replace('-',' ')),kind='normalized-negative-response',calibration='Approximate viewing adaptation; not a calibrated Plustek scanner/film characterization.',source=DORF,paper='https://www.cs.columbia.edu/CAVE/publications/pdfs/Grossberg_PAMI04.pdf',database_sha256=hashlib.sha256(SRC.read_bytes()).hexdigest(),source_label=prefix,source_revision='DoRF historical stock version; exact emulsion generation not documented',curves=curves)
 write_profile(ident,doc)
print('Built',len(selected),'DoRF negative profiles')
