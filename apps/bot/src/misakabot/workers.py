from __future__ import annotations

import asyncio
import logging

from .gateway import AiogramGateway
from .onboarding import OnboardingService

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

async def verification_expiry_worker(
    onboarding: OnboardingService,
    gateway: AiogramGateway | None = None,
    interval_seconds: int = 30,
) -> None:
    """Keep join-request deadlines enforced even when nobody opens the verification page."""
    try:
        while True:
            try:
                await onboarding.expire_pending_verifications()
            except Exception:
                # A transient database or Telegram failure must not silently kill
                # join-request expiry.
                logger.exception("verification_expiry_worker.expiry_iteration_failed")
            if gateway is not None:
                try:
                    await gateway.process_scheduled_message_deletions()
                except Exception:
                    # Deletion queue and webhook retention are independent of an
                    # onboarding failure and must still receive their next retry.
                    logger.exception("verification_expiry_worker.housekeeping_failed")
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        raise
