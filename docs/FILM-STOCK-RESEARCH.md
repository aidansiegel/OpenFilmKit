# Film identity and conversion research — 5 November 2026

## Kirkland: a store brand, not a permanent emulsion

Costco dates the launch of the Kirkland Signature brand to 1995. That establishes the brand's earliest possible date, not a launch date for every film product. [Costco company history](https://www.costco.com/wcsstore/CostcoUSBCCatalogAssetStore/Attachment/A-CostcoStory-Base-0617.pdf).

There is direct, roll-specific evidence for German-made Kirkland 200: an owner transcribed a cartridge as Kirkland Signature 200, AP70/C-41, Made in Germany, barcode 018513. This supports investigating Agfa-family responses, but does not independently establish HDC Plus versus Vista or every roll's manufacturer. [Owner's cartridge transcription](https://www.flickr.com/groups/ishootfilm/discuss/72157634429783414/).

An archived Kirkland 200 package has a December 2002 expiry. Expiry is neither exposure date nor a definitive emulsion generation. [Film Packaging Archive, ref. 0441](https://fp-archive.com/film_packaging/by_expiry.html).

Claims that Kirkland also used Fuji appear in forum recollections, but this research did not establish an authoritative supplier-by-year schedule or a reliable transition year. A discussion mentioning both German and Japanese Kirkland also includes a different product called “Signature Color”; that separate Italian product must not be silently counted as Kirkland. [Photrio discussion, especially posts 40 and 44](https://www.photrio.com/forum/threads/signature-color-film-so-its-an-ecn-ii-process.183170/page-2).

Implementation: Kirkland 200/400 are brand identities. Their default is generic when no roll-specific choice has been saved. Original Agfa HDC Plus and Vista curves are selectable proposed alternatives. A Fuji response can be explicitly tried where the markings support it, but Japanese manufacture alone is not a universal Fuji identification. Year and country are recorded as clues; neither automatically changes a stock identity. A roll's saved choice of an Agfa HDC Plus adaptation stays in place; it does not claim an exact Kirkland emulsion.

## Other consumer stocks

Ferrania's own historical account documents the Scotch, 3M, Imation and private-label connection. It does not supply a numerical response for Imation 200. The catalog recognizes Imation/Scotch/Ferrania stocks without substituting Agfa or inventing a Solaris equivalence. [FILM Ferrania history](https://www.filmferrania.com/news/2014/the-3m-era-xz6je).

Kodak Gold 100/200, Max Zoom 800, historical Portra and Advantix, and original Agfa HDC Plus, Vista and Futura have named negative responses in Columbia's DoRF data. We extracted 19 complete RGB sets, excluding slides, cameras and ambiguously named Fuji F125/F400 curves. [DoRF](https://www.cs.columbia.edu/CAVE/software/softlib/dorf.php).

Two additional responses were traced directly from manufacturer PDF vectors: Kodak Ultra Max 400, E-7019 (February 2007, page 4), and Fujicolor Superia X-TRA 400 [CH], AF3-0217E (print code KAMIQ-06.10-FP, page 6). Each JSON records source URL/hash, chart coordinates, trace indices, sampled physical densities and the normalization. The PDF revision is not a date for a family photograph. [Kodak manufacturer sheet, archived copy](https://125px.com/docs/film/kodak/E7019_en-Ultra_Max_400.pdf), [Fujifilm manufacturer sheet](https://asset.fujifilm.com/master/emea/files/2020-10/9a958fdcc6bd1442a06f71e134b811f6/films_superia-xtra400_datasheet_01.pdf).

## Manufacturer data sheets added 6 November 2026

Nineteen more responses were traced from manufacturer characteristic-curve charts; `tools/research/datasheets.json` lists each sheet's URL, SHA-256, page, frame and axes, and every profile records the same. Kodak sheets come from the [125px Kodak technical-document archive](https://125px.com/techdocs/kodak/), which mirrors Kodak and Kodak Alaris publications.

- Current Kodak Alaris (February 2016): Ektar 100 (E-4046), Portra 160 (E-4051), Portra 400 (E-4050), Portra 800 (E-4040), Ultra Max 400 (E-7023).
- Earlier Kodak consumer stocks: Royal Gold 100, 200 and 400 (E-41/E-42/E-43, February 1998), Royal Gold 200 / RB (E-7006, March 2002), High Definition 200 (E-7017, July 2003) and 400 (E-7013, January 2003), Gold 100 and 200 (E-7022, February 2007), Ultra Max 800 (E-7024, December 2007), Elite Color 200 and 400 (E-4039, May 2006).
- Fujifilm: Superia Reala (AF3-967E, print code EIGI-00.12), and Superia 100 [CN] and 200 [CA] (AF3-007E/AF3-008E, 1998). The Superia 100/200 charts exist only as scanned images in [Fujifilm Hong Kong's PDFs](https://www.fujifilm.com.hk/products/consumer_film/pdf/superia_200_datasheet.pdf); they were traced from the pixels, with the grid fitted to the printed grid lines. The Reala sheet is a copy on a personal mirror, verified by hash.

Kodak printed the same curves under several names. Ultra Max 800 and Portra 800 (2016) match within 0.001 density; High Definition 200, Royal Gold 200 / RB and Elite Color 200 within 0.004; High Definition 400 and Elite Color 400 within 0.013. Each keeps its own profile so a roll can be chosen by the name on its box, and the stock notes say which curves are shared. Kodak's Portra 800 sheets (2009 and 2016) print the -3.0 and -2.0 exposure labels transposed; the evenly spaced ticks were used.

The DoRF Gold 100/200, Portra 800 and Max Zoom 800 profiles stay the defaults for those stocks, so earlier conversions are reproducible; the manufacturer curves are offered as candidates. Superia 100/200 replace their generic fallback. Kirkland 200/400 list Agfa and Fuji candidates, chosen per roll from cartridge and edge markings, because the supplier changed over the years.

Superia 800 and Konica Centuria were later found (below). Ferrania Solaris (and its Imation/Scotch private labels) remain generic; Ferrania brochures are mentioned in forum posts but no copy was found.

## More manufacturer data sheets, 7 November 2026

Seventy-one more data-sheet profiles bring the total to 111 profiles and 115 stocks, mostly from the [125px archive](https://125px.com/docs/film/) of Kodak, Fujifilm, Konica and Agfa documents. `tools/research/datasheets.json` gives every sheet, page, frame and axis.

- Kodak: Ektapress PJ100/400/800; Pro 100, 100T, 400, 400 MC and 1000; Bright Sun; Portra 100T; Vericolor III; Profoto 100; Royal Gold 25 and 1000; Ultra Color 100UC/400UC; Supra 100/200/400/800 and Royal Supra 200/400/800; Portra 160NC/160VC/400NC/400VC (E-4040, 2006-2009, and the earlier E-190 curves); Portra 800 (E-4040, 2006); Law Enforcement LE100/400/800; Vision3 250D, 500T and 200T.
- Fujifilm, mostly from the Professional Film Data Guide (AF3-207U): Pro 160S, Pro 160C, NPL 160, Pro 800Z, Superia 100 and 200 (later curves than the 1998 sheets), Superia X-TRA 800, nexia A200/400/800 (APS); plus Superia 1600 (AF3-145E), True Definition 400 (AF3-196E), Superia X-TRA 400 (AF3-151E), C200 (AF3-0249E, scanned chart) and Pro 400H (Japanese sheet).
- Konica: VX 100, VX Super 100/200/400, Centuria Super 100/200/800/1600.
- Agfa: Portrait 160, Portrait XPS 160, Optima 100/200/400, Optima II 100/200/400, Ultra 50.

Push-processing charts, slide films (Kodachrome, Agfachrome, Konica Chrome) and black-and-white films (Technical Pan, Agfapan, Neopan) were left out. Later reprints that print identical curves were skipped: Gold 200 (2016), Ultra Max 400 (2009), Ektar 100 (2008), Portra 400 (2010) and Portra 160 (2011) match the profiles already here; Pro 100T E-29 matches E-182; the data guide's Superia X-TRA 400 matches AF3-151E, and its Reala is within 0.04 density of AF3-967E. Royal Supra 200 matches Royal Gold 200 / RB, and Royal Supra 400/800 match Supra 400/800 (E-4029); they keep their own profiles so a roll can be chosen by name. Curves were compared after allowing one exposure shift, since Kodak moves its Log H reference between sheets and that shift does not change a profile.

Kodak's two May 2003 Supra sheets print different Supra 400 curves (E-4029 is the default, E-2519 an alternative). Portra 800 on the 2006 E-4040 sheet differs from the 2016 sheet, whose curves match Ultra Max 800.

Vision3 curves are for ECN-2 processing. CineStill 800T is Vision3 500T without its remjet layer and is usually developed in C-41, so the 500T profile is only an approximation for it. The 500T sheet's top log-exposure labels are unevenly placed; its scale was taken from the 16-stop camera axis at 0.3 log H per stop, as Kodak's other Vision3 sheets use.

Axes are fitted to the printed tick labels and then refined through the tick marks and grid lines, which corrected text-only fits by up to 7% on Fujifilm's guide and Agfa's sheets. Still generic: Konica VX 200/400 and Centuria 400 (only tiny scans inside page images), Kirkland, and the Ferrania family.

## Slide films, 7 November 2026

Thirty-one slide (reversal) profiles: Kodachrome 25, 64 and 200 (E-55, 2009); Ektachrome EPP, EPN, E200, 64T, 160T, 320T, P1600, 400X, E100VS, E100G, E100GX and 64 (EPR); Elite Chrome 100, Extra Color 100, 200 and 400; Fujichrome Provia 400F and 400X, Astia 100F, T64 and Velvia 100F (their own sheets) and Velvia 50, Velvia 100 and Sensia 200 (the Professional Film Data Guide); Konica Chrome R-100; Agfachrome RSX II 50, 100 and 200. Densitometry is Status A, as Kodak and Fujifilm publish for slides.

Kodachrome's curves are identical across E-55 (1996, 2003, 2009) and the consumer E-88 sheets (1998-2009); Agfa's two documents print the same RSX II curves; both E100G revisions match. Slide curves converge towards clear film, so channels are assigned from the printed R/G/B labels, from a legend's line styles (Fujifilm draws red solid, green dash-dot, blue dashed), or, where Kodak repeats labels on callouts or prints none, from the order given in the spec. Fujifilm stops drawing green and blue where they merge with red on some charts; those traces continue along red. Kodak's P1600 sheet shows push-2 (E-6P) curves.

Not traced: Konica Chrome Centuria 100/200 (dashed curves drawn in disconnected pieces), Fujichrome Provia 100F, Sensia 100/400 and RTP II (their guide charts did not separate into three traces and their own sheets are scans), Kodachrome-specific scanner calibration (outside what curves can describe).

## Conversion fix, 7 November 2026

The print comparison's synthetic check showed the uncalibrated conversion misused every curve: it treated inverted transmission as if it were the profiles' normalized opacity, and stretched each curve between the ends the chart happened to draw. Film simulated through Portra 400's own curves converted closer to its print with the generic conversion than with the Portra profile, and midtones came out up to nine times too bright. Data-sheet profiles now invert their stored log-exposure/density traces, which recovers simulated exposures exactly for every film tried. DoRF curves, which have no absolute density or exposure, are read as normalized opacity: against film simulated from Kodak's own curves for Gold 100/200 and Portra 160NC/400NC/800, that cut the mean exposure error from about 0.6 to 0.4 log units; the rest is the missing absolute scale. Conversions made before this fix with a named profile and no calibration plan will look different if redone; density-calibrated conversions and generic ones are unchanged.

## What these profiles can establish

DoRF normalizes response/exposure and transforms decreasing negative responses to increasing ones; the paper describes those operations and warns through its model that a complete imaging chain includes scanning. Our adaptations normalize measured scanner transmission before reversing a published curve. Manufacturer Status M density is likewise not a calibration of the Plustek's RGB spectral response. Every profile is labeled an approximate viewing starting point. [Grossberg and Nayar, Modeling the Space of Camera Response Functions](https://www.cs.columbia.edu/CAVE/publications/pdfs/Grossberg_PAMI04.pdf).

No proprietary SilverFast or ColorPerfect tables were copied. No current camera-scanning LUT was applied to a broadband Plustek scan. There is no claim that an unmeasured scanner/film combination is colorimetrically calibrated.

## Workflow

Keep the original linear RGB16 TIFF and capture record. Select stock and, where necessary, a proposed response. Make a separate preview and processing record; review before rendering full size. Contrast, gamma and warmth remain independent. Use `--levels-from` for neighboring frames that should share starting channel levels, rather than independently leveling away real scene differences. The converter never overwrites approved edits, changes original sidecars, enrolls a batch or modifies pipeline jobs.
