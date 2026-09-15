FROM python:3.13.14-slim-trixie@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6
WORKDIR /opt/tracedesk
COPY requirements-lock.txt /opt/tracedesk/requirements-lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements-lock.txt
COPY app app
COPY web web
COPY alembic alembic
COPY alembic.ini alembic.ini
COPY scripts/check_database.py scripts/check_openapi.py scripts/
COPY docs/industrial/openapi.json docs/industrial/openapi.json
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
ENTRYPOINT ["python"]
CMD ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8765", "--workers", "1", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
