"""Gateway entrypoint - data plane on configurable HTTP and PG ports.

uvloop is mandatory per architecture; we install it explicitly and pass
``loop='uvloop'`` to uvicorn so a missing uvloop fails fast rather than
silently degrading to the default selector loop.
"""

import logging
import os

import uvicorn
import uvloop

from interlock.config import load_config
from interlock.gateway.app import create_app

logger = logging.getLogger(__name__)


def main() -> None:
    uvloop.install()

    config = load_config()
    app = create_app(config)

    # Respect $PORT if set (preview tooling, container platforms, etc.)
    # so the runner can assign an auto-port. Falls back to the configured
    # http_port when unset.
    http_port = int(os.environ.get("PORT", config.gateway.http_port))

    logger.info(
        "Starting gateway: HTTP on :%d, PG proxy on :%d",
        http_port,
        config.gateway.pg_port,
    )

    uvicorn.run(
        app,
        host=config.gateway.host,
        port=http_port,
        loop="uvloop",
    )


if __name__ == "__main__":
    main()
