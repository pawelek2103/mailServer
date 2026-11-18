#!/usr/bin/env python3
"""Receive-only SMTP server.

Accepts inbound mail for configured domains only (never relays), stores every
message in SQLite including the raw bytes, and optionally pushes each message
to a webhook as signed JSON. A small HTTP API lets other scripts poll for
messages as an alternative to webhooks.

Configuration is via environment variables; see DEPLOY.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mailbox
import os
import re
import sqlite3
import ssl
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

import aiohttp
from aiohttp import web
from aiosmtpd.controller import Controller

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mailreceiver")

MAIL_DOMAINS = {d.strip().lower() for d in os.environ.get("MAIL_DOMAINS", "").split(",") if d.strip()}
SMTP_HOST = os.environ.get("SMTP_HOST", "0.0.0.0")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT", "8080"))
API_TOKEN = os.environ.get("API_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_RETRIES = int(os.environ.get("WEBHOOK_RETRIES", "5"))
TLS_CERT = os.environ.get("TLS_CERT", "")
TLS_KEY = os.environ.get("TLS_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "mail.db")
ATTACH_DIR = Path(os.environ.get("ATTACH_DIR", "attachments"))
# If set, every message is also delivered here in Maildir format so an IMAP
# server (Dovecot) can serve it to mail clients.
MAILDIR_PATH = os.environ.get("MAILDIR_PATH", "")
MAX_MESSAGE_SIZE = int(os.environ.get("MAX_MESSAGE_SIZE", str(35 * 1024 * 1024)))
ATTACHMENT_B64_LIMIT = int(os.environ.get("ATTACHMENT_B64_LIMIT", str(1024 * 1024)))

# Optional: forward a copy of every message to Gmail (or any mailbox) via
# authenticated SMTP submission, e.g. smtp.gmail.com:587 with an app password.
FORWARD_TO = os.environ.get("FORWARD_TO", "")
FORWARD_SMTP_HOST = os.environ.get("FORWARD_SMTP_HOST", "")
FORWARD_SMTP_PORT = int(os.environ.get("FORWARD_SMTP_PORT", "587"))
FORWARD_SMTP_USER = os.environ.get("FORWARD_SMTP_USER", "")
FORWARD_SMTP_PASS = os.environ.get("FORWARD_SMTP_PASS", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    peer TEXT,
    mail_from TEXT,
    rcpt_tos TEXT NOT NULL,
    subject TEXT,
    message_id TEXT,
    text_body TEXT,
    html_body TEXT,
    headers TEXT NOT NULL,
    attachments TEXT NOT NULL,
    raw BLOB NOT NULL,
    webhook_attempts INTEGER NOT NULL DEFAULT 0,
    webhook_delivered INTEGER NOT NULL DEFAULT 0
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def extract_bodies(msg) -> tuple[str, str, list[dict]]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        filename = part.get_filename()
        if part.get_content_disposition() == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append({
                "filename": filename or "attachment",
                "content_type": ctype,
                "size": len(payload),
                "data": payload,
            })
        elif ctype == "text/plain":
            text_parts.append(part.get_content())
        elif ctype == "text/html":
            html_parts.append(part.get_content())
    return "\n".join(text_parts), "\n".join(html_parts), attachments


def open_maildir(path: str) -> mailbox.Maildir:
    # Maildir(create=True) only makes tmp/new/cur when the top dir does not
    # exist yet; create them explicitly so pre-created dirs work too.
    md = mailbox.Maildir(path, create=True)
    for sub in ("tmp", "new", "cur"):
        (Path(path) / sub).mkdir(exist_ok=True)
    return md


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[-100:] or "attachment"


def save_attachments(msg_db_id: int, attachments: list[dict]) -> list[dict]:
    meta = []
    folder = ATTACH_DIR / str(msg_db_id)
    for att in attachments:
        entry = {
            "filename": att["filename"],
            "content_type": att["content_type"],
            "size": att["size"],
        }
        if att["size"] <= ATTACHMENT_B64_LIMIT:
            entry["content_b64"] = base64.b64encode(att["data"]).decode()
        else:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / sanitize_filename(att["filename"])
            path.write_bytes(att["data"])
            entry["path"] = str(path)
        meta.append(entry)
    return meta


class SMTPHandler:
    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        domain = address.rsplit("@", 1)[-1].lower()
        if domain not in MAIL_DOMAINS:
            log.warning("rejected recipient %s (domain not allowed)", address)
            return "550 5.7.1 Relay denied"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        msg = BytesParser(policy=policy.default).parsebytes(envelope.content)
        text, html, attachments = extract_bodies(msg)
        headers = {k: str(v) for k, v in msg.items()}
        received_at = datetime.now(timezone.utc).isoformat()

        conn = db()
        try:
            cur = conn.execute(
                """INSERT INTO messages
                   (received_at, peer, mail_from, rcpt_tos, subject, message_id,
                    text_body, html_body, headers, attachments, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    received_at,
                    str(session.peer),
                    envelope.mail_from,
                    json.dumps(envelope.rcpt_tos),
                    str(msg.get("subject", "")),
                    str(msg.get("message-id", "")),
                    text,
                    html,
                    json.dumps(headers),
                    "[]",
                    sqlite3.Binary(envelope.content),
                ),
            )
            row_id = cur.lastrowid
            att_meta = save_attachments(row_id, attachments)
            conn.execute("UPDATE messages SET attachments = ? WHERE id = ?", (json.dumps(att_meta), row_id))
            conn.commit()
        finally:
            conn.close()

        log.info("stored message %s from=%s to=%s subject=%r",
                 row_id, envelope.mail_from, envelope.rcpt_tos, str(msg.get("subject", "")))

        if MAILDIR_PATH:
            try:
                open_maildir(MAILDIR_PATH).add(envelope.content)
            except Exception:
                log.exception("maildir delivery failed for message %s", row_id)

        if WEBHOOK_URL:
            payload = {
                "id": row_id,
                "received_at": received_at,
                "from": envelope.mail_from,
                "to": envelope.rcpt_tos,
                "subject": str(msg.get("subject", "")),
                "message_id": str(msg.get("message-id", "")),
                "text": text,
                "html": html,
                "headers": headers,
                "attachments": att_meta,
            }
            asyncio.create_task(deliver_webhook(row_id, payload))

        if FORWARD_TO and FORWARD_SMTP_HOST:
            asyncio.create_task(forward_copy(row_id, envelope.content))

        return "250 Message accepted for delivery"


def _forward_sync(raw: bytes) -> None:
    import smtplib
    with smtplib.SMTP(FORWARD_SMTP_HOST, FORWARD_SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(FORWARD_SMTP_USER, FORWARD_SMTP_PASS)
        s.sendmail(FORWARD_SMTP_USER, [FORWARD_TO], raw)


FORWARD_RETRIES = int(os.environ.get("FORWARD_RETRIES", "3"))


async def forward_copy(row_id: int, raw: bytes) -> None:
    for attempt in range(1, FORWARD_RETRIES + 1):
        try:
            await asyncio.to_thread(_forward_sync, raw)
            log.info("forwarded copy of message %s to %s", row_id, FORWARD_TO)
            return
        except Exception:
            log.exception("forwarding copy of message %s to %s failed (attempt %s)",
                          row_id, FORWARD_TO, attempt)
        if attempt < FORWARD_RETRIES:
            await asyncio.sleep(60 * attempt)
    log.error("giving up forwarding message %s; it remains in the DB and maildir", row_id)


async def deliver_webhook(row_id: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SECRET:
        sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Signature-256"] = "sha256=" + sig

    async with aiohttp.ClientSession() as session:
        for attempt in range(1, WEBHOOK_RETRIES + 1):
            try:
                async with session.post(
                    WEBHOOK_URL, data=body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status < 300:
                        conn = db()
                        try:
                            conn.execute(
                                "UPDATE messages SET webhook_attempts = ?, webhook_delivered = 1 WHERE id = ?",
                                (attempt, row_id),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                        log.info("webhook delivered for message %s (attempt %s)", row_id, attempt)
                        return
                    log.warning("webhook HTTP %s for message %s (attempt %s)", resp.status, row_id, attempt)
            except Exception as exc:
                log.warning("webhook error for message %s (attempt %s): %s", row_id, attempt, exc)
            conn = db()
            try:
                conn.execute("UPDATE messages SET webhook_attempts = ? WHERE id = ?", (attempt, row_id))
                conn.commit()
            finally:
                conn.close()
            if attempt < WEBHOOK_RETRIES:
                await asyncio.sleep(min(2 ** attempt * 5, 300))
    log.error("webhook delivery failed permanently for message %s; it remains in the DB", row_id)


def row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "received_at": row["received_at"],
        "from": row["mail_from"],
        "to": json.loads(row["rcpt_tos"]),
        "subject": row["subject"],
        "message_id": row["message_id"],
        "text": row["text_body"],
        "html": row["html_body"],
        "headers": json.loads(row["headers"]),
        "attachments": json.loads(row["attachments"]),
        "webhook_delivered": bool(row["webhook_delivered"]),
    }


@web.middleware
async def auth_middleware(request, handler):
    if request.path == "/health":
        return await handler(request)
    if not API_TOKEN:
        return web.json_response({"error": "API_TOKEN not configured"}, status=503)
    if request.headers.get("Authorization") != f"Bearer {API_TOKEN}":
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def health(request):
    return web.json_response({"ok": True})


async def list_messages(request):
    since_id = int(request.query.get("since_id", "0"))
    limit = min(int(request.query.get("limit", "50")), 200)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT ?", (since_id, limit)
        ).fetchall()
    finally:
        conn.close()
    return web.json_response({"messages": [row_to_dict(r) for r in rows]})


async def get_message(request):
    conn = db()
    try:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (int(request.match_info["id"]),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(row_to_dict(row))


async def redeliver(request):
    conn = db()
    try:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (int(request.match_info["id"]),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    if not WEBHOOK_URL:
        return web.json_response({"error": "WEBHOOK_URL not configured"}, status=503)
    payload = row_to_dict(row)
    payload["headers"] = json.loads(row["headers"])
    asyncio.create_task(deliver_webhook(row["id"], payload))
    return web.json_response({"queued": True})


def build_ssl_context() -> ssl.SSLContext | None:
    if not (TLS_CERT and TLS_KEY):
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(TLS_CERT, TLS_KEY)
    return ctx


def main() -> None:
    if not MAIL_DOMAINS:
        raise SystemExit("Set MAIL_DOMAINS to a comma-separated list of recipient domains to accept")
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    init_db()
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    if MAILDIR_PATH:
        open_maildir(MAILDIR_PATH)

    tls_context = build_ssl_context()

    controller = Controller(
        SMTPHandler(),
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        tls_context=tls_context,
        require_starttls=False,
        data_size_limit=MAX_MESSAGE_SIZE,
    )
    controller.start()
    log.info("SMTP listening on %s:%s accepting: %s (STARTTLS: %s)",
             SMTP_HOST, SMTP_PORT, ", ".join(sorted(MAIL_DOMAINS)), bool(tls_context))

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/health", health)
    app.router.add_get("/messages", list_messages)
    app.router.add_get("/messages/{id:\\d+}", get_message)
    app.router.add_post("/messages/{id:\\d+}/redeliver", redeliver)

    if tls_context is None and API_TOKEN:
        log.warning("API is plain HTTP; the bearer token is sent in the clear. Set TLS_CERT/TLS_KEY.")
    web.run_app(app, host=API_HOST, port=API_PORT, ssl_context=tls_context, print=None)


if __name__ == "__main__":
    main()
