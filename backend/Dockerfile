# 默认从官方 slim 构建，只装 Python 依赖（manylinux wheel，无需 gcc）。
# 健康检查用标准库，不再 apt-get 装 curl，避免 Debian 镜像站在
# FlClash TUN / fake-ip（解析到 172.18.0.x）下把整次构建打挂。
#
# 容器访问不了 PyPI 时，在 build/.env 里设：
#   BACKEND_FROM=mt5hub-server-backend:latest
#   INSTALL_DEPS=0
# 复用本机已有后端镜像的 site-packages，只覆盖业务代码。
ARG BACKEND_FROM=python:3.12-slim
FROM ${BACKEND_FROM}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .

ARG INSTALL_DEPS=1
RUN if [ "$INSTALL_DEPS" = "1" ]; then \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

# Single worker: WebSocket connections live in-process (see doc ch.5 for HA via Redis pub/sub).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
