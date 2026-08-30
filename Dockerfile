FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git ffmpeg tini ripgrep xz-utils && \
    rm -rf /var/lib/apt/lists/*   

ARG GOG_VERSION=0.29.0

RUN curl -L -o /tmp/gogcli.tar.gz \
      "https://github.com/openclaw/gogcli/releases/download/v${GOG_VERSION}/gogcli_${GOG_VERSION}_linux_amd64.tar.gz" \
 && tar -xzf /tmp/gogcli.tar.gz -C /tmp \
 && install -m 0755 /tmp/gog /usr/local/bin/gog \
 && rm -f /tmp/gogcli.tar.gz /tmp/gog

RUN apt-get update && \
    apt-get install -y --no-install-recommends gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y --auto-remove gnupg && \
    rm -rf /var/lib/apt/lists/*

RUN node --version && npm --version

RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /tmp/hermes-agent && \
    cd /tmp/hermes-agent && \
    npm --prefix web ci && \
    npm --prefix web run build && \
    uv pip install --system --no-cache \                                                                  
     -e ".[all]" \                                                                                       
     "python-telegram-bot[webhooks]==22.8" && \ 
    rm -rf /tmp/hermes-agent/.git

COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

RUN mkdir -p /data/.hermes

COPY server.py /app/server.py
COPY supervisor.py /app/supervisor.py
COPY templates/ /app/templates/
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENV HOME=/data
ENV HERMES_HOME=/data/.hermes

ENTRYPOINT ["tini", "--"]
CMD ["/app/start.sh"]
