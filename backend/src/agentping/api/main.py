"""`agentping-api`: run the API with uvicorn."""

from __future__ import annotations

import os

import uvicorn

from agentping.api.app import create_app
from agentping.config import get_settings
from agentping.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("API_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", os.environ.get("API_PORT", "8000"))),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
