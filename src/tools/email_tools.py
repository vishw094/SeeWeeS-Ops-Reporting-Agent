from __future__ import annotations
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_email_smtp(subject: str, html_body: str, to_email: str) -> None:
    """
    Generic SMTP sender. Works with Zoho if env vars are set:
      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD
    """
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

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
