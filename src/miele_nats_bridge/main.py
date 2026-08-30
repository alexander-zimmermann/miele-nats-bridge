"""Entry point: wire config, metrics, auth, client, publisher and bridge; handle signals."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import time

import httpx
from nats_bridge_core import Publisher
from nats_bridge_core import configure as configure_logging
from nats_bridge_core import serve as serve_metrics
from nats_bridge_core import watchdog_ok as logger_watchdog_ok

from .auth import ConsentRequiredError, TokenManager
from .bridge import MieleBridge
from .client import MieleClient
from .config import Settings
from .metrics import Metrics

logger = logging.getLogger(__name__)

async def _amain() -> int:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    logger.info("miele-nats-bridge starting")

    appliances = settings.load_appliances()
    logger.info("config: %d appliance(s)", len(appliances))
    for appliance in appliances:
        logger.info(
            "config: appliance=%s device_id=%s model=%s state_subject=%s",
            appliance.name,
            appliance.device_id,
            appliance.model or "-",
            appliance.state_subject,
        )

    metrics = Metrics()
    publisher = Publisher(settings, metrics)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    def is_healthy() -> bool:
        # Cloud reachability is deliberately NOT part of liveness: a Miele outage
        # should page via miele_connected, not restart-loop the pod.
        if not publisher.is_connected:
            return False
        return logger_watchdog_ok(time.monotonic())

    http_server = await serve_metrics(metrics.registry, settings.metrics_port, is_healthy)

    bridge: MieleBridge | None = None
    async with httpx.AsyncClient(http2=False) as http:
        try:
            # Reads the credential files; a missing or empty one is a
            # configuration error and should fail startup, not run degraded.
            tokens = TokenManager(settings, metrics, http)
            client = MieleClient(settings, metrics, tokens, http)
            bridge = MieleBridge(settings, appliances, client, publisher, metrics)

            await publisher.connect()
            bridge_task = await bridge.start()
            logger.info("bridge is up (%d appliance(s))", len(appliances))

            # Wait for either a shutdown signal or the stream loop giving up, so
            # a non-retryable failure ends the process instead of idling here.
            stop_task = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait(
                {stop_task, bridge_task}, return_when=asyncio.FIRST_COMPLETED
            )
            stop_task.cancel()
            if bridge_task in done:
                bridge_task.result()
        except ConsentRequiredError:
            # Restarting cannot help: the stored refresh token is dead and only a
            # new consent round can produce a working one.
            logger.exception("refresh token rejected, a new consent round is required")
            return 1
        except Exception:
            logger.exception("fatal error in bridge startup/run")
            return 1
        finally:
            logger.info("shutting down")
            if bridge is not None:
                with contextlib.suppress(Exception):
                    await bridge.stop()
            try:
                await publisher.close()
            except Exception:
                logger.exception("error closing NATS publisher")
            http_server.close()
            with contextlib.suppress(Exception):
                await http_server.wait_closed()

    return 0


def run() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    run()
