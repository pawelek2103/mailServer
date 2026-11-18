# Deploying the mail receiver

Receive-only SMTP server: accepts mail for your domains, stores it in SQLite,
and pushes it to your script via webhook (or your script polls the API).

Architecture: your domain's MX stays at Google. A Google Workspace routing
rule diverts the bot addresses to this server; everything else (your GSuite
mailbox, confirmations forwarded to Gmail) works exactly as before.

```
web.de → yourdomain.com (GSuite, MX unchanged)
         ├─ bot addresses → routed to the receiver → webhook → script
         └─ your mailbox   → Gmail as today
```

Two ways to host it: **A) Railway** (no VPS needed) or **B) any cheap VPS**.

## Option A: Railway

Since only GSuite connects to the receiver — on a custom port we choose —
Railway's TCP Proxy feature can front it. No VPS, no DNS changes.

Limitation: a Railway service gets only **one** public TCP proxy (used by
SMTP), so the built-in IMAP server (Dovecot, port 143) is only reachable
from inside the same Railway project via `mailserver.railway.internal`.
If your mail client/bot runs outside Railway, use Option B instead.

1. Push this project to a Git repo and create a new Railway service from it
   (the included `Dockerfile` is picked up automatically).
2. Set these variables on the service:

   ```
   MAIL_DOMAINS=taylorgoat.com
   SMTP_PORT=2525
   API_PORT=8080
   API_TOKEN=<long random string>
   WEBHOOK_URL=<your other Railway service URL>/mail
   WEBHOOK_SECRET=<long random string>
   DB_PATH=/data/mail.db
   ATTACH_DIR=/data/attachments

   # optional, keeps copies flowing to your Gmail archive:
   #FORWARD_TO=you@gmail.com
   #FORWARD_SMTP_HOST=smtp.gmail.com
   #FORWARD_SMTP_PORT=587
   #FORWARD_SMTP_USER=you@gmail.com
   #FORWARD_SMTP_PASS=<gmail app password>
   ```

3. Service → Settings → Networking:
   - Add a **TCP Proxy** targeting internal port `2525`. Railway gives you
     `something.proxy.rlwy.net:<port>` — use this host:port in the GSuite
     Host entry (step 3 below).
   - Add a **public domain** for the HTTP API (port `8080`) — your script
     polls it at `https://<that domain>/messages`, or skip this if you only
     use the webhook.
4. Optionally attach a **Volume** mounted at `/data` so the SQLite DB and
   attachments survive redeploys (the webhook is the primary path, so this
   is just the backup copy).
5. TLS note: without a certificate, mail between Google and Railway is not
   encrypted. GSuite Host entries let you require TLS only with a CA-signed
   cert, which we can't get for `*.proxy.rlwy.net`. If the mail contents are
   sensitive to you, use Option B (VPS + Let's Encrypt) instead; otherwise
   proceed — traffic rides Google's and Railway's backbone networks.

## Option B: VPS

## 0. Before you start

- Any cheap VPS with a static IP works (Ubuntu 24.04 assumed). Port 25 is
  **not** needed — GSuite routes to our custom port 2525, so no unblock
  tickets.
- You need admin access to the Google Workspace admin console
  (admin.google.com).

## 1. DNS (VPS option only)

One record only — no MX changes:

| Type | Name | Value |
|------|------|-------|
| A    | mail | <VPS IP> |

Do **not** touch MX or TXT/SPF records; GSuite keeps working untouched.

## 2. Server setup (VPS option only)

```bash
# packages
sudo apt update
sudo apt install -y python3-venv python3-pip certbot ufw dovecot-imapd

# firewall
sudo ufw allow OpenSSH
sudo ufw allow 2525/tcp    # SMTP from GSuite routing
sudo ufw allow 993/tcp     # IMAPS for your mail client/bot
sudo ufw allow 80/tcp      # certbot
sudo ufw allow 8080/tcp    # API for your script
sudo ufw enable

# user + directories
sudo useradd --system --home /opt/mailreceiver mailrecv
sudo mkdir -p /opt/mailreceiver /var/lib/mailreceiver/maildir
sudo chown -R mailrecv:mailrecv /var/lib/mailreceiver

# code (scp/rsync this project up first, or git clone)
cd /opt/mailreceiver
sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements.txt

# TLS certificate (uses port 80, needs the A record from step 1 live)
sudo certbot certonly --standalone -d mail.example.com

# let the services read the certs
sudo chgrp -R mailrecv /etc/letsencrypt/live /etc/letsencrypt/archive
sudo chmod 750 /etc/letsencrypt/live /etc/letsencrypt/archive
sudo usermod -aG mailrecv dovecot

# IMAP (Dovecot): config + one user. Replace the password!
sudo cp deploy/dovecot.conf /etc/dovecot/dovecot.conf
sudo sed -i 's/mail.example.com/mail.yourdomain.com/' /etc/dovecot/dovecot.conf
printf 'bot:{PLAIN}your-strong-password\n' | sudo tee /etc/dovecot/users
sudo systemctl enable --now dovecot

# config
sudo cp deploy/mailreceiver.env.example /etc/mailreceiver.env
sudo nano /etc/mailreceiver.env   # fill in domain, webhook URL, secrets

# service
sudo cp deploy/mailreceiver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mailreceiver
sudo journalctl -u mailreceiver -f   # watch the logs
```

Your mail client/bot then uses: host `mail.example.com`, port `993`
(IMAPS/SSL), username `bot`, the password you put in `/etc/dovecot/users`,
mailbox `INBOX`.

Add a renewal hook so the service reloads after certbot renews:

```bash
printf '#!/bin/sh\nsystemctl restart mailreceiver dovecot\n' | sudo tee /etc/letsencrypt/renewal-hooks/deploy/mailreceiver.sh
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/mailreceiver.sh
```

## 3. Google Workspace routing rule

In admin.google.com (labels vary slightly between console versions):

1. **Apps → Google Workspace → Gmail → Hosts → Add route**
   - Name: `mailreceiver`
   - Host/port:
     - Railway: the `something.proxy.rlwy.net:<port>` from your TCP proxy
     - VPS: `mail.example.com`, port `2525`
   - TLS options: on the VPS path you can require TLS (the Let's Encrypt
     cert passes validation); on Railway leave TLS optional/unrequired.
2. **Apps → Google Workspace → Gmail → Routing → Add rule**
   - Envelope recipient: **Pattern match**, regexp `.*@taylorgoat\.com`
     (diverts every address on the domain).
   - Action: **Change route** → select the `mailreceiver` host.
   - Warning: with a match-everything pattern, any real GSuite inbox on
     taylorgoat.com stops receiving inbound mail. Sending *from* GSuite is
     unaffected. If you still want copies in your Gmail, set the `FORWARD_*`
     variables (see below) — the receiver will relay a copy to your Gmail
     via Gmail's own SMTP, so deliverability is guaranteed. To create the
     app password: myaccount.google.com → Security → 2-Step Verification →
     App passwords.

If the receiver is down (deploy, restart), Google queues and retries the
delivery, so mail is not lost during short outages.

## 4. Test

From any outside machine, against whichever endpoint you deployed
(Railway: `something.proxy.rlwy.net:<port>`; VPS: `mail.example.com:2525`):

```bash
swaks --to anything@taylorgoat.com --server <host> --port <port> \
      --from test@web.de --header "Subject: live test" --body "hello"
```

Then the real end-to-end test: send an email to one of the web.de accounts
and watch it arrive via GSuite.

Check what the server received (Railway: the HTTP domain from step A3;
VPS: `https://mail.example.com:8080`):

```bash
curl -H "Authorization: Bearer <API_TOKEN>" https://<api-host>/messages
```

Test IMAP (VPS): `openssl s_client -connect mail.example.com:993`, then type
`a1 login bot your-strong-password` — expect `a1 OK ... logged in`.

## 5. Your script on Railway

**Webhook (recommended):** add an HTTPS endpoint that accepts POSTs. Each
email arrives as JSON: `id, received_at, from, to, subject, text, html,
headers, attachments[]` (attachments under 1 MB are inline as `content_b64`).
Verify the `X-Signature-256` header:
`"sha256=" + HMAC_SHA256(WEBHOOK_SECRET, raw_body)`.

**Or poll** instead (simpler, adds delay):

```
GET https://<api-host>/messages?since_id=<last id you saw>
Authorization: Bearer <API_TOKEN>
```

## 6. Ops notes

- Everything is stored in SQLite (`webhook_delivered` flag included), and
  failed webhooks can be re-fired with `POST /messages/<id>/redeliver`.
- The server only accepts recipients at MAIL_DOMAINS and never relays — it
  cannot be abused as an open relay.
- Port 2525 is publicly reachable, so in theory a spammer could inject mail
  directly to bot addresses. Risk is low (nobody scans for it), and it only
  lands in your DB — but if it becomes a problem, restrict 2525 in ufw to
  Google's published outbound IP ranges.
