# Worker layer

The worker will execute durable background jobs such as image extraction and image editing.

The initial design uses a single local process polling SQLite-backed jobs. No Redis/Celery is required.
