# Worker layer

The worker will execute durable background jobs such as image extraction and image editing.

The initial design uses a single local process polling SQLite-backed jobs. No Redis/Celery is required.

Handlers receive the claimed Job identity plus its persisted payload. They run
after the claim transaction is committed and closed. Raising `PermanentJobError`
marks the Job failed immediately; other errors use the existing bounded retry
schedule.

For an explicit real category-suggestion smoke attempt after migrations are at
head, set `OPENAI_API_KEY` and run from `backend`:

```powershell
python -m app.scripts.manual_openai_category_suggestion --product-id <PRODUCT_UUID>
```

The script uses the trusted snapshot/enqueue path and durable worker. Add
`--requeue-failed` only to retry the same failed logical Job when its existing
attempt budget has not been exhausted.
