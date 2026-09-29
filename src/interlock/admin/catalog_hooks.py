"""What the admin does to the source catalog when a source changes.

Every path that stores a source - the API's create and update, the console's
create and edit forms, re-enabling a source, and the wizard - calls
`after_source_saved`. A scan is queued for the worker; the save never waits for
it and never fails because of it.
"""

from __future__ import annotations

from typing import Any

from interlock.catalog.queue import enqueue_after_save


async def after_source_saved(request: Any, source_id: str) -> int | None:
    """Queue a catalog scan for `source_id`, if the catalog is on."""
    config = getattr(request.app.state, "config", None)
    catalog = getattr(config, "catalog", None)
    if catalog is not None and not (catalog.enabled and catalog.scan_on_save):
        return None
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        return None
    admin = getattr(request.state, "admin", None)
    return await enqueue_after_save(pool, source_id, requested_by=getattr(admin, "username", None))
