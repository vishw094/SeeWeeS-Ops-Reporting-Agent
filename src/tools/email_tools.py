from __future__ import annotations
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional


def send_email_smtp(
    subject: str,
    html_body: str,
    to_email: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """
    Generic SMTP sender. Works with Zoho if env vars are set:
      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

    attachments: optional list of {"filename", "mime_type", "content"} dicts.
    """
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_email

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    for att in attachments or []:
        maintype, _, subtype = (att.get("mime_type", "text/plain")).partition("/")
        part = MIMEApplication(att.get("content", "").encode("utf-8"), _subtype=subtype or "plain")
        part.add_header("Content-Disposition", "attachment", filename=att.get("filename", "attachment.txt"))
        msg.attach(part)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                server.login(user, password)
                server.sendmail(user, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls()
                server.login(user, password)
                server.sendmail(user, [to_email], msg.as_string())
        print(f"[Email] Report sent to {to_email}")
    except smtplib.SMTPAuthenticationError:
        print("[Email] SMTP authentication failed — check SMTP_USER / SMTP_PASSWORD in .env. Report not sent.")
    except smtplib.SMTPException as e:
        print(f"[Email] SMTP error, report not sent: {e}")
    except (TimeoutError, ConnectionError, OSError) as e:
        # Covers WinError 10060 (timeout), DNS failures, network unreachable, etc.
        print(f"[Email] Network error, report not sent: {e}")
