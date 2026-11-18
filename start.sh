#!/bin/sh
set -e

: "${IMAP_USER:=bot}"
: "${IMAP_PASSWORD:?Set IMAP_PASSWORD (the IMAP login for your mail client)}"

printf '%s:{PLAIN}%s\n' "$IMAP_USER" "$IMAP_PASSWORD" > /etc/dovecot/users

mkdir -p /data/maildir
dovecot

exec python receiver.py
