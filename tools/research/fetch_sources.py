"""Download the published source data the profiles are built from and verify it byte-for-byte.

Files land in research/sources/ (not committed). Hashes are the ones recorded in profiles/*.json,
so a rebuild from these files reproduces the published profiles.
"""
from pathlib import Path
import hashlib,io,json,urllib.request,zipfile
OUT=Path(__file__).resolve().parent/'sources'
SOURCES=json.loads((Path(__file__).resolve().parent/'datasheets.json').read_text())['sources']

def fetch(s):
    dest=OUT/s['file']
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest()==s['sha256']:return 'present'
    with urllib.request.urlopen(urllib.request.Request(s['url'],headers={'User-Agent':'openfilmkit/1.0'}),timeout=60) as r:data=r.read()
    if 'member' in s:data=zipfile.ZipFile(io.BytesIO(data)).read(s['member'])
    digest=hashlib.sha256(data).hexdigest()
    if digest!=s['sha256']:raise SystemExit(f"{s['file']}: SHA-256 {digest} does not match the recorded {s['sha256']}; the source has changed.")
    OUT.mkdir(exist_ok=True);dest.write_bytes(data);return 'downloaded'

if __name__=='__main__':
    for s in SOURCES:print(s['file'],fetch(s))
