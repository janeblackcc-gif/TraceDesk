from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.target_browser_smoke import parse_origin, read_credentials, safe_origin


def test_safe_helpers_import_without_optional_playwright() -> None:
    code = """
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'playwright' or name.startswith('playwright.'):
        raise ModuleNotFoundError("blocked optional browser dependency")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import scripts.target_browser_smoke
"""
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_parse_origin_accepts_path_free_https() -> None:
    origin, parsed = parse_origin('https://203.0.113.10:8443/')

    assert origin == 'https://203.0.113.10:8443'
    assert safe_origin(parsed) == {'scheme': 'https', 'host': '203.0.113.10', 'port': 8443}


@pytest.mark.parametrize('origin', [
    'http://example.invalid',
    'https://user@example.invalid',
    'https://example.invalid/path',
    'https://example.invalid?query=1',
    'https://example.invalid/#fragment',
])
def test_parse_origin_rejects_unsafe_targets(origin: str) -> None:
    with pytest.raises(ValueError):
        parse_origin(origin)


def test_read_credentials_accepts_exact_bounded_schema(tmp_path: Path) -> None:
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps({
        'email': 'browser@example.invalid',
        'password': 'not-a-real-password-123',
    }), encoding='utf-8')

    assert read_credentials(path) == ('browser@example.invalid', 'not-a-real-password-123')


@pytest.mark.parametrize('payload', [
    {'email': 'browser@example.invalid'},
    {'email': 'browser@example.invalid', 'password': 'too-short'},
    {'email': 'not-an-email', 'password': 'not-a-real-password-123'},
    {'email': 'browser@example.invalid', 'password': 'not-a-real-password-123', 'token': 'forbidden'},
])
def test_read_credentials_rejects_invalid_payload(tmp_path: Path, payload: dict[str, str]) -> None:
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps(payload), encoding='utf-8')

    with pytest.raises(ValueError):
        read_credentials(path)


def test_read_credentials_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / 'target.json'
    target.write_text('{}', encoding='utf-8')
    link = tmp_path / 'link.json'
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip('symlinks are unavailable')

    with pytest.raises(ValueError):
        read_credentials(link)
