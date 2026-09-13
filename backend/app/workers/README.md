# Worker layer

The worker will execute durable background jobs such as image extraction and image editing.

The initial design uses a single local process polling SQLite-backed jobs. No Redis/Celery is required.

Handlers receive the claimed Job identity plus its persisted payload. They run
after the claim transaction is committed and closed. Raising `PermanentJobError`
marks the Job failed immediately; other errors use the existing bounded retry
schedule.
