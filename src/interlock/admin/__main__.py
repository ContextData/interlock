"""Admin API entrypoint - management plane on configurable port."""

import os

import uvicorn
import uvloop

from interlock.admin.app import create_app
from interlock.config import load_config


def main() -> None:
    uvloop.install()
    config = load_config()
    app = create_app(config)
    port = int(os.environ.get("PORT", config.admin.port))
    host = os.environ.get("HOST", config.admin.host)
    uvicorn.run(app, host=host, port=port, loop="uvloop")


if __name__ == "__main__":
    main()
