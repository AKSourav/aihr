import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.settings import get_settings

logger = logging.getLogger(__name__)

DISPLAY_TZ = ZoneInfo("Asia/Kolkata")

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "prompts" / "email"),
    autoescape=select_autoescape(enabled_extensions=("html.j2",), default_for_string=False),
)


@dataclass
class Invite:
    to_email: str
    candidate_first_name: str
    company_name: str
    job_title: str
    join_url: str
    expires_at: datetime
    max_minutes: int
    # A re-sent invite (earlier link unused, or the candidate asked for a callback).
    reminder: bool = False


def render_invite(invite: Invite) -> EmailMessage:
    context = {
        "first_name": invite.candidate_first_name,
        "company_name": invite.company_name,
        "job_title": invite.job_title,
        "join_url": invite.join_url,
        "expires_at": invite.expires_at.astimezone(DISPLAY_TZ).strftime("%d %b %Y, %I:%M %p IST"),
        "max_minutes": invite.max_minutes,
        "reminder": invite.reminder,
    }
    message = EmailMessage()
    subject = f"{invite.company_name}: screening call for {invite.job_title}"
    message["Subject"] = f"Reminder: {subject}" if invite.reminder else subject
    message["From"] = get_settings().smtp_from
    message["To"] = invite.to_email
    message.set_content(_env.get_template("invite.txt.j2").render(**context))
    message.add_alternative(_env.get_template("invite.html.j2").render(**context), subtype="html")
    return message


def send_email(message: EmailMessage) -> None:
    settings = get_settings()
    if not settings.smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)

    logger.info("email sent", extra={"subject": message["Subject"]})
