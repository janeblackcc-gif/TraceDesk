"""Run a bounded authenticated Edge smoke against an existing HTTPS target.

The credential file is read locally and never copied into the evidence bundle.
Only synthetic content may be queried. Response bodies, credentials, cookies,
CSRF tokens, questions, answers, and source text are deliberately not recorded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import SplitResult, urlsplit

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page


COOKIE_NAME = '__Host-tracedesk-session'
MARKER = 'RECOVERY-OK'
SCOPE = 'portfolio-target-browser'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_origin(value: str) -> tuple[str, SplitResult]:
    candidate = value.strip().rstrip('/')
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('origin port is invalid') from exc
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
            parsed.path or parsed.query or parsed.fragment):
        raise ValueError('origin must be a path-free HTTPS origin without credentials')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('origin port is invalid')
    return candidate, parsed


def read_credentials(path: Path) -> tuple[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError('credentials file must be a regular file')
    raw = path.read_bytes()
    if not raw or len(raw) > 4096:
        raise ValueError('credentials file must be bounded and non-empty')
    try:
        payload = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('credentials file must be valid UTF-8 JSON') from exc
    if type(payload) is not dict or set(payload) != {'email', 'password'}:
        raise ValueError('credentials JSON must contain exactly email and password')
    email, password = payload['email'], payload['password']
    if (type(email) is not str or len(email) > 254 or
            re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email) is None):
        raise ValueError('credential email is invalid')
    if type(password) is not str or not 12 <= len(password) <= 128 or '\x00' in password:
        raise ValueError('credential password is invalid')
    return email, password


def safe_origin(parsed: SplitResult) -> dict[str, str | int]:
    result: dict[str, str | int] = {'scheme': parsed.scheme, 'host': parsed.hostname or ''}
    if parsed.port is not None:
        result['port'] = parsed.port
    return result


def write_status(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def run_browser(
    origin: str,
    email: str,
    password: str,
    output: Path,
    report: dict[str, Any],
    *,
    ignore_https_errors: bool,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            'target browser smoke requires deploy/browser-requirements.txt and a Playwright browser installation'
        ) from exc
    status_path = output / 'status.json'
    stage = 'launch'
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    page_error_count = 0

    def check(check_id: str, detail: dict[str, Any] | None = None) -> None:
        item: dict[str, Any] = {'id': check_id, 'status': 'passed', 'completed_at': utc_now()}
        if detail:
            item.update(detail)
        report['checks'].append(item)
        report['last_stage'] = check_id
        write_status(status_path, report)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(
                ignore_https_errors=ignore_https_errors,
                viewport={'width': 1440, 'height': 1000},
                accept_downloads=True,
            )
            page = context.new_page()

            def page_error(_: object) -> None:
                nonlocal page_error_count
                page_error_count += 1

            page.on('pageerror', page_error)
            stage = 'login-page'
            response = page.goto(origin, wait_until='networkidle', timeout=30_000)
            if response is None or response.status != 200 or not page.locator('#login-form').is_visible():
                raise RuntimeError('login page did not become ready')
            check('https-login-page', {'http_status': response.status})

            stage = 'authenticated-session'
            page.locator('#login-email').fill(email)
            page.locator('#login-password').fill(password)
            page.locator('#login-form button[type="submit"]').click()
            page.locator('#team-shell').wait_for(state='visible', timeout=20_000)
            cookies = context.cookies([origin])
            cookie = next((item for item in cookies if item.get('name') == COOKIE_NAME), None)
            if cookie is None:
                raise RuntimeError('session cookie was not created')
            cookie_flags = {
                'secure': cookie.get('secure') is True,
                'http_only': cookie.get('httpOnly') is True,
                'same_site': cookie.get('sameSite'),
                'host_prefix': str(cookie.get('name', '')).startswith('__Host-'),
            }
            if cookie_flags != {'secure': True, 'http_only': True, 'same_site': 'Strict', 'host_prefix': True}:
                raise RuntimeError('session cookie flags are invalid')
            check('authenticated-session-cookie', cookie_flags)

            stage = 'csrf'
            csrf_probe = page.evaluate("""async () => {
                const response = await fetch('/api/v1/auth/csrf', {credentials: 'same-origin'});
                const body = response.ok ? await response.json() : {};
                return {status: response.status, tokenPresent: typeof body.csrf_token === 'string' && body.csrf_token.length > 0};
            }""")
            if csrf_probe != {'status': 200, 'tokenPresent': True}:
                raise RuntimeError('CSRF endpoint probe failed')
            check('csrf-endpoint', {'http_status': 200, 'token_present': True})

            stage = 'knowledge-base'
            selected_kb = page.locator('#kb-select').input_value()
            if not selected_kb:
                raise RuntimeError('no authorized knowledge base is selected')
            if page.locator('#profile-select').input_value() != 'evidence':
                page.locator('#profile-select').select_option('evidence')
            check('authorized-knowledge-base', {'selected': True, 'profile': 'evidence'})

            stage = 'query'
            page.locator('[data-view="workspace"]').click()
            page.locator('#question').fill('What is the bounded worker recovery marker?')
            page.locator('#ask-button').click()
            page.locator('.answer-card').wait_for(state='visible', timeout=60_000)
            if MARKER not in page.locator('.answer-card').inner_text():
                raise RuntimeError('synthetic marker was not present in the rendered evidence')
            check('async-evidence-query', {'synthetic_marker_present': True})

            stage = 'source'
            citation = page.locator('.citation-button').first
            citation.wait_for(state='visible', timeout=10_000)
            citation.click()
            page.locator('#source-dialog').wait_for(state='visible', timeout=10_000)
            page.locator('.quote-mark').first.wait_for(state='visible', timeout=10_000)
            page.locator('#close-source').click()
            page.locator('#source-dialog').wait_for(state='hidden', timeout=10_000)
            check('source-highlight-dialog', {'exact_highlight_visible': True})

            stage = 'export'
            with tempfile.TemporaryDirectory(prefix='tracedesk-target-browser-') as temporary:
                target = Path(temporary) / 'query-export.md'
                with page.expect_download(timeout=20_000) as pending_download:
                    page.get_by_role('button', name='导出本次记录').click()
                pending_download.value.save_as(target)
                exported = target.read_bytes()
            if not exported or MARKER.encode() not in exported:
                raise RuntimeError('authorized export did not preserve the synthetic marker')
            check('authorized-export', {
                'bytes': len(exported),
                'sha256': hashlib.sha256(exported).hexdigest(),
                'synthetic_marker_present': True,
                'body_persisted': False,
            })

            stage = 'logout'
            page.locator('#logout').click()
            page.locator('#login-form').wait_for(state='visible', timeout=10_000)
            after_logout = page.evaluate("""async () => {
                const response = await fetch('/api/v1/me', {credentials: 'same-origin'});
                return response.status;
            }""")
            if after_logout != 401:
                raise RuntimeError('logout did not revoke the authenticated session')
            if any(item.get('name') == COOKIE_NAME for item in context.cookies([origin])):
                raise RuntimeError('session cookie remained after logout')
            check('logout-session-revocation', {'me_http_status': 401, 'cookie_removed': True})

            if page_error_count:
                raise RuntimeError('browser page errors were observed')
            report['page_error_count'] = 0
            report['status'] = 'passed'
            report['last_stage'] = 'complete'
    except Exception as exc:
        report['status'] = 'failed'
        report['failed_stage'] = stage
        report['error_type'] = type(exc).__name__
        raise
    finally:
        report['page_error_count'] = page_error_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--credentials-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ignore-https-errors', action='store_true')
    args = parser.parse_args()
    try:
        origin, parsed = parse_origin(args.origin)
        email, password = read_credentials(args.credentials_file)
        if args.output.exists():
            raise ValueError('output directory must not already exist')
        args.output.mkdir(parents=True)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    report: dict[str, Any] = {
        'schema_version': 1,
        'status': 'running',
        'scope': SCOPE,
        'formal_claim': 'none',
        'run_mode': 'external-target',
        'synthetic_data_only': True,
        'started_at': utc_now(),
        'origin': safe_origin(parsed),
        'browser': {'engine': 'chromium', 'channel': 'msedge', 'headless': True},
        'tls_verification': 'disabled' if args.ignore_https_errors else 'verified',
        'checks': [],
        'last_stage': 'starting',
    }
    status_path = args.output / 'status.json'
    write_status(status_path, report)
    try:
        run_browser(origin, email, password, args.output, report,
                    ignore_https_errors=args.ignore_https_errors)
    except Exception:
        print(f'target browser smoke failed at stage: {report.get("failed_stage", "unknown")}', file=sys.stderr)
        return_code = 1
    else:
        return_code = 0
    finally:
        report['finished_at'] = utc_now()
        write_status(status_path, report)
    if return_code == 0:
        print(json.dumps({'status': report['status'], 'scope': report['scope'],
                          'checks': len(report['checks'])}, ensure_ascii=False))
    return return_code


if __name__ == '__main__':
    raise SystemExit(main())
