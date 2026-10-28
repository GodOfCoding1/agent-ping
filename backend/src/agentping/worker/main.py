"""`agentping-worker`: standalone outbox worker process."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal

from agentping.bootstrap import build_runtime, listen_url
from agentping.config import get_settings
from agentping.logging import configure_logging
from agentping.push.base import close_push
from agentping.worker.runner import Worker

logger = logging.getLogger(__name__)


async def _run(once: bool) -> None:
    settings = get_settings()
    runtime, engine = build_runtime(settings)
    worker = Worker(runtime)
    try:
        if once:
            await worker.ensure_maintenance()
            processed = await worker.drain()
            print(
                json.dumps(
                    {"processed": processed, "next_run_in_seconds": await worker.next_delay()}
                )
            )
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def request_stop() -> None:
            stop.set()
            runtime.signal.notify()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_stop)
        await worker.run_forever(stop, listen_url=listen_url(settings))
    finally:
        await engine.dispose()
        await close_push(runtime.push)


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentPing outbox worker")
    parser.add_argument(
        "--once", action="store_true", help="Process all currently due jobs, then exit"
    )
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    asyncio.run(_run(args.once))


if __name__ == "__main__":
    main()
