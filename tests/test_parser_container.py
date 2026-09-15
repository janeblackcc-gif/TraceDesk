import io
import os
import subprocess
import time
from uuid import uuid4

import pytest
from pypdf import PdfWriter

from app.parsing.worker import DockerParser
from app.services.errors import DomainError


@pytest.fixture
def parser_image():
    image = os.environ.get('TRACEDESK_PARSER_TEST_IMAGE')
    if not image:
        pytest.skip('Docker parser tests require TRACEDESK_PARSER_TEST_IMAGE')
    return image


def test_real_container_parses_and_preserves_text(parser_image, tmp_path):
    path = tmp_path / 'original'
    path.write_bytes('# 部署\n默认端口 8088。\n'.encode())
    result = DockerParser(parser_image, timeout=30).parse(path, 'deploy.md')
    assert result.pages == ['# 部署\n默认端口 8088。\n']
    assert result.chunks[0].text == '# 部署\n默认端口 8088。'


@pytest.mark.parametrize('encrypted', [False, True])
def test_empty_and_encrypted_pdf_fail_in_container(parser_image, tmp_path, encrypted):
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt('synthetic-test-password')
    buffer = io.BytesIO()
    writer.write(buffer)
    path = tmp_path / 'original'
    path.write_bytes(buffer.getvalue())
    with pytest.raises(DomainError, match='INPUT_INVALID'):
        DockerParser(parser_image, timeout=30).parse(path, 'input.pdf')


def test_hung_container_is_killed_and_removed(parser_image, tmp_path):
    class HungParser(DockerParser):
        name = ''

        def command(self, path, filename, name):
            self.name = name
            base = super().command(path, filename, name)
            return base[:-2] + ['--entrypoint', 'python', self.image, '-c', 'import time; time.sleep(300)']

    path = tmp_path / 'original'
    path.write_bytes(b'fixture')
    parser = HungParser(parser_image, timeout=3)
    started = time.monotonic()
    with pytest.raises(DomainError, match='PARSE_TIMEOUT'):
        parser.parse(path, 'fixture.txt')
    assert time.monotonic() - started < 30
    check = subprocess.run(['docker', 'inspect', parser.name], capture_output=True, timeout=10)
    assert check.returncode != 0


def test_memory_limit_terminates_parser_without_host_oom(parser_image, tmp_path):
    class MemoryParser(DockerParser):
        def command(self, path, filename, name):
            base = super().command(path, filename, name)
            base[base.index('--memory') + 1] = '64m'
            base[base.index('--memory-swap') + 1] = '64m'
            return base[:-2] + ['--entrypoint', 'python', self.image, '-c', 'data = bytearray(256 * 1024 * 1024)']

    path = tmp_path / 'original'
    path.write_bytes(b'fixture')
    with pytest.raises(DomainError, match='PARSER_RESOURCE_OR_RUNTIME_FAILURE'):
        MemoryParser(parser_image, timeout=30).parse(path, 'fixture.txt')


def test_expired_orphan_container_is_reaped(parser_image, tmp_path):
    path = tmp_path / 'original'
    path.write_bytes(b'fixture')
    parser = DockerParser(parser_image)
    name = 'tracedesk-parse-' + uuid4().hex
    command = parser.command(path, 'fixture.txt', name)
    label = next(index for index, value in enumerate(command) if value.startswith('tracedesk.expires='))
    command[label] = 'tracedesk.expires=1'
    command.insert(2, '--detach')
    command = command[:-2] + ['--entrypoint', 'python', parser_image, '-c', 'import time; time.sleep(300)']
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
        assert result.returncode == 0
        assert parser.reap_expired() >= 1
        result = subprocess.run(['docker', 'inspect', name], capture_output=True, timeout=10)
        assert result.returncode != 0
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)


def test_child_wall_timer_works_independently_of_supervisor(parser_image, tmp_path):
    class AlarmParser(DockerParser):
        def command(self, path, filename, name):
            base = super().command(path, filename, name)
            program = ('import time, signal; import app.parsing.subprocess_main as p; '
                       'p.parse = lambda *args: time.sleep(300); signal.alarm(1); p.main()')
            return base[:-2] + ['--entrypoint', 'python', self.image, '-c', program, 'fixture.txt']

    path = tmp_path / 'original'
    path.write_bytes(b'fixture')
    with pytest.raises(DomainError, match='PARSE_TIMEOUT'):
        AlarmParser(parser_image, timeout=30).parse(path, 'fixture.txt')
