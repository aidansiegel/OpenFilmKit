"""A photo's pixels the way its file says to show them: turned by its orientation tag, and in sRGB.

Neither is in the pixels. A camera or a phone stores a photo as its sensor lay and notes the turn
beside it (EXIF orientation; TIFF has the same tag), and an embedded color profile says what the
numbers mean as colors. `clean` writes its copies upright and tagged sRGB, carrying neither note
over, so both are followed where a photo is read: for `clean`, and for the UI, which shows an
original next to its cleaned copy. The kit's own conversions are upright sRGB and come through
untouched.

Colors are converted by LittleCMS, through imagecodecs, which takes 8- and 16-bit arrays alike
(Pillow's ImageCms, the same library, is 8-bit only).
"""
import io

import numpy as np

ORIENTATION, ICC_PROFILE = 274, 34675    # tag numbers in a TIFF; EXIF has orientation under the same one
INTENT = 'relative colorimetric, with black point compensation'
SAME = 257           # a profile that moves no color by a whole 8-bit level (here in 16-bit units) is sRGB
STRIP = 4_000_000    # pixels converted at a time: LittleCMS can't convert in place


class ProfileError(Exception):
    """A photo's color profile can't be used to convert it."""


def tags(im):
    """(orientation, color profile or None) of an open Pillow image."""
    return im.getexif().get(ORIENTATION, 1), im.info.get('icc_profile')


def tiff_tags(page):
    """The same, of a tifffile page."""
    return int(page.tags.valueof(ORIENTATION, 1)), page.tags.valueof(ICC_PROFILE)


def tiff_page(tif):
    """The page of an open TIFF (a tifffile.TiffFile) that holds its picture: the first that isn't a
    reduced copy or a mask (a thumbnail can come first, and SilverFast keeps infrared on a page of
    its own), and only if the file can hold the image its header describes. tifffile fills what
    is missing with zeros, so a file cut short, or one with a damaged header, otherwise reads as a
    picture partly or wholly black, as large as the header says. ValueError otherwise."""
    page = next((p for p in tif.pages if not p.subfiletype & 5), None)   # 1: reduced-size copy, 4: mask
    if page is None:
        raise ValueError('TIFF has no full-size image.')
    offsets, counts, size = page.dataoffsets, page.databytecounts, tif.filehandle.size
    expected = int(np.prod(page.chunked))
    if len(offsets) != expected or len(counts) != expected:
        raise ValueError(f'TIFF is damaged: its header describes {expected} strips or tiles and lists {len(offsets)}.')
    if page.compression == 1 and page.is_contiguous:   # stored plainly, in one run (some programs get its strip sizes wrong)
        cut = offsets[0] + page.nbytes > size
    else:
        cut = any(o <= 0 or c <= 0 or o + c > size for o, c in zip(offsets, counts))
    if cut:
        raise ValueError('TIFF pixel data is truncated.')
    if page.nbytes > 2000 * max(sum(counts), 2 ** 20):   # no compression packs a picture that small
        raise ValueError(f'TIFF is damaged: its header describes an image of {page.nbytes / 1e9:.1f} GB, '
                         f'and it holds {sum(counts):,} bytes.')
    return page


def upright(img, orientation):
    """`img` turned and mirrored as its orientation tag (2 to 8) says to show it; `img` itself when
    the tag says it is upright, or nothing that makes sense."""
    if orientation not in range(2, 9):
        return img
    if orientation >= 5:    # stored on its side
        img = img.swapaxes(0, 1)
    if orientation in (3, 4, 7, 8):
        img = img[::-1]
    if orientation in (2, 3, 6, 7):
        img = img[:, ::-1]
    return np.ascontiguousarray(img)


def profile_name(icc):
    """What a color profile calls itself ("Display P3"), for recipes and messages."""
    from PIL import ImageCms
    try:
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip() or 'unnamed'
    except (OSError, ImageCms.PyCMSError):
        return 'unreadable'


def method():
    """What converts the colors, for recipes."""
    import imagecodecs
    return f'LittleCMS ({imagecodecs.cms_version()}), {INTENT}'


def _converter(icc, gray, exact=True):
    """A function that takes pixels (RGB, or gray as a 2-D array) from the profile `icc` to sRGB, as RGB."""
    import imagecodecs
    from .films.convert import srgb_icc
    cms, srgb = imagecodecs.CMS, srgb_icc()
    flags = cms.FLAGS.BLACKPOINTCOMPENSATION | (cms.FLAGS.NOOPTIMIZE if exact else 0)

    def convert(a):
        try:
            return imagecodecs.cms_transform(a, icc, srgb, colorspace='gray' if gray else 'rgb', planar=False,
                                             outcolorspace='rgb', outplanar=False,
                                             intent=cms.INTENT.RELATIVE_COLORIMETRIC, flags=flags)
        except imagecodecs.CmsError as e:
            raise ProfileError(f"can't convert from its color profile ({profile_name(icc)}) to sRGB: it is damaged, "
                               f'or for other pixels than these, such as CMYK (LittleCMS: {e})') from e
    return convert


def _is_srgb(icc, gray):
    """Whether a profile is sRGB, by what it does rather than its bytes or its name (sRGB profiles
    come in many versions, from many makers): colors from all over its range stay where they are."""
    steps = np.linspace(0, 65535, 256 if gray else 17).round().astype(np.uint16)
    probe = steps[None] if gray else np.stack(np.meshgrid(steps, steps, steps, indexing='ij'), -1).reshape(1, -1, 3)
    moved = _converter(icc, gray)(probe).astype(np.int32) - (probe[..., None] if gray else probe)
    return np.abs(moved).max() < SAME


def to_srgb(img, icc, exact=True):
    """`img` converted from its color profile `icc` to sRGB: (the image as RGB, the profile's name).
    `img` is RGB or, as a 2-D array, gray, at 8 or 16 bits. Without a profile, or with one that is
    sRGB, nothing is converted and the name is None: such a photo's pixels stay exactly as they
    are. An RGB image that can be written to is converted in place. ProfileError if the profile
    can't be used.

    `exact` computes every pixel (some 7 megapixels a second). Otherwise LittleCMS looks colors up
    in a table it fills first: 20 times quicker at 8 bits and within a level there, so fine to look
    at, but at 16 bits up to 19 8-bit levels off where colors clip at the edge of sRGB."""
    gray = img.ndim == 2
    if gray and not (icc and icc[16:20] == b'GRAY'):    # no gray profile: gray is R = G = B
        img, gray = np.dstack([img] * 3), False
    if not icc or _is_srgb(icc, gray):
        return (np.dstack([img] * 3) if gray else img), None
    convert = _converter(icc, gray, exact)
    out = img if not gray and img.flags.writeable else np.empty(img.shape[:2] + (3,), img.dtype)
    rows = max(1, STRIP // img.shape[1])
    for y in range(0, img.shape[0], rows):
        out[y:y + rows] = convert(img[y:y + rows])
    return out, profile_name(icc)
