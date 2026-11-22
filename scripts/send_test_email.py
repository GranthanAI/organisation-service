"""Send ONE plain test email through the configured SMTP provider and print the outcome. For the launch checklist: run it once with
the production settings (EMAIL_SENDER=smtp, SMTP_HOST, SMTP_*, EMAIL_FROM) before the first real invite.

    uv run python -m scripts.send_test_email --to someone@example.com

It uses the same `SmtpEmailSender` the invite job uses (TLS or STARTTLS as configured, a 10 s timeout) and never runs in tests. The
message says it is a test; the address, host and password are never printed.
"""

import argparse
import asyncio
import sys
import time

from app.core.config import settings
from app.email.sender import OutgoingEmail, SmtpEmailSender


async def main(to: str) -> int:
    if not settings.SMTP_HOST.strip():
        print("FAIL: SMTP_HOST is not set (EMAIL_SENDER=smtp needs it)")
        return 2
    message = OutgoingEmail(
        to=to, subject="GranthanAI test email (organisation-service)",
        text="This is a test message from organisation-service, sent to check that invite emails can leave the building.\n"
             "If you did not expect it, you can ignore it.\n")
    started = time.perf_counter()
    try:
        await SmtpEmailSender().send(message)
    except Exception as exc:                                          # the point of the script: say why it failed
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    print(f"OK: the provider accepted the message in {time.perf_counter() - started:.1f} s "
          f"(from {settings.EMAIL_FROM}, port {settings.SMTP_PORT}, "
          f"{'SSL' if settings.SMTP_USE_SSL else 'STARTTLS' if settings.SMTP_STARTTLS else 'no TLS'}). Check the inbox and the spam folder.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", required=True, help="the address to send the test to")
    sys.exit(asyncio.run(main(parser.parse_args().to)))
