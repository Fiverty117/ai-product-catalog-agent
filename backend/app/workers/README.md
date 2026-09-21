# Worker layer

The worker will execute durable background jobs such as image extraction and image editing.

The initial design uses a single local process polling SQLite-backed jobs. No Redis/Celery is required.

Handlers receive the claimed Job identity plus its persisted payload. They run
after the claim transaction is committed and closed. Raising `PermanentJobError`
marks the Job failed immediately; other errors use the existing bounded retry
schedule.

For the local Product Copy editorial queue, set `OPENAI_API_KEY` and run from
`backend`:

```powershell
python -m app.scripts.run_product_copy_worker
```

This process continuously polls only `product.copy.v1` Jobs, so it cannot claim
unrelated queued work. Use `--once` for a single claim attempt during a smoke
test.

For an explicit real category-suggestion smoke attempt after migrations are at
head, set `OPENAI_API_KEY` and run from `backend`:

```powershell
python -m app.scripts.manual_openai_category_suggestion --product-id <PRODUCT_UUID>
```

The script uses the trusted snapshot/enqueue path and durable worker. Add
`--requeue-failed` only to retry the same failed logical Job when its existing
attempt budget has not been exhausted.

For one explicit real image-enhancement smoke attempt, set `OPENAI_API_KEY` and
optionally `OPENAI_IMAGE_MODEL`, then run from `backend`:

```powershell
python -m app.scripts.manual_openai_image_enhancement --photo-id <PHOTO_UUID>
```

This uses the `image.enhance.v1` durable Job, verifies the registered original
Photo, calls the real image-edit provider outside a database transaction, and
stores validated output under `storage/processed`. Add `--requeue-failed` only
to requeue the same failed logical Job while its existing attempt budget allows
another attempt.
