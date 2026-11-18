#!/usr/bin/env python3
"""Send a test message to the receiver (default localhost:2525).

Usage: python send_test.py [host] [port] [to-address]
"""

import smtplib
import sys
from email.message import EmailMessage

host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 2525
to_addr = sys.argv[3] if len(sys.argv) > 3 else "test@example.com"

msg = EmailMessage()
msg["From"] = "someone@web.de"
msg["To"] = to_addr
msg["Subject"] = "receiver test"
msg.set_content("plain text body")
msg.add_alternative("<p>html body</p>", subtype="html")
msg.add_attachment(b"hello attachment", maintype="text", subtype="plain", filename="hello.txt")

with smtplib.SMTP(host, port) as s:
    s.send_message(msg)
print(f"sent to {to_addr} via {host}:{port}")
