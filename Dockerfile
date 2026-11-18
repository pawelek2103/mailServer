FROM python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends dovecot-imapd \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY receiver.py start.sh ./
COPY dovecot.conf /etc/dovecot/dovecot.conf
RUN chmod +x start.sh
CMD ["./start.sh"]
