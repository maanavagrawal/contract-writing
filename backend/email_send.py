"""
Thin Resend wrapper for transactional email.

We keep the API surface tiny: send(to, subject, html) returns nothing on
success or raises EmailSendError. Callers don't reach into the resend SDK
directly so swapping providers later is one file's worth of work.

In dev (no RESEND_API_KEY) we log to stdout instead of raising — that lets
end-to-end tests run without a real Resend account, while production failures
surface as 503 to the user.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    """Resend rejected the request, returned an error, or the SDK blew up.
    Maps to 503 in the auth handler."""


def _from_address() -> str:
    """The 'From:' header. Must be on a Resend-verified domain or delivery
    fails. Falls back to the resend.dev sandbox in dev so local testing works
    without DNS setup."""
    return os.environ.get("EMAIL_FROM", "memoir <onboarding@resend.dev>")


def send(to: str, subject: str, html: str) -> None:
    """Send a transactional email. Raises EmailSendError on failure.

    No retries here — the auth handler chooses whether to retry or surface
    a 503 to the user. Resend's own SDK has client-side retries but those
    are for transient network errors, not API-level rejections.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        # Dev mode: log the email instead of sending. Lets local testing
        # exercise the full magic-link flow without a real Resend account.
        logger.warning(
            "RESEND_API_KEY not set; would have sent to %s subject=%r body=%s",
            to, subject, html[:200],
        )
        return

    try:
        import resend
    except ImportError as e:
        raise EmailSendError(f"resend SDK missing: {e}")

    resend.api_key = api_key
    try:
        resend.Emails.send({
            "from": _from_address(),
            "to": [to],
            "subject": subject,
            "html": html,
        })
    except Exception as e:
        # Resend SDK raises a few different exception types depending on
        # the failure mode; we don't care about the distinction here.
        raise EmailSendError(f"resend send failed: {e}")
