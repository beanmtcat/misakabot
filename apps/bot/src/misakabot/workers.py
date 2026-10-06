from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .gateway import AiogramGateway
from .onboarding import OnboardingService

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_BACKGROUND_RETRY_DELAYS_SECONDS = (0.5, 1.5)


async def _run_background_operation(
    name: str,
    operation: Callable[[], Awaitable[object]],
    retry_delays: tuple[float, ...] = _BACKGROUND_RETRY_DELAYS_SECONDS,
) -> bool:
    """Run an idempotent worker operation with bounded transient retries."""
    for attempt in range(len(retry_delays) + 1):
        try:
            await operation()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if attempt >= len(retry_delays):
                logger.warning(
                    "verification_expiry_worker.%s_deferred attempts=%s reason=%s",
                    name,
                    attempt + 1,
                    error,
                )
                return False
            delay = retry_delays[attempt]
            logger.warning(
                "verification_expiry_worker.%s_retry attempt=%s delay=%.2f reason=%s",
                name,
                attempt + 1,
                delay,
                error,
            )
            await asyncio.sleep(delay)
    return False

async def verification_expiry_worker(
    onboarding: OnboardingService,
    gateway: AiogramGateway | None = None,
    interval_seconds: int = 30,
) -> None:
    """Keep join-request deadlines enforced even when nobody opens the verification page."""
    try:
        while True:
            await _run_background_operation(
                "expiry_iteration", onboarding.expire_pending_verifications
            )
            if gateway is not None:
                # Deletion queue and webhook retention are independent of an
                # onboarding failure and must still receive their next retry.
                await _run_background_operation(
                    "housekeeping", gateway.process_scheduled_message_deletions
                )
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        raise
