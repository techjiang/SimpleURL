# SimpleURL - 轻量短链接服务
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# 先装依赖，利用镜像缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
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
