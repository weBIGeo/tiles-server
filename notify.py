import logging
import smtplib
import urllib.request
import config
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def _send_mail(subject: str, body: str) -> None:
    if not getattr(config, "smtp_host", None) or not getattr(config, "notify_email", None):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = getattr(config, "smtp_user", config.notify_email)
        msg["To"] = config.notify_email
        msg.set_content(body)
        with smtplib.SMTP(config.smtp_host, getattr(config, "smtp_port", 587)) as smtp:
            smtp.starttls()
            user = getattr(config, "smtp_user", None)
            password = getattr(config, "smtp_password", None)
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        logger.debug(f"Email notification sent: {subject}")
    except Exception as e:
        logger.warning(f"Failed to send email notification: {e}")


def _send_ntfy(subject: str, body: str) -> None:
    topic = getattr(config, "ntfy_topic", None)
    if not topic:
        return
    url = f"{getattr(config, 'ntfy_server', 'https://ntfy.sh')}/{topic}"
    try:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={"Title": subject, "Content-Type": "text/plain"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        logger.debug(f"ntfy notification sent: {subject}")
    except Exception as e:
        logger.warning(f"Failed to send ntfy notification: {e}")


def notify(subject: str, body: str) -> None:
    _send_mail(subject, body)
    _send_ntfy(subject, body)
