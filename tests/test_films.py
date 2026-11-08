"""Film selection and non-destructive RGB16 conversion.

Ported from film-scan's test_film_scan.py and test_film_density.py. Roll choices
now live in roll folders; the bundled data is read in place (nothing writes to it).
"""
import argparse,contextlib,io,json,struct,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
try:
 import numpy as np
 import tifffile
 from PIL import Image
 from openfilmkit.films import convert as conversion,density as d
except ImportError:np=None
from openfilmkit import cli,roll as rolls
from openfilmkit.films import stocks as films
ROOT=Path(__file__).resolve().parents[1]

def tiff16(path,a):
 h,w,_=a.shape;offset=140
 tags=[(256,4,1,w),(257,4,1,h),(258,3,3,134),(259,3,1,1),(262,3,1,2),(273,4,1,offset),(277,3,1,3),(278,4,1,h),(279,4,1,a.nbytes),(284,3,1,1)]
 content=b'II'+struct.pack('<HIH',42,8,len(tags))
 for tag,kind,count,value in tags:content+=struct.pack('<HHII',tag,kind,count,value)
 content+=struct.pack('<IHHH',0,16,16,16)
 assert len(content)==offset
 path.write_bytes(content+a.astype('<u2').tobytes())

def make_roll(path,stock,**choice):
 rolls.create(path);rolls.set_choice(path,films.roll_choice(stock,**choice));return path

class Selection(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def test_identity_does_not_become_unverified_emulsion(self):
  r=make_roll(self.root/'roll-90','Kirkland Signature 200',made_in='Japan',year_hint=2004)
  s=films.selection(saved=rolls.load(r)['film_choice'],roll='roll-90');self.assertEqual(s['profile_id'],'generic');self.assertTrue(s['year_does_not_determine_emulsion'])
  r=make_roll(self.root/'roll-35','kirkland-200',profile_id='agfa-hdc200',year_hint=1999)
  s=films.selection(saved=rolls.load(r)['film_choice']);self.assertEqual(s['profile_id'],'agfa-hdc200');self.assertEqual(s['stock_id'],'kirkland-200')
  self.assertEqual(films.selection('Imation 200')['profile_id'],'generic')
 def test_roll_choice_keeps_history_and_validates(self):
  r=make_roll(self.root/'roll-1','imation-200');rolls.set_choice(r,films.roll_choice('kodak-gold200'))
  self.assertEqual(len(list((r/'settings-history').glob('*.json'))),2);self.assertEqual(rolls.load(r)['film_choice']['stock_id'],'kodak-gold200')
  for bad in (dict(stock_value='no-such-film'),dict(stock_value='imation-200',year_hint=1800),dict(stock_value='imation-200',profile_id='no-such-profile')):
   with self.assertRaises(ValueError):films.roll_choice(**bad)
 def test_catalog_and_datasheet_specs_match_the_profiles(self):
  names=set(films.profile_ids())|{'generic'}
  for s in films.stocks():
   for ident in [s['default_profile'],*s['candidate_profiles']]:
    self.assertIn(ident,names,s['id'])
    if ident!='generic':self.assertEqual(films.profile(ident).get('film','color-negative'),s.get('film','color-negative'),(s['id'],ident))
  specs=json.loads((ROOT/'tools'/'research'/'datasheets.json').read_text());hashes={s['file']:s['sha256'] for s in specs['sources']}
  for spec in specs['profiles']:
   model=films.profile(spec['id']);self.assertEqual(model['source_sha256'],hashes[spec['file']],spec['id']);self.assertIn('physical_traces',model)
 def test_compressed_profiles_hash_by_content(self):
  self.assertEqual(len(films.profile_ids()),142)
  self.assertEqual(len(films.profile_sha256('agfa-hdc200')),64);self.assertIsNone(films.profile_sha256('generic'))

@unittest.skipIf(np is None,'needs numpy and Pillow')
class Conversion(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def args(self,**kw):
  opts=dict(source=str(self.root/'raw.tif'),output=str(self.root/'view.jpg'),stock='kirkland-200',roll=None,profile='agfa-hdc200',crop=None,levels_from=None,max_side=1800,contrast=1.3,gamma=1.,warmth=0.);opts.update(kw);return argparse.Namespace(**opts)
 def raw(self,path=None):
  a=np.linspace(2000,50000,12*10*3).reshape(10,12,3).astype('uint16');tiff16(path or self.root/'raw.tif',a);return a
 def test_all_profiles_are_complete_monotone_and_traceable(self):
  for ident in films.profile_ids():
   model=films.profile(ident);slide=model.get('film')=='slide'
   table=(conversion.slide_lut if slide else conversion.make_lut)(model,[.01,.02,.03],[.7,.6,.5]);steps=np.diff(table.astype('int32'),axis=0)
   # Negatives invert (brighter scan, darker print); slides do not.
   self.assertEqual(table.shape,(65536,3));self.assertTrue(np.all(steps>=0) if slide else np.all(steps<=0),ident);self.assertIn('http',model['source'])
 def test_all_profiles_share_one_curve_scale(self):
  # DoRF and traced curves alike reach half response at roughly 7-45% exposure. A traced curve
  # normalized by transmission instead of opacity reached it below 0.2% and rendered nearly black.
  for ident in films.profile_ids():
   model=films.profile(ident)
   for channel,c in model['curves'].items():
    x,y=np.asarray(c['irradiance']),np.asarray(c['response'])
    self.assertTrue((np.diff(y)>0).all(),(ident,channel));self.assertEqual((x[0],y[0],x[-1],y[-1]),(0,0,1,1),(ident,channel))
    if model.get('film')!='slide':self.assertTrue(.05<np.interp(.5,y,x)<.6,(ident,channel))
 def test_slide_profiles_render_a_grey_scene_grey(self):
  hi=np.array([.8,.7,.6])
  for ident in films.profile_ids():
   model=films.profile(ident)
   if model.get('film')!='slide':continue
   traces=[(np.array(model['physical_traces'][k]['log_exposure']),np.array(model['physical_traces'][k]['density'])) for k in conversion.CHANNELS]
   lut=conversion.slide_lut(model,[.001]*3,hi);start=max(L[0] for L,_ in traces);end=min(L[-1] for L,_ in traces)
   for e in np.linspace(start,end,9)[1:-1]:
    raw=[int(round(65535*hi[c]*10**-(np.interp(e,L,D)-D[-1]))) for c,(L,D) in enumerate(traces)]
    out=[lut[raw[c],c] for c in range(3)];self.assertLess(max(out)-min(out),1200,(ident,e,out))
 def test_slide_capture_converts_as_a_positive(self):
  self.raw();Path(str(self.root/'raw.tif')+'.json').write_text(json.dumps({'film':'slide'}))
  r=conversion.convert(self.args(stock=None,profile=None,contrast=None))
  self.assertEqual((r['film'],r['film_selection']['stock_id'],r['contrast']),('slide','unknown-slide',1.0))
  lut=np.load(str(self.root/'view.jpg')+'.lut.npy');self.assertTrue(np.all(np.diff(lut.astype('int32'),axis=0)>=0))
  for kw in (dict(output=str(self.root/'b.jpg'),stock='kodak-gold200',profile=None),dict(output=str(self.root/'c.jpg'),stock='kodak-kodachrome64',profile='kodak-gold200')):
   with self.assertRaises(ValueError):conversion.convert(self.args(**kw))
  Path(str(self.root/'raw.tif')+'.json').write_text(json.dumps({'film':'color-negative'}))
  with self.assertRaises(ValueError):conversion.convert(self.args(output=str(self.root/'d.jpg'),stock='kodak-kodachrome64',profile=None))
 def test_rgb16_conversion_preserves_source_and_records_recipe(self):
  a=self.raw();self.assertTrue(np.array_equal(conversion.raw_open(self.root/'raw.tif'),a));before=conversion.sha(self.root/'raw.tif')
  r=conversion.convert(self.args(crop=[1,1,11,9]))
  self.assertEqual(before,conversion.sha(self.root/'raw.tif'))
  with Image.open(self.root/'view.jpg') as im:self.assertEqual(im.size,(10,8))
  self.assertFalse(r['approved']);self.assertEqual(r['input_bits_per_channel'],16);self.assertEqual(r['profile_sha256'],films.profile_sha256('agfa-hdc200'))
  with self.assertRaises(ValueError):conversion.convert(self.args())
 def test_shared_levels_are_reused_and_wrong_stock_is_rejected(self):
  self.raw()
  first=conversion.convert(self.args())
  second=conversion.convert(self.args(output=str(self.root/'second.jpg'),levels_from=str(self.root/'view.jpg.json')))
  self.assertEqual(first['transmission_low_rgb'],second['transmission_low_rgb']);self.assertEqual(second['levels_basis']['type'],'shared')
  with self.assertRaises(ValueError):conversion.convert(self.args(output=str(self.root/'wrong.jpg'),stock='kodak-gold200',profile=None,levels_from=str(self.root/'view.jpg.json')))
  self.assertFalse((self.root/'wrong.jpg').exists())
 def test_conversion_is_byte_reproducible(self):
  # film-scan embedded the current time in the sRGB profile, so its files differed between runs.
  self.raw()
  for ext in ('.jpg','.png'):
   a,b=self.root/('a'+ext),self.root/('b'+ext)
   conversion.convert(self.args(output=str(a)));conversion.convert(self.args(output=str(b)))
   self.assertEqual(a.read_bytes(),b.read_bytes(),ext)
 def test_gamma_lifts_midtones_without_changing_geometry(self):
  raw=np.full((4,6,3),40000,np.uint16);lut=np.repeat(np.arange(65536,dtype=np.uint16)[:,None],3,axis=1)
  a=conversion.render(raw,lut,gamma=1);b=conversion.render(raw,lut,gamma=1.08)
  self.assertEqual(a.shape,b.shape);self.assertTrue((b>a).all())
 def test_checksum_mismatch_stops_before_writing(self):
  self.raw();Path(str(self.root/'raw.tif')+'.json').write_text(json.dumps({'sha256':'bad'}))
  with self.assertRaises(ValueError):conversion.convert(self.args())
  self.assertFalse((self.root/'view.jpg').exists())
 def test_density_calibration_records_plan_and_preserves_raw(self):
  self.raw();before=conversion.sha(self.root/'raw.tif')
  calibration={'film_base_transmission_rgb':[.8,.7,.6],'profile_strength':.4,'scenes':[{'scene':'test','density_offsets_rgb':[0,0,0],'density_black':[0,0,0],'density_mid':[.3,.3,.3],'density_white':[1,1,1],'density_mid_target':.58}]}
  p=self.root/'calibration.json';p.write_text(json.dumps(calibration))
  r=conversion.convert(self.args(calibration=str(p),scene='test',recover_clipped_blue=True))
  self.assertEqual(before,conversion.sha(self.root/'raw.tif'));self.assertEqual(r['density_calibration']['plan_sha256'],conversion.sha(p));self.assertTrue(r['density_calibration']['recover_clipped_blue'])
 def test_saved_density_plan_selects_scene_for_exact_capture(self):
  self.raw();digest=conversion.sha(self.root/'raw.tif')
  calibration={'film_base_transmission_rgb':[.8,.7,.6],'capture_scenes':{digest:'room'},'scenes':[{'scene':'room','density_offsets_rgb':[0,0,0],'density_black':[0,0,0],'density_mid':[.3,.3,.3],'density_white':[1,1,1]}]}
  plan=self.root/'cal.json';plan.write_text(json.dumps(calibration))
  r27=make_roll(self.root/'roll-27','kodak-gc400-8',profile_id='generic',calibration_file=str(plan),recover_clipped_blue=True)
  r=conversion.convert(self.args(stock=None,roll=str(r27),profile=None))
  self.assertEqual(r['density_calibration']['scene']['scene'],'room');self.assertTrue(r['density_calibration']['recover_clipped_blue']);self.assertEqual(r['film_selection']['stock_id'],'kodak-gc400-8');self.assertEqual(r['film_selection']['roll'],'roll-27')
 def test_multi_scene_calibration_requires_scene_before_any_output(self):
  self.raw();calibration={'scenes':[{'scene':'one'},{'scene':'two'}]};p=self.root/'calibration.json';p.write_text(json.dumps(calibration))
  with self.assertRaises(ValueError):conversion.convert(self.args(calibration=str(p)))
  self.assertFalse((self.root/'view.jpg').exists())
 def test_bad_parameters_and_flat_levels_stop(self):
  with self.assertRaises(ValueError):conversion.validate_levels([.1]*3,[.1]*3)
  self.raw()
  with self.assertRaises(ValueError):conversion.convert(self.args(gamma=0))
  self.assertFalse((self.root/'view.jpg').exists())
 def test_kit_capture_in_a_roll_uses_roll_choice_and_derived_folder(self):
  r=make_roll(self.root/'roll-35','kirkland-200',profile_id='agfa-hdc200');master=r/'masters'/'frame-01.tif';self.raw(master)
  Path(str(master)+'.json').write_text(json.dumps({'film':{'film':'color-negative','stock':'kirkland-200','roll':{}},'sha256':conversion.sha(master)}))
  for expected in ('frame-01-v1.jpg','frame-01-v2.jpg'):
   out=io.StringIO()
   with contextlib.redirect_stdout(out):code=cli.main(['convert',str(master),'--json'])
   self.assertEqual(code,0);rec=json.loads(out.getvalue())
   self.assertEqual(Path(rec['output']).resolve(),(r/'derived'/expected).resolve());self.assertEqual(rec['film_selection']['profile_id'],'agfa-hdc200');self.assertEqual(rec['film_selection']['selection_basis'],'saved roll choice')

@unittest.skipIf(np is None,'needs numpy, Pillow and tifffile')
class OtherScanSoftware(unittest.TestCase):
 """Raw scans as SilverFast (HDR, HDRi) and VueScan (raw, RGBI raw) save them read the same as the kit's masters."""
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.rgb=np.linspace(2000,50000,24*20*3).reshape(20,24,3).astype('uint16');self.ir=np.full((20,24),60000,np.uint16)
 def tearDown(self):self.temp.cleanup()
 def write(self,name,data,byteorder='<',thumbnail=False,infrared_page=False,**kw):
  path=self.root/name
  with tifffile.TiffWriter(path,byteorder=byteorder) as w:
   w.write(data,photometric='rgb',**kw)
   if thumbnail:w.write(self.rgb[::4,::4].copy(),photometric='rgb',subfiletype=1)
   # SilverFast marks its infrared page as a mask (NewSubfileType 4), which tifffile won't write by name.
   if infrared_page:w.write(self.ir,photometric='minisblack',extratags=[(254,'I',1,4,True)])
  return path
 def test_layouts_and_compressions_read_identically(self):
  cases={'lzw':dict(data=self.rgb,compression='lzw'),'lzw-predictor':dict(data=self.rgb,compression='lzw',predictor=True),
   'zip':dict(data=self.rgb,compression='zlib'),'packbits':dict(data=self.rgb,compression='packbits'),
   'planar':dict(data=np.moveaxis(self.rgb,-1,0),planarconfig='separate'),'big-endian':dict(data=self.rgb,byteorder='>'),
   'vuescan-rgbi':dict(data=np.dstack([self.rgb,self.ir]),extrasamples=['unspecified']),
   'silverfast-hdri':dict(data=self.rgb,thumbnail=True,infrared_page=True)}
  for name,kw in cases.items():
   self.assertTrue(np.array_equal(conversion.raw_open(self.write(name+'.tif',**kw)),self.rgb),name)
 def test_plain_files_are_memory_mapped(self):
  for kw in (dict(data=self.rgb),dict(data=self.rgb,byteorder='>'),dict(data=self.rgb,thumbnail=True,infrared_page=True)):
   self.assertIsInstance(conversion.raw_open(self.write('plain.tif',**kw)).base,np.memmap)
 def test_compressed_rgbi_scan_converts_like_the_plain_one(self):
  plain=self.write('plain.tif',self.rgb);rgbi=self.write('rgbi.tif',np.dstack([self.rgb,self.ir]),extrasamples=['unspecified'],compression='lzw')
  args=dict(stock=None,roll=None,profile='kodak-gold200',crop=None,levels_from=None,max_side=0,contrast=1.3,gamma=1.,warmth=0.)
  for src in (plain,rgbi):conversion.convert(argparse.Namespace(source=str(src),output=str(src.with_suffix('.jpg')),**args))
  self.assertEqual(plain.with_suffix('.jpg').read_bytes(),rgbi.with_suffix('.jpg').read_bytes())
 def test_scans_without_16_bit_rgb_are_refused(self):
  for name,data,photometric in (('8-bit.tif',(self.rgb>>8).astype('uint8'),'rgb'),('grey.tif',self.rgb[:,:,0],'minisblack'),('float.tif',self.rgb.astype('float32'),'rgb')):
   tifffile.imwrite(self.root/name,data,photometric=photometric)
   with self.assertRaisesRegex(ValueError,'16 bits per channel'):conversion.raw_open(self.root/name)
  (self.root/'junk.tif').write_bytes(b'not a tiff')
  with self.assertRaisesRegex(ValueError,'Not a readable TIFF'):conversion.raw_open(self.root/'junk.tif')

@unittest.skipIf(np is None,'needs numpy and Pillow')
class Density(unittest.TestCase):
 def test_colored_base_has_zero_relative_density(self):
  base=np.array([.48,.23,.1]);self.assertTrue(np.allclose(d.density(base,base),0))
  self.assertTrue(np.allclose(d.density(base/10,base),1))
 def test_profile_exposure_anchors_match_without_huge_midpoint_shift(self):
  for ident in ['generic','kodak-gc400-8','agfa-hdc200']:
   for channel in d.CHANNELS:
    result=d.matched_response(films.profile(ident),np.array([0,.58,1]),channel,pivot=.58)
    self.assertTrue(np.allclose(result,[0,.58,1],atol=1e-7),ident)
 def test_all_profiles_produce_monotone_finite_luts(self):
  for ident in films.profile_ids():
   model=films.profile(ident)
   if model.get('film')=='slide':
    with self.assertRaises(ValueError):d.density_lut(model,[.48,.23,.1],[0,0,0],[.1,.1,.1],[1.6,1.6,1.6])
    continue
   lut=d.density_lut(model,[.48,.23,.1],[0,0,0],[.1,.1,.1],[1.6,1.6,1.6],mid=[.7,.7,.7])
   self.assertTrue(np.isfinite(lut).all());self.assertTrue((np.diff(lut.astype(int),axis=0)<=0).all(),ident)
 def test_missing_blue_recovery_does_not_change_uncorrupted_pixels(self):
  lut=np.repeat(np.arange(65536,dtype=np.uint16)[:,None],3,axis=1);raw=np.array([[[12000,22000,30000],[12000,22000,0]]],dtype=np.uint16)
  a=d.render_density(raw,lut,contrast=1,recover_clipped_blue=False);b=d.render_density(raw,lut,contrast=1,recover_clipped_blue=True)
  self.assertTrue(np.array_equal(a[:,0],b[:,0]));self.assertTrue(np.array_equal(a[0,1,:2],b[0,1,:2]));self.assertGreater(b[0,1,2],a[0,1,2])
 def test_degenerate_anchor_fails(self):
  with self.assertRaises(ValueError):d.density_lut(films.profile('generic'),[.4,.2,.1],[0,0,0],.5,.5)

if __name__=='__main__':unittest.main()
