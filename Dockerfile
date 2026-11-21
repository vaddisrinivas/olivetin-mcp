# olivetin-mcp — hardened MCP SSE server
#
# Executes approved actions via OliveTin REST API.
# No SSH, no Remote Login needed.
#
# Security layers:
#   1. Non-root user bridge:10001, no login shell
#   2. All shells + network tools removed after pip install
#   3. pip removed — no runtime package installation
#   4. read_only root filesystem (in compose)
#   5. ALL Linux capabilities dropped (in compose)
FROM python:3.14.9-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST_BRIDGE_HOST=0.0.0.0

LABEL org.opencontainers.image.source="https://github.com/vaddisrinivas/olivetin-mcp"
LABEL org.opencontainers.image.description="Hardened MCP server for OliveTin with human-in-the-loop approval"
LABEL org.opencontainers.image.licenses="MIT"

USER root

RUN groupadd -r -g 10001 bridge \
    && useradd -r -u 10001 -g bridge -s /usr/sbin/nologin -d /app -M bridge

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r requirements.txt

COPY main.py setup_google_auth.py ./
RUN chown -R bridge:bridge /app

# Strip attack surface — shells and exfiltration tools
RUN \
    for b in bash sh dash zsh ksh csh tcsh; do rm -f /bin/$b /usr/bin/$b 2>/dev/null || true; done && \
    for b in curl wget nc netcat ncat socat ssh scp sftp; do rm -f /usr/bin/$b /usr/local/bin/$b /bin/$b 2>/dev/null || true; done && \
    for b in perl ruby su sudo gcc cc g++ ld make; do rm -f /usr/bin/$b /usr/local/bin/$b 2>/dev/null || true; done && \
    pip uninstall -y pip setuptools wheel 2>/dev/null || true

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:9003/healthz', timeout=3)"]

USER bridge
EXPOSE 9003
CMD ["python3", "main.py", "--http"]
