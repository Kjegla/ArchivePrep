"""The golden corpus: byte patterns a real archive actually contains.

Every entry is a file shape that ``file_health()`` must return a specific
verdict for. Most of them exist because a real photo was once judged wrongly:
four whole classes of false positive were found only by running against two
real collections - a 3,322-file Google Takeout export and an 815-file iPhone
backup - and never showed up against fixtures.

This is the regression net for the parts of the application that must not
change: truncation detection, format sniffing, and the honest "unchecked"
verdict. Anything that touches those runs against this table first.

**Adding a case is a one-line edit.** When a real archive turns up a file
that gets the wrong verdict, write a builder for it, add a row, and say in
the last column why it exists. The "why" is the point - in two years it is
the only thing that explains why the row is allowed to matter.
"""
import struct

import pytest
from PIL import Image as PILImage

from conftest import SCRATCH, check, make_img, make_mp4_with_date, truncate
import archiveprep_core as core


# --------------------------------------------------------------------------
# Builders. Each takes the scratch folder and returns the file it made.
# --------------------------------------------------------------------------

def _good_jpeg(d):
    p = d / "good.jpg"
    make_img(p, model="SM-S918B", date="2023:05:10 14:30:00")
    return p


def _good_png(d):
    p = d / "good.png"
    make_img(p, color='blue', fmt='PNG')
    return p


def _good_mp4(d):
    p = d / "good.mp4"
    make_mp4_with_date(p, __import__('datetime').datetime(2019, 6, 15, 12, 0))
    return p


def _truncated_jpeg(d):
    p = d / "cut.jpg"
    p.write_bytes(_good_jpeg(d).read_bytes())
    truncate(p, 200)
    return p


def _truncated_png(d):
    p = d / "cut.png"
    p.write_bytes(_good_png(d).read_bytes())
    truncate(p, 20)
    return p


def _truncated_mp4(d):
    p = d / "cut.mp4"
    p.write_bytes(_good_mp4(d).read_bytes())
    truncate(p, 6)
    return p


def _mp4_without_moov(d):
    p = d / "nomoov.mp4"
    ftyp = struct.pack('>I', 16) + b'ftyp' + b'isom\x00\x00\x02\x00'
    mdat = struct.pack('>I', 16) + b'mdat' + bytes(8)
    p.write_bytes(ftyp + mdat)
    return p


def _empty_file(d):
    p = d / "empty.jpg"
    p.write_bytes(b"")
    return p


def _garbage_named_jpg(d):
    p = d / "junk.jpg"
    p.write_bytes(b"this is definitely not a jpeg")
    return p


def _half_a_photo(d):
    data = _good_jpeg(d).read_bytes()
    p = d / "really_cut.jpg"
    p.write_bytes(data[:len(data) // 2])
    return p


def _with_embedded_preview(d):
    """A photo carrying a small preview image inside an APP1 segment, the way
    a camera does. The preview has an end marker of its own."""
    original = _good_jpeg(d).read_bytes()
    preview = b'Exif\x00\x00' + b'\xff\xd8\xff\xdb' + bytes(64) + b'\xff\xd9'
    app1 = b'\xff\xe1' + struct.pack('>H', len(preview) + 2) + preview
    p = d / "intact_preview.jpg"
    p.write_bytes(original[:2] + app1 + original[2:])
    return p


def _truncated_past_its_preview(d):
    """The same photo, cut off after its preview. The preview's end marker
    survives, and must not be mistaken for proof the photo finished."""
    whole = _with_embedded_preview(d).read_bytes()
    p = d / "thumb_only.jpg"
    p.write_bytes(whole[:len(whole) // 8])
    assert p.read_bytes().count(b'\xff\xd9') >= 1, "case needs a stray end marker"
    return p


def _raw(d):
    p = d / "photo.arw"
    p.write_bytes(b"fake raw data" * 100)
    return p


def _avi(d):
    p = d / "clip.avi"
    p.write_bytes(b"fake avi" * 100)
    return p


def _zero_padded_jpeg(d):
    p = d / "padded.jpg"
    p.write_bytes(_good_jpeg(d).read_bytes() + b"\x00" * (3 * 1024 * 1024))
    return p


def _mpo_portrait(d):
    """A complete photo followed by a second embedded image that got cut off -
    what an iPhone Portrait / dual-camera shot looks like on disk."""
    whole = _good_jpeg(d).read_bytes()
    p = d / "portrait.jpg"
    p.write_bytes(whole + whole[:400])
    return p


def _pixel_motion_photo(d):
    p = d / "PXL_20251103_094531578.MP.jpg"
    p.write_bytes(_good_jpeg(d).read_bytes()
                  + struct.pack('>I', 16) + b'ftyp' + b'isom\x00\x00\x02\x00'
                  + bytes(40000))
    return p


def _samsung_motion_photo(d):
    p = d / "20240629_073655.jpg"
    p.write_bytes(_good_jpeg(d).read_bytes()
                  + b'\x00\x00\x01\x0a\x0e\x00\x00\x00Image_UTC_Data'
                  + bytes(1500))
    return p


def _photo_with_arbitrary_tail(d):
    p = d / "noisetail.jpg"
    p.write_bytes(_good_jpeg(d).read_bytes() + bytes(range(256)) * 200)
    return p


def _jpeg_named_mov(d):
    p = d / "IMG_0607.MOV"
    p.write_bytes(_good_jpeg(d).read_bytes())
    return p


def _jpeg_named_heic(d):
    p = d / "IMG_0803.HEIC"
    p.write_bytes(_good_jpeg(d).read_bytes())
    return p


def _png_named_jpg(d):
    p = d / "shot.jpg"
    p.write_bytes(_good_png(d).read_bytes())
    return p


def _webp_named_png(d):
    # A real WEBP, not a header stub. It used to be a stub, which passed only
    # because the check returned 'misnamed' and stopped before decoding it.
    # Now that a file is checked as what it is, the fixture has to be one.
    p = d / "04165a3a49aaf42842f7f02dc088f4f3.png"
    PILImage.new('RGB', (40, 30), 'purple').save(p, 'WEBP')
    return p


def _mp4_named_mov(d):
    p = d / "real.mov"
    p.write_bytes(_good_mp4(d).read_bytes())
    return p


# --------------------------------------------------------------------------
# The corpus
# --------------------------------------------------------------------------
# (name, builder, expected verdict, why this case is in the table)

GOLDEN = [
    # --- ordinary files, which must simply pass -----------------------------
    ("intact JPEG", _good_jpeg, 'ok',
     "the baseline: an ordinary camera photo"),
    ("intact PNG", _good_png, 'ok',
     "the baseline for the other format with a reliable end marker"),
    ("well-formed MP4", _good_mp4, 'ok',
     "the baseline for the container walk"),
    ("real video named .mov", _mp4_named_mov, 'ok',
     "MP4 and MOV are interchangeable in practice; nagging about a .mov "
     "that is technically an .mp4 would flag half an iPhone library"),

    # --- genuinely broken, which must be caught -----------------------------
    ("truncated JPEG", _truncated_jpeg, 'damaged',
     "the interrupted copy this whole check exists to find"),
    ("truncated PNG", _truncated_png, 'damaged',
     "same, for PNG"),
    ("truncated MP4", _truncated_mp4, 'damaged',
     "a half-finished download: the boxes claim more data than the file has"),
    ("MP4 with no moov box", _mp4_without_moov, 'damaged',
     "media data with no index - unplayable"),
    ("0-byte file", _empty_file, 'damaged',
     "what a failed copy leaves behind"),
    ("garbage named .jpg", _garbage_named_jpg, 'damaged',
     "not a photo at all and not a recognisable format either"),
    ("photo cut in half", _half_a_photo, 'damaged',
     "the control for the lenient cases below: leniency must not go so far "
     "that a genuinely unfinished photo passes"),
    ("truncated past its own preview", _truncated_past_its_preview, 'damaged',
     "THE subtle one. A camera embeds a preview near the start of a JPEG and "
     "that preview has an end marker of its own. Searching backwards for any "
     "end marker would find the preview's and call a cut-off photo intact, "
     "which is why _jpeg_scan_start locates the real photo's data first"),

    # --- perfectly fine, and must never be called damaged -------------------
    # All four were false positives found against real collections, not fixtures.
    ("complete JPEG + 3 MB of zero padding", _zero_padded_jpeg, 'ok',
     "recovery tools, card readers and backup exports pad a complete file "
     "out to a block boundary, leaving the end marker megabytes from the end"),
    ("iPhone Portrait / MPO", _mpo_portrait, 'ok',
     "two images in one file, and it is usually the SECOND one that got cut "
     "off, not the photo. Found in a real 815-file iPhone backup"),
    ("Pixel motion photo", _pixel_motion_photo, 'ok',
     "Pixel welds a short MP4 onto the back of an ordinary JPEG"),
    ("Samsung motion photo", _samsung_motion_photo, 'ok',
     "Samsung does the same thing with a different trailer format - which is "
     "why the rule is about the photo's own data finishing, not about what "
     "the file ends with"),
    ("complete photo + arbitrary tail", _photo_with_arbitrary_tail, 'ok',
     "generalises the two above: anything after a finished photo is somebody "
     "else's business"),
    ("photo with an embedded preview", _with_embedded_preview, 'ok',
     "the intact counterpart of the truncated-past-its-preview case"),

    # --- formats we cannot verify, which must say so rather than guess ------
    ("RAW", _raw, 'unchecked',
     "cannot be verified without decoding it; reported honestly so a good "
     "file is never wrongly accused"),
    ("AVI", _avi, 'unchecked',
     "same, for the video formats with no cheap structural check"),

    # --- the name disagrees with the contents, which is not damage ----------
    # A file is checked as what it is, not as what it is called, so all four
    # of these are simply healthy. They used to return a fourth verdict,
    # 'misnamed', which stopped the check before it ever ran - so the one
    # question this exists to answer went unanswered for exactly the files
    # whose names could not be trusted. Nothing here is renamed or moved:
    # the name is the source's business, the contents are ours.
    ("JPEG named .MOV", _jpeg_named_mov, 'ok',
     "a photo the computer would hand to a video player. The photo itself is "
     "fine, and saying so is the whole job"),
    ("JPEG named .HEIC", _jpeg_named_heic, 'ok',
     "same shape, found in a real iPhone backup"),
    ("PNG named .jpg", _png_named_jpg, 'ok',
     "checking it as a JPEG would report nonsense; checking it as the PNG it "
     "is reports the truth"),
    ("WEBP named .png", _webp_named_png, 'ok',
     "Google Takeout exports some pictures as WEBP under a .png name"),
]


@pytest.mark.parametrize("builder,expected,why",
                         [pytest.param(b, e, w, id=name)
                          for name, b, e, w in GOLDEN])
def test_golden_corpus_quick(builder, expected, why):
    """Quick mode must return the recorded verdict for every case."""
    path = builder(SCRATCH)
    status, reason = core.file_health(path)
    check(status == expected,
          f"expected {expected!r}, got {status!r} ({reason})\n"
          f"this case exists because: {why}")


@pytest.mark.parametrize("builder,expected,why",
                         [pytest.param(b, e, w, id=name)
                          for name, b, e, w in GOLDEN])
def test_golden_corpus_thorough(builder, expected, why):
    """Thorough mode fully decodes images, so it can only ever be stricter.
    It must not change any verdict in the table - a case that is fine in
    quick mode and damaged in thorough mode would mean one of the two is
    lying to the user."""
    path = builder(SCRATCH)
    status, reason = core.file_health(path, thorough=True)
    check(status == expected,
          f"thorough mode disagrees with quick mode: expected {expected!r}, "
          f"got {status!r} ({reason})\nthis case exists because: {why}")


def test_a_wrong_name_hides_nothing_from_the_check():
    """The reason the fourth verdict had to go.

    A file whose name disagrees with its contents used to return 'misnamed'
    and stop - so the one question this check exists to answer went
    unanswered for exactly the files whose names could not be trusted. In one
    real collection that was 804 of them.

    A wrong name is now no protection: the file is checked as what it is, and
    a broken photo is caught however it happens to be called.
    """
    intact = SCRATCH / "IMG_0607.MOV"
    intact.write_bytes(_good_jpeg(SCRATCH).read_bytes())
    status, reason = core.file_health(intact)
    check(status == 'ok',
          f"an intact photo called .MOV is healthy (got {status!r}, {reason})")

    broken = SCRATCH / "IMG_0608.MOV"
    broken.write_bytes(_good_jpeg(SCRATCH).read_bytes())
    truncate(broken, 200)
    status, reason = core.file_health(broken)
    check(status == 'damaged',
          f"...and a truncated one is still caught, despite the name "
          f"(got {status!r}, {reason})")

    # The other direction: the wrong name must not manufacture damage either.
    # Handing a photo to the MP4 structure check reports nonsense, which is
    # what the removed early return was really protecting against.
    check('mp4' not in reason.lower() and 'moov' not in reason.lower(),
          f"the reason talks about the photo, not about MP4 boxes ({reason})")


def test_sniff_says_nothing_rather_than_guessing():
    check(core.sniff_real_format(b'\xff\xd8\xff\xe0' + b'x' * 12)[0] == 'JPEG image',
          "sniff detects JPEG")
    check(core.sniff_real_format(b'RIFF' + b'\x00' * 4 + b'WEBP' + b'x' * 4)[0]
          == 'WEBP image', "sniff detects WEBP without confusing it with AVI")
    check(core.sniff_real_format(b'total garbage!!!')[0] is None,
          "an unknown format produces no verdict at all, rather than a guess")


def test_jpeg_scan_start_locates_the_photo_data():
    good = _good_jpeg(SCRATCH)
    size = good.stat().st_size
    start = core._jpeg_scan_start(good, size)
    check(start > 0, f"_jpeg_scan_start locates the photo's data (got {start})")
    check(start < size, "...and it is inside the file")
