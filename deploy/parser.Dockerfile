FROM python:3.13.14-slim-trixie@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6
COPY deploy/parser-requirements-lock.txt /opt/parser-requirements-lock.txt
RUN pip install --no-cache-dir --require-hashes -r /opt/parser-requirements-lock.txt
WORKDIR /opt/tracedesk
COPY app/__init__.py app/__init__.py
COPY app/ingest.py app/ingest.py
COPY app/parsing/subprocess_main.py app/parsing/subprocess_main.py
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "app.parsing.subprocess_main"]
