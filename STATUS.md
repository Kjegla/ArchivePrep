# V35 status

**This branch is unfinished.** It is pushed so the work is not lost, ahead of a
full architectural review (V40). The released version is still
[v34](../../releases).

Nothing here has been built into a download, and no tag has been created, so
the Releases page is untouched.

## What works, and is proven

All five V35 features are implemented and covered by the test suite:

| Feature | What it does |
|---|---|
| Duplicates by content | Byte-for-byte matches only, never by filename. Extra copies go to `Duplicates/`, mirroring where they came from |
| Smart keeper picking | A damaged copy can never beat a healthy one; real camera info beats none; `IMG_1234.jpg` beats `IMG_1234 (1).jpg` |
| Damage check | Standalone **🩺 Check Files** button plus an opt-in check while organizing. Quick and Thorough modes |
| Empty-folder sweep | Bottom-up across the whole source tree. Only genuinely empty folders; no file is ever deleted |
| Wrong extensions | Renamed to the correct extension and set aside in `Wrong Extension/`, with its own **↩️ Undo Renames** button |

**218 automated checks pass.** They drive the real app with its window hidden,
against real generated files in a temporary folder - so what is tested is what
ships, not a stripped-down copy of the logic:

```bash
python tests/test_photo_organizer.py
```

Tested against two real photo collections, not just fixtures - which is what
caught four separate classes of false positive that the fixtures missed
(zero-padded JPEGs, iPhone MPO portrait shots, Pixel and Samsung motion photos,
and images whose extension lied about their format):

| Collection | Files | Damaged | Wrong extension |
|---|---|---|---|
| Google Takeout | 3,322 | **0** | 30 |
| iPhone backup | 815 | 1 (genuine) | 185 |

Every "damaged" verdict is cross-examined by independently asking Pillow to
fully decode the file. **No false positives remain** across 4,137 real photos.

## What is not proven

- The RAW/Pixel matching added last (`media_base_name`, `lookup_model`,
  `camera_from_filename`) **has no automated test yet.** It was verified by hand
  against the real Takeout folder - 888 of 897 RAW files now match their JPEG
  and land under `Pixel 9 Pro`, the remaining 9 under `Google Pixel`, none in
  `Unknown Camera` - but nothing in the suite guards it against regressions.

## Open questions - deliberately undecided

These are for the V40 review. They were left open on purpose rather than
guessed at.

1. **The 14 redundant motion-photo clips.** Google Takeout exports the video
   half of a motion photo a second time as a separate file, already byte-for-byte
   inside the matching `.jpg`. Where should those go - `Duplicates/`, their own
   folder, or left alone with a note in the report? Evidence and the proposed
   detection rule are in [GOOGLE_TAKEOUT_NOTES.md](GOOGLE_TAKEOUT_NOTES.md).

2. **RAW files with no surviving JPEG.** 9 of them can be identified as a Pixel
   from the filename but not as *which* Pixel, so they get a generic
   `Google Pixel` folder next to `Pixel 9 Pro`. Should they be assumed to be the
   same model as the other 888, or kept honestly separate?

3. **Harmless versus breaking extension mismatches.** A WEBP named `.png` opens
   fine everywhere; a photo named `.MOV` does not open at all. Both are
   currently flagged the same way. Should they be?

4. **Unknown-extension sniffing is tied to extension fixing.** Turning off
   "fix wrong extensions" also stops the app reading file headers to identify
   unfamiliar extensions, which hides files rather than just leaving them
   alone. These should probably be separate options.

## Known housekeeping

- An iPhone backup folder was organized with an **older build** whose damage
  check produced false positives. It needs **↩️ Undo Last Run** before being
  re-run, otherwise its `Corrupt/` folder still reflects the old, wrong
  verdicts.

## Building it

There is no download for this branch. To run it:

```bash
pip3 install -r requirements.txt
python3 photo_organizer.py
```
