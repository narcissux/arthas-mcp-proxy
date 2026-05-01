# Arthas MCP Proxy - Dockerfile
# Supports: online (pip) / offline (local whl) / auto (fallback)

FROM python:3.12-slim

ARG PIP_SOURCE=auto
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

WORKDIR /app

# Install build tools for hatchling
RUN pip install --no-cache-dir --upgrade pip hatchling

# Copy project files
COPY pyproject.toml .
COPY README.md .
COPY src/ ./src/

# Copy offline packages (optional, for offline mode)
COPY packages/ /app/packages/
RUN cp /app/packages/arthas-bin.zip /app/arthas-bin.zip 2>/dev/null || true

# Install dependencies
RUN set -eux; \
    pip install --no-cache-dir -e .; \
    PIP_SRC=$(echo "${PIP_SOURCE}" | tr '[:upper:]' '[:lower:]'); \
    install_online() { pip install --index-url "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}" -e ".[dev]"; }; \
    install_offline() { pip install --no-index --find-links=/app/packages -e ".[dev]"; }; \
    case "${PIP_SRC}" in \
        "offline") install_offline ;; \
        "online") install_online ;; \
        *) install_online || install_offline ;; \
    esac; \
    rm -rf /app/packages /root/.cache

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
ENV SSH_IDLE_TIMEOUT=300
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000

EXPOSE 8000
ENTRYPOINT ["python", "-m", "arthas_mcp_proxy", "--transport", "sse"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
