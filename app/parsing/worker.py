from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.ingest import MAX_PAGES, MAX_TEXT, MAX_CHUNKS
from app.services.errors import DomainError


class ParsedChunk(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    page: int = Field(ge=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    heading: str = Field(max_length=160)
    text: str = Field(min_length=1, max_length=10000)


class ParseResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    filename: str = Field(min_length=1, max_length=180)
    pages: list[str] = Field(min_length=1, max_length=MAX_PAGES)
    chunks: list[ParsedChunk] = Field(min_length=1, max_length=MAX_CHUNKS)
    parser_version: str = Field(default='fixture', min_length=1, max_length=40, pattern=r'^[A-Za-z0-9._-]+$')

    @model_validator(mode='after')
    def exact_text(self) -> ParseResult:
        if sum(map(len, self.pages)) > MAX_TEXT or any('\x00' in page for page in self.pages):
            raise ValueError('Invalid parser text')
        for chunk in self.chunks:
            if chunk.page > len(self.pages):
                raise ValueError('Invalid parser page')
            lines = self.pages[chunk.page - 1].split('\n')
            if not chunk.start_line <= chunk.end_line <= len(lines):
                raise ValueError('Invalid parser lines')
            if chunk.text != '\n'.join(lines[chunk.start_line - 1:chunk.end_line]).strip():
                raise ValueError('Parser chunk differs from source')
        return self


class DockerParser:
    def __init__(self, image: str, *, timeout: float = 120):
        if not image or timeout <= 0:
            raise ValueError('Parser image and positive timeout required')
        self.image = image
        self.timeout = timeout

    def command(self, path: Path, filename: str, name: str) -> list[str]:
        return ['docker', 'run', '--name', name, '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--label', 'tracedesk.parser=true', '--label', 'tracedesk.instance=' + name,
                '--label', 'tracedesk.expires=' + str(time.time() + self.timeout + 5),
                '--security-opt', 'no-new-privileges', '--user', '65532:65532', '--memory', '1536m',
                '--memory-swap', '1536m', '--cpus', '1', '--pids-limit', '64', '--log-driver', 'none',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=2147483648',
                '--mount', f'type=bind,source={path.resolve()},target=/input/source,readonly',
                self.image, filename]

    @staticmethod
    def reap_expired() -> int:
        """Recover only this application's expired parser containers after a crash."""
        listing = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=tracedesk.parser=true'],
                                 capture_output=True, text=True, timeout=15, check=True)
        removed = 0
        for identifier in listing.stdout.splitlines():
            if not re.fullmatch('[0-9a-f]{12,64}', identifier):
                continue
            details = subprocess.run(['docker', 'inspect', '--format', '{"name":{{json .Name}},"labels":{{json .Config.Labels}}}', identifier],
                                     capture_output=True, text=True, timeout=10, check=False)
            if details.returncode:
                continue
            record = json.loads(details.stdout)
            labels = record['labels']
            name = labels.get('tracedesk.instance', '')
            if not re.fullmatch(r'tracedesk-parse-[0-9a-f]{32}', name) or record['name'] != '/' + name:
                continue
            try:
                expired = float(labels['tracedesk.expires']) <= time.time()
            except (KeyError, ValueError, TypeError):
                continue
            if expired:
                subprocess.run(['docker', 'rm', '-f', identifier], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15, check=True)
                removed += 1
        return removed

    @staticmethod
    def stop(name: str) -> None:
        subprocess.run(['docker', 'kill', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)

    def parse(self, path: Path, filename: str, cancel: threading.Event | None = None) -> ParseResult:
        name = 'tracedesk-parse-' + uuid4().hex
        oversized = threading.Event()
        finished = threading.Event()
        process = subprocess.Popen(self.command(path, filename, name), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with tempfile.TemporaryFile() as output:
            def collect() -> None:
                size = 0
                try:
                    while block := process.stdout.read(65536):
                        size += len(block)
                        if size > 16 * 1024 * 1024:
                            oversized.set()
                            self.stop(name)
                            break
                        output.write(block)
                finally:
                    process.stdout.close()

            def cancellation() -> None:
                while not finished.wait(.1):
                    if cancel is not None and cancel.is_set():
                        self.stop(name)
                        return

            reader = threading.Thread(target=collect, daemon=True)
            watcher = threading.Thread(target=cancellation, daemon=True)
            reader.start()
            watcher.start()
            try:
                try:
                    code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self.stop(name)
                    process.wait(timeout=15)
                    raise DomainError('PARSE_TIMEOUT', 422) from None
                if cancel is not None and cancel.is_set():
                    raise DomainError('PARSE_CANCELLED', 409)
                if oversized.is_set():
                    raise DomainError('PARSE_OUTPUT_LIMIT', 422)
                reader.join(timeout=10)
                if reader.is_alive():
                    raise DomainError('PARSER_PROTOCOL_ERROR', 503)
                if code == 4:
                    raise DomainError('PARSE_TIMEOUT', 422)
                if code not in {0, 2, 3}:
                    raise DomainError('PARSER_RESOURCE_OR_RUNTIME_FAILURE', 503)
                output.seek(0)
                raw = output.read(16 * 1024 * 1024 + 1)
                if code != 0:
                    raise DomainError('INPUT_INVALID' if code == 2 else 'PARSER_FAILED', 422)
                try:
                    result = ParseResult.model_validate_json(raw)
                    if result.parser_version == 'fixture':
                        raise DomainError('PARSER_IDENTITY_MISSING', 503)
                    return result
                except ValidationError:
                    raise DomainError('PARSER_PROTOCOL_ERROR', 503) from None
            finally:
                finished.set()
                if process.poll() is None:
                    self.stop(name)
                    process.kill()
                    process.wait(timeout=10)
                reader.join(timeout=10)
                watcher.join(timeout=16)
                subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
