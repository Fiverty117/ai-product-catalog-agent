# MPO source compatibility

`Photo.mime_type`, checksum, size, dimensions and path describe the immutable
uploaded source. MPO is stored as `image/mpo` at
`originals/<prefix>/<original-sha256>.mpo`; the displayed filename is unchanged.
One upload remains one Photo, including after Product promotion.

The shared `resolve_photo_for_processing` boundary first verifies the original
bytes and metadata. JPEG/PNG/WebP remain byte-identical. MPO uses Pillow's
default frame 0 (the Baseline MP Primary Image in the fixture), never a frame
merge. EXIF transpose is applied to pixels, then RGB conversion and JPEG
encoding at quality 95, subsampling 0, optimize false and progressive false.
No resize/upscale occurs. Transparency, if present, is flattened onto white.
Orientation/MPF metadata is not copied to the processing JPEG.

JPEG bytes are stored at `normalized/<prefix>/<jpeg-sha256>.jpg`. An immutable
index at `normalized/index/image-processing-v1/<prefix>/<source-sha256>.json`
records the source/version-to-JPEG mapping. Repeated resolution verifies and
reuses that JPEG without re-encoding. The index is not a database entity or a
user-visible Photo; normalization is not an AI DerivedImage. Bump the processing
version if encoding/selection semantics change. Frozen catalogs retain the
actual JPEG hash/path, independent of any future resolver version.

Intake and Product previews, extraction and enhancement use processing assets.
Jobs/runs continue to reference the original Photo, with original hashes used
for source identity. CatalogSnapshot preserves `source_original_asset` as MPO
and freezes the ordinary JPEG in `presentation_asset`. `presentation_type`
remains `original` (the user's source selection, not an AI enhancement); only
MPO sources may use `normalized/` for this selection. The renderer verifies and
embeds the frozen JPEG, never decodes MPO or resolves live Photo rows. Historical
JPEG/PNG/WebP payloads and hashes are unchanged.

The 12-photo/20-MB Intake limits remain. Validation checks actual decoded format,
container bounds and all MPO frames sequentially, with Pillow decompression-bomb
protections; malformed MP-index fallback warnings are errors. Processing/output
validation still rejects raw MPO and all previously unsupported formats. Invalid
uploads fail before creating a Photo or normalized files. Existing storage
semantics still allow recoverable immutable files after a later DB failure; do
not delete shared content-addressed assets automatically.

Migration `20261005_0028` follows head `20261004_0027`. It replaces only the Photo
MIME CHECK, permitting `image/mpo` only when `is_original = 1`. Alembic batch
recreation follows the FK-toggle convention of 0016 and checks foreign-key
integrity afterward. Ordinary rows, ownership, timestamps and dependent rows
are preserved. Downgrade explicitly fails if MPO Photos exist; it never deletes
them. No extra dependency or codec is installed.

## Real smoke (user-controlled)

1. Stop the application with the existing `stop.ps1`, then run `start.ps1`.
   The existing launcher applies `alembic upgrade head` before starting services.
2. Open Product Intake and upload the unconverted `IMG_0223.JPEG` and
   `IMG_0224.JPEG`. There should be no unsupported-MPO error, exactly two source
   Photos, the original display filenames and correctly oriented previews.
3. Run extraction and review the result. This is a real AI action and can incur
   its normal cost; automated tests do not perform it.
4. Do not promote if doing so would duplicate an existing Product. On an
   intentionally safe test Product, optionally verify enhancement accepts its
   MPO-origin source. Do not delete database rows or source files manually.
5. Confirm previous Catalog Builds/PDFs remain unchanged. Normalization has no
   historical rewrite or automatic re-render operation.
