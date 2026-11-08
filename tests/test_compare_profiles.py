"""Checks for tools/compare_profiles.py (ported from film-scan): a synthetic negative shot through a profile's own curves, and its print."""
import argparse,contextlib,io,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'));sys.path.insert(0,str(Path(__file__).resolve().parent))
import numpy as np
from PIL import Image,ImageDraw
try:
    import cv2,skimage  # noqa: F401
    import compare_profiles as film_compare
except ImportError:film_compare=None
from openfilmkit.films import stocks as films
from test_films import tiff16

def scene(seed=4,w=720,h=480):
    """Linear-light test scene: colored shapes on a textured ground (features to register), plus gray steps."""
    rng=np.random.default_rng(seed);im=Image.new('RGB',(w,h),(118,112,104));d=ImageDraw.Draw(im)
    for _ in range(260):
        x,y=rng.integers(0,w),rng.integers(0,h);s=int(rng.integers(6,40));c=tuple(int(v) for v in rng.integers(25,235,3))
        (d.ellipse if rng.random()<.5 else d.rectangle)([x,y,x+s,y+int(rng.integers(6,40))],fill=c)
    for i,v in enumerate(np.linspace(20,235,8)):d.rectangle([60+i*75,h-90,120+i*75,h-40],fill=(int(v),)*3)
    srgb=np.asarray(im,float)/255;return np.where(srgb<=.04045,srgb/12.92,((srgb+.055)/1.055)**2.4)

def negative(lin,model):
    """Raw transmission a scanner would record from film exposed to `lin`, using the profile's published curves."""
    raw=np.empty(lin.shape)
    for c,key in enumerate(('Red','Green','Blue')):
        t=model['physical_traces'][key];L,D=np.array(t['log_exposure']),np.array(t['status_m_density'])
        logH=np.log10(np.maximum(lin[...,c],1e-4))+np.interp(D[0]+.45*(D[-1]-D[0]),D,L)-np.log10(.18)
        raw[...,c]=.85*10**-(np.interp(logH,L,D)-D[0])
    return np.rint(np.clip(raw,0,1)*65535).astype(np.uint16)

@unittest.skipIf(film_compare is None,'needs OpenCV and scikit-image')
class PrintComparisonChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.data=films.DATA
    def tearDown(self):self.temp.cleanup()
    # This test found a converter fault: make_lut used to feed inverted transmission into curves normalized in
    # opacity and stretch them between their drawn ends, so the right profile lost to generic.
    def test_the_right_profile_matches_its_print_better_than_generic(self):
        lin=scene();model=films.profile('kodak-portra400');tiff16(self.root/'neg.tif',negative(lin,model))
        srgb=np.where(lin<=.0031308,12.92*lin,1.055*lin**(1/2.4)-.055)
        # A lab print: turned, trimmed and at another size than the scan.
        Image.fromarray(np.uint8(np.rint(srgb*255))).rotate(90,expand=True).crop((30,40,450,690)).resize((357,552)).save(self.root/'print.jpg',quality=95)
        (self.root/'pairs.json').write_text(json.dumps([dict(negative=str(self.root/'neg.tif'),print=str(self.root/'print.jpg'),stock='kodak-portra400')]))
        args=argparse.Namespace(pairs=str(self.root/'pairs.json'),out=str(self.root/'out'),root=self.data,profiles='',contrast=None)
        with contextlib.redirect_stdout(io.StringIO()):film_compare.score_pairs(args)
        r=json.loads((self.root/'out'/'results.json').read_text())[0]
        self.assertNotIn('error',r);self.assertGreater(r['registration']['inliers'],100);self.assertGreater(r['registration']['overlap'],.5)
        s=r['scores'];self.assertLess(s['kodak-portra400']['dE_bal'],s['generic']['dE_bal']);self.assertLess(s['kodak-portra400']['gray'],s['generic']['gray']);self.assertLess(s['kodak-portra400']['dL'],s['generic']['dL'])
        self.assertTrue(list((self.root/'out'/'views').glob('*.jpg')));self.assertIn('kodak-portra400',(self.root/'out'/'summary.txt').read_text())
    def test_a_turned_trimmed_print_registers_onto_its_negative(self):
        lin=scene();tiff16(self.root/'neg.tif',negative(lin,films.profile('kodak-portra400')))
        srgb=np.where(lin<=.0031308,12.92*lin,1.055*lin**(1/2.4)-.055)
        Image.fromarray(np.uint8(np.rint(srgb*255))).rotate(90,expand=True).transpose(Image.Transpose.FLIP_LEFT_RIGHT).crop((30,40,450,690)).resize((357,552)).save(self.root/'print.jpg',quality=95)
        (self.root/'pairs.json').write_text(json.dumps([dict(negative=str(self.root/'neg.tif'),print=str(self.root/'print.jpg'),stock='kodak-portra400')]))
        args=argparse.Namespace(pairs=str(self.root/'pairs.json'),out=str(self.root/'out'),root=self.data,profiles='',contrast=None)
        with contextlib.redirect_stdout(io.StringIO()):film_compare.score_pairs(args)
        r=json.loads((self.root/'out'/'results.json').read_text())[0]
        self.assertNotIn('error',r);self.assertTrue(r['registration']['mirror']);self.assertGreater(r['registration']['inliers'],100);self.assertGreater(r['registration']['overlap'],.5)
    def test_unrelated_print_is_reported_not_scored(self):
        model=films.profile('kodak-portra400');tiff16(self.root/'neg.tif',negative(scene(4),model))
        lin=scene(9);Image.fromarray(np.uint8(np.rint((np.where(lin<=.0031308,12.92*lin,1.055*lin**(1/2.4)-.055))*255))).save(self.root/'print.jpg')
        (self.root/'pairs.json').write_text(json.dumps([dict(negative=str(self.root/'neg.tif'),print=str(self.root/'print.jpg'),stock='kodak-portra400')]))
        args=argparse.Namespace(pairs=str(self.root/'pairs.json'),out=str(self.root/'out'),root=self.data,profiles='',contrast=None)
        with contextlib.redirect_stdout(io.StringIO()):film_compare.score_pairs(args)
        self.assertIn('error',json.loads((self.root/'out'/'results.json').read_text())[0])

if __name__=='__main__':unittest.main()
