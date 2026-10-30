# SimpleURL - 轻量短链接服务
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# 国内网络可在构建时替换 pip 源（install.sh 会自动带上，海外不传这两个参数）
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=
COPY requirements.txt .
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      pip install --no-cache-dir -i "$PIP_INDEX_URL" --trusted-host "${PIP_TRUSTED_HOST:-}" -r requirements.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY app ./app

# 数据目录（SQLite 挂载点）
RUN mkdir -p /data && chown -R 65534:65534 /data
VOLUME ["/data"]

ENV DATABASE_URL=sqlite:////data/simpleurl.db \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# 非 root 运行，数据目录可写
USER 65534:65534

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST} --port ${PORT} --workers ${WORKERS:-1}"]
