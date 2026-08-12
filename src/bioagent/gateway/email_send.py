"""Outbound email — a thin, pluggable SMTP sender with a dev/log fallback.

Sending email is free; the only choice is which relay you send THROUGH. The UCI deployment
sends through **UCI SER** (Secure Email Relay, a Proofpoint service) at
``smtp-us.ser.proofpoint.com``: an authenticated relay that DKIM-signs outbound mail so it
passes DMARC and delivers to any address (gmail.com, etc.), not just ``@uci.edu``. The From
address must still be a ``uci.edu`` address. Point the env vars below at SER — no third-party
paid service is needed. (An IP-allowed on-campus relay like ``smtp.uci.edu`` with no auth
also works if you have one; just omit USER/PASSWORD.)

Config (all optional):
- ``BIOAGENT_SMTP_HOST``      SMTP server hostname (SER: ``smtp-us.ser.proofpoint.com``). If
                              UNSET, the sender runs in DEV mode: it logs the message (incl.
                              the code) to stdout → the systemd journal instead of sending, so
                              the registration flow is fully testable before SMTP is wired.
- ``BIOAGENT_SMTP_PORT``      default 587 (STARTTLS) — SER also accepts 25; use 465 for a relay
                              that wants implicit TLS.
- ``BIOAGENT_SMTP_USER`` / ``BIOAGENT_SMTP_PASSWORD``  relay auth (SER: the "Relay User ID"
                              GUID + its password). Omit both for an IP-allowed on-campus relay.
- ``BIOAGENT_SMTP_FROM``      envelope/From address; must be a ``uci.edu`` address (default
                              ``AiScientist <no-reply@uci.edu>``).
- ``BIOAGENT_SMTP_TLS``       ``starttls`` (default) | ``ssl`` | ``none``. SER requires TLS;
                              ``starttls`` negotiates TLS 1.2+.

No external API, no dependency beyond the stdlib ``smtplib``/``email``.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def smtp_configured() -> bool:
    """True when a real SMTP host is set — else the sender is in dev/log mode."""
    return bool(os.environ.get("BIOAGENT_SMTP_HOST", "").strip())


def email_mode() -> str:
    """'smtp' when a relay is configured, else 'dev' (codes are logged, not emailed)."""
    return "smtp" if smtp_configured() else "dev"


def default_from() -> str:
    return os.environ.get("BIOAGENT_SMTP_FROM", "AiScientist <no-reply@uci.edu>").strip()


def send_email(to: str, subject: str, body: str) -> bool:
    """Send a plaintext email. Returns True on success.

    In DEV mode (no ``BIOAGENT_SMTP_HOST``) this does NOT send — it prints the message to
    stdout (→ journal) and returns True, so the caller's flow proceeds and an operator can
    read the code from the logs. Any real SMTP error returns False (the caller surfaces a
    friendly message); it never raises.
    """
    msg = EmailMessage()
    msg["From"] = default_from()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    if not smtp_configured():
        # Dev/log fallback: make the code trivially findable in `journalctl -u bioagent`.
        print(f"[email:dev] (SMTP not configured) would send to {to}\n"
              f"    Subject: {subject}\n"
              + "\n".join(f"    {line}" for line in body.splitlines()))
        return True

    host = os.environ["BIOAGENT_SMTP_HOST"].strip()
    port = int(os.environ.get("BIOAGENT_SMTP_PORT", "587"))
    user = os.environ.get("BIOAGENT_SMTP_USER", "").strip() or None
    password = os.environ.get("BIOAGENT_SMTP_PASSWORD", "") or None
    tls = os.environ.get("BIOAGENT_SMTP_TLS", "starttls").strip().lower()

    try:
        if tls == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20,
                                                    context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            if tls == "starttls":
                server.starttls(context=ssl.create_default_context())
        with server:
            if user and password:
                server.login(user, password)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        print(f"[email] send to {to} failed: {type(exc).__name__}: {exc}")
        return False
