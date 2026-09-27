# Image orientation and MPO source compatibility

`Photo.mime_type`, checksum, size, dimensions and path describe the immutable
uploaded source. MPO is stored as `image/mpo` at
`originals/<prefix>/<original-sha256>.mpo`; the displayed filename is unchanged.
One upload remains one Photo, including after Product promotion.

The shared `resolve_photo_for_processing` boundary first verifies the original
bytes and metadata. JPEG/PNG/WebP with absent or identity container-native EXIF
Orientation remain byte-identical, avoiding needless recompression. A
non-identity or malformed value creates a content-addressed processing
derivative in the same format. The transform is applied once to pixels and
metadata is not copied, so preview, AI providers and catalogs do not need to
interpret Orientation.

PNG requires an explicit metadata distinction. Pillow can synthesize EXIF
Orientation from `tiff:Orientation` inside XMP even when the PNG has no `eXIf`
chunk. Windows can produce a PNG whose pixels are already physically oriented
while retaining the source JPEG's stale Orientation in XMP; Explorer and normal
browser display use the raw PNG pixels. For an XMP-only PNG, the resolver keeps
the raw pixel orientation and creates a metadata-clean PNG derivative. A real
PNG `eXIf` chunk remains authoritative and is physically applied.

MPO uses Pillow's default frame 0 (the Baseline MP Primary Image in the fixture),
never a frame merge. The primary frame is physically oriented, converted to RGB
and encoded as JPEG at quality 95, subsampling 0, optimize false and progressive
false. No resize/upscale occurs. JPEG/MPO transparency, if present, is flattened
onto white; PNG/WebP derivatives retain their supported alpha channel.
Orientation and MPF metadata are not copied to processing assets.

If pixels are already physically sideways while Orientation is absent or `1`,
there is no deterministic metadata signal from which to infer a corrective
rotation. Those bytes remain unchanged. A Windows save that physically rotates
the pixels and writes identity Orientation is already canonical and likewise
needs no further transform; guessing from aspect ratio or product content is not
part of this deterministic boundary.

Processing bytes are stored at
`normalized/<prefix>/<processing-sha256>.<ext>`. An immutable index at
`normalized/index/image-processing-v3/<prefix>/<source-sha256>.json` records the
source/version-to-asset mapping. v3 distinguishes container-native PNG `eXIf`
from XMP-only Orientation. Repeated resolution verifies and reuses the asset
without re-encoding. Valid v2 assets remain reusable whenever their semantics
are compatible; XMP-only PNG v2 entries are intentionally bypassed. Valid v1 MPO
JPEGs are also reused and receive a v3 index rather than being rewritten. The
index is not a database entity or a user-visible Photo; normalization is not an
AI DerivedImage. Frozen catalogs retain the actual processing hash/path,
independent of future resolver versions.

Intake and Product previews, extraction and enhancement all resolve the same
processing asset. Jobs/runs continue to reference the immutable original Photo,
with the original hash used for source identity. Enhancement provider output is
also decoded and orientation-canonicalized before it becomes a DerivedImage; it
does not inherit source EXIF.

Product Intake image URLs include the processing-contract version as a query
value, and the response uses endpoint-scoped `Cache-Control: no-cache`. A
contract change therefore selects a new URL while repeated requests are
revalidated. This does not disable caching globally or change frozen catalog
media.

CatalogSnapshot preserves the exact source in `source_original_asset` and freezes
the canonical bytes in `presentation_asset`. `presentation_type` remains
`original` when the user's source selection is represented by a normalized
asset. The renderer only verifies and embeds the frozen JPEG/PNG/WebP; it never
interprets EXIF, decodes MPO or resolves live Photo rows. Historical Snapshot,
Build and PDF payloads are not rewritten or re-rendered.

The 12-photo/20-MB Intake limits remain. Validation first proves the actual MPO
primary at offset 0 by fully loading its pixels with Pillow decompression-bomb
and decoded-pixel protections. That decoded primary is authoritative for
application usability. MPF entries are then inspected without allocating from
their declared sizes. Stale primary Size metadata and auxiliary offsets or sizes
that extend beyond EOF are logged and ignored; no seek is attempted for such an
auxiliary. Decodable auxiliaries retain the same pixel safety check, while an
in-bounds auxiliary whose JPEG payload does not decode is also logged and
ignored because no application semantic consumes it. Invalid primary pixels,
oversized primary pixels, corrupt ordinary images and primary-truncated files
still fail. When Pillow identifies a broken/stale MPF index as a baseline JPEG,
the JPEG is accepted only after both `verify()` and a fresh full pixel `load()`
succeed; it is then re-encoded to remove stale MPF and canonicalize orientation.
Decode logs record only the stage, detected format, frame number and exception
type, without paths or secrets. Processing/output validation still rejects raw
MPO and all previously unsupported formats. Invalid uploads fail before creating
a Photo or normalized files. Existing storage semantics still allow recoverable
immutable files after a later DB failure; do not delete shared content-addressed
assets automatically.

Migration `20261005_0028` follows head `20261004_0027`. It replaces only the Photo
MIME CHECK, permitting `image/mpo` only when `is_original = 1`. Alembic batch
recreation follows the FK-toggle convention of 0016 and checks foreign-key
integrity afterward. Ordinary rows, ownership, timestamps and dependent rows
are preserved. Downgrade explicitly fails if MPO Photos exist; it never deletes
them. No extra dependency or codec is installed.

## Real smoke (user-controlled)

1. Stop the application with the existing `stop.ps1`, then run `start.ps1`.
   The existing launcher applies `alembic upgrade head` before starting services.
2. Confirm the full filenames because both `IMG_0278.JPEG` and `IMG_0278.png`
   may exist while Explorer hides extensions. Upload the exact `IMG_0278.png`
   that previously produced a sideways Intake preview. Its raw portrait pixels
   should remain portrait while stale XMP Orientation is removed from the
   processing asset.
3. Upload `IMG_0278.JPEG` and `IMG_0279.JPEG` separately. Their native MPO/JPEG
   orientation behavior should remain correct, and the displayed filename must
   identify which variant was selected.
4. Recheck the unconverted MPO examples `IMG_0223.JPEG` and `IMG_0224.JPEG`.
   They should retain their displayed filenames, remain one Photo per upload and
   show the same normalized primary-frame behavior as before.
5. Run extraction and, on an intentionally safe Product, enhancement. Both must
   see the same orientation and no duplicate user-visible Photo should appear.
   Real calls can incur their normal cost; automated tests use fakes and make no
   OpenAI calls. Do not promote if doing so would duplicate an existing Product.
6. Confirm previous Catalog Builds/PDFs remain unchanged. Normalization has no
   historical rewrite or automatic re-render operation.
7. Upload `IMG_0258.JPEG` and `IMG_0257.JPEG` together. Each should remain one
   `image/mpo` source Photo with its exact original filename and bytes. Their
   malformed unused frame 1 must not show a decode error; preview, extraction
   and enhancement must use the Orientation=3-canonicalized frame 0.
