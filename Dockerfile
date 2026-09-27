FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# 先装依赖以利用构建缓存；verify 服务需要 pytest / httpx，一并装入镜像。
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY app ./app
COPY tests ./tests
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 appuser \
    && chmod +x scripts/verify \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import json,urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5); sys.exit(0 if json.load(r)['status']=='ok' else 1)"

# 常驻 Web 服务；compose 的 verify 服务会覆盖 command 为一次性校验。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
