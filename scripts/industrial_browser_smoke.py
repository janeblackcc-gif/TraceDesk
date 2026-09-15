"""Real HTTPS/Edge acceptance on a newly created, disposable PostgreSQL database.

Only synthetic inputs are used. Parsing runs in the real parser container;
embedding uses a deterministic fixture, so this is not model quality evidence.
The source test instance is used only to CREATE/DROP this run's own database.
"""
from __future__ import annotations

import argparse
import json
import secrets
import socket
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn
from alembic import command
from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import create_engine, select

from app.application import create_application
from app.config import Settings
from app.db.models import KnowledgeBase, Workspace
from app.db.session import Database, migration_config, validate_database_url
from app.jobs.index_handler import IndexHandler
from app.jobs.parse_handler import ParseHandler
from app.jobs.query_handler import QueryHandler
from app.jobs.repository import JobRepository
from app.models.profile import EmbeddingIdentity
from app.parsing.worker import DockerParser
from app.services.auth_service import AuthService
from app.storage.object_store import ObjectStore


class FixtureProvider:
    def identity(self):
        return EmbeddingIdentity('fixture', 'fixture', 'a' * 64)
    def embed(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]
    def generate(self, question, evidence):
        return {'abstain': False, 'claims': [{'text': 'Synthetic answer', 'citations': [
            {'chunk_id': evidence[0]['id'], 'quote': evidence[0]['text']}]}]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-env', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--certificate', type=Path, required=True)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--parser-image', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat(), 'checks': [],
              'model_mode': 'synthetic fixture; no model quality claim', 'browser': 'installed Microsoft Edge'}
    def checked(label):
        report['checks'].append(label)
        print(label, flush=True)
        (args.output / 'status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    connection_url = dotenv_values(args.database_env, encoding='utf-8-sig', interpolate=False).get('TRACEDESK_TEST_DATABASE_URL')
    if not connection_url:
        parser.error('TRACEDESK_TEST_DATABASE_URL is required')
    url = validate_database_url(connection_url)
    db_name = 'tracedesk_test_browser_' + uuid4().hex
    admin_engine = create_engine(url, isolation_level='AUTOCOMMIT', hide_parameters=True)
    with admin_engine.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{db_name}"')
    database = Database(url.set(database=db_name).render_as_string(hide_password=False))
    server, thread, browser, page = None, None, None, None
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    origin = f'https://localhost:{port}'
    try:
        with database.engine.begin() as connection:
            config = migration_config()
            config.attributes['connection'] = connection
            command.upgrade(config, 'head')
        token, password = secrets.token_urlsafe(40), secrets.token_urlsafe(24)
        auth = AuthService(database, token)
        admin = auth.bootstrap(token, 'browser-admin@example.invalid', password, '验收管理员', 'req_browser')
        viewer = auth.create_user(admin, 'browser-viewer@example.invalid', password, '验收查看者', 'req_browser')
        with database.transaction() as session:
            workspace = session.scalar(select(Workspace))
            kb = KnowledgeBase(workspace_id=workspace.id, name='团队操作手册', slug='browser-fixture')
            session.add(kb)
            session.flush()
            workspace_id, kb_id = workspace.id, kb.id
        auth.set_workspace_member(admin, workspace_id, viewer, 'member', 'req_browser')
        auth.set_kb_member(admin, kb_id, viewer, 'viewer', 'req_browser')
        settings = Settings(data_dir=args.output / 'data', database_url=database.engine.url.render_as_string(hide_password=False), public_origin=origin)
        ready = threading.Event()
        class TestServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                ready.set()
        server = TestServer(uvicorn.Config(create_application(settings), host='127.0.0.1', port=port,
            ssl_certfile=str(args.certificate), ssl_keyfile=str(args.key), log_level='warning', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        if not ready.wait(timeout=15):
            raise RuntimeError('HTTPS server did not start within 15 seconds')
        queue = JobRepository(database)
        def run_job(kind):
            lease = queue.claim('browser-fixture-' + kind, ('index', 'reindex') if kind == 'index' else (kind,))
            if lease is None:
                raise AssertionError('Expected queued ' + kind + ' job')
            handlers = {'parse': ParseHandler(database, ObjectStore(settings.data_dir / 'objects'), DockerParser(args.parser_image, timeout=30)),
                        'index': IndexHandler(database, lambda cancel: FixtureProvider()),
                        'query': QueryHandler(database, lambda cancel: FixtureProvider())}
            state = handlers[kind].run(queue, lease)
            if state != 'succeeded':
                raise AssertionError(f'{kind} fixture ended with {state}')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 1000})
            page = context.new_page()
            page_errors = []
            page.on('pageerror', lambda error: page_errors.append(str(error)))
            page.goto(origin, wait_until='networkidle')
            expect(page.locator('#login-form')).to_be_visible()
            page.screenshot(path=str(args.output / '01-login.png'), full_page=True)
            page.locator('#login-email').fill('browser-admin@example.invalid')
            page.locator('#login-password').fill(password)
            page.locator('#login-form button').click()
            expect(page.locator('#team-shell')).to_be_visible()
            checked('Real HTTPS login, Secure session cookie, CSRF and static assets')
            page.locator('[data-view="library"]').click()
            page.locator('#upload-file').set_input_files({'name': 'fixture.md', 'mimeType': 'text/markdown',
                'buffer': '# Team deployment\nThe service port is 8088.\n'.encode()})
            page.locator('#upload-form button').click()
            expect(page.locator('#document-rows')).to_contain_text('fixture.md')
            run_job('parse')
            run_job('index')
            page.locator('#refresh').click()
            expect(page.locator('#document-rows')).to_contain_text('可查询')
            checked('Browser upload to real container parsing, PostgreSQL index activation and ready UI')
            page.locator('#toast').evaluate('element => { element.hidden = true; }')
            page.screenshot(path=str(args.output / '02-library.png'), full_page=True)
            page.locator('[data-view="workspace"]').click()
            page.locator('#question').fill('What is the service port?')
            page.locator('#ask-button').click()
            expect(page.locator('#query-progress')).to_be_visible()
            run_job('query')
            expect(page.locator('.answer-card')).to_contain_text('8088', timeout=15000)
            page.locator('.citation-button').first.click()
            expect(page.locator('#source-dialog')).to_be_visible()
            expect(page.locator('.quote-mark').first).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.locator('#source-dialog')).not_to_be_visible()
            checked('Async evidence answer, exact source highlight and Escape focus flow')
            with page.expect_download() as pending_download:
                page.get_by_role('button', name='导出本次记录').click()
            download = pending_download.value
            download.save_as(args.output / 'query-export.md')
            if '8088' not in (args.output / 'query-export.md').read_text(encoding='utf-8'):
                raise AssertionError('Export lost source content')
            checked('Authorized server-side Markdown export')
            page.locator('#toast').evaluate('element => { element.hidden = true; }')
            page.screenshot(path=str(args.output / '03-workspace.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            if page.evaluate('document.documentElement.scrollWidth > innerWidth'):
                raise AssertionError('Horizontal viewport overflow at 390px')
            page.screenshot(path=str(args.output / '04-mobile.png'), full_page=True)
            checked('390px layout without horizontal page overflow')
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.locator('[data-view="library"]').click()
            page.locator('#upload-file').set_input_files({'name': 'fixture.md', 'mimeType': 'text/markdown',
                'buffer': '# Team deployment\nThe service port is 8099.\n'.encode()})
            page.locator('#upload-form button').click()
            expect(page.locator('#document-rows')).to_contain_text('旧版可用 · 新版处理中')
            page.locator('[data-view="workspace"]').click()
            page.locator('#new-chat').click()
            page.locator('#question').fill('What is the service port?')
            page.locator('#ask-button').click()
            expect(page.locator('#query-progress')).to_be_visible()
            run_job('query')
            expect(page.locator('.answer-card')).to_contain_text('8088', timeout=15000)
            expect(page.locator('.answer-card')).not_to_contain_text('8099')
            run_job('parse')
            run_job('index')
            page.locator('#refresh').click()
            page.locator('#new-chat').click()
            page.locator('#question').fill('What is the service port?')
            page.locator('#ask-button').click()
            expect(page.locator('#query-progress')).to_be_visible()
            run_job('query')
            expect(page.locator('.answer-card')).to_contain_text('8099', timeout=15000)
            expect(page.locator('.answer-card')).not_to_contain_text('8088')
            checked('Replacement keeps the old active revision until atomic index activation, then serves only the new revision')
            page.locator('#new-chat').click()
            page.locator('#question').fill('What is the service port?')
            page.locator('#ask-button').click()
            expect(page.locator('#query-progress')).to_be_visible()
            page.reload(wait_until='networkidle')
            expect(page.locator('#query-progress')).to_be_visible()
            page.locator('#cancel-query').click()
            expect(page.locator('.answer-card')).to_contain_text('已取消', timeout=15000)
            checked('Reload resumes queued query; cancellation reaches terminal UI')
            viewer_context = browser.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 1000})
            viewer_page = viewer_context.new_page()
            viewer_page.on('pageerror', lambda error: page_errors.append(str(error)))
            viewer_page.goto(origin)
            viewer_page.locator('#login-email').fill('browser-viewer@example.invalid')
            viewer_page.locator('#login-password').fill(password)
            viewer_page.locator('#login-form button').click()
            expect(viewer_page.locator('#team-shell')).to_be_visible()
            viewer_page.locator('[data-view="library"]').click()
            expect(viewer_page.locator('#upload-panel')).not_to_be_visible()
            denied = viewer_page.evaluate('''async kb => {
                const csrf = await (await fetch('/api/v1/auth/csrf')).json();
                const form = new FormData(); form.append('file', new File(['denied'], 'denied.md'));
                return (await fetch('/api/v1/knowledge-bases/' + kb + '/documents', {method:'POST', body:form,
                    headers:{'X-CSRF-Token':csrf.csrf_token, 'Idempotency-Key':crypto.randomUUID()}})).status;
            }''', str(kb_id))
            if denied != 403:
                raise AssertionError('Viewer upload was not denied by server')
            checked('Viewer editing controls hidden and direct upload API denied')
            viewer_page.locator('[data-view="workspace"]').click()
            viewer_page.locator('#question').fill('What is the service port?')
            viewer_page.locator('#ask-button').click()
            expect(viewer_page.locator('#query-progress')).to_be_visible()
            run_job('query')
            expect(viewer_page.locator('.answer-card')).to_contain_text('8099', timeout=15000)
            auth.set_kb_member(admin, kb_id, viewer, None, 'req_browser_revoke')
            viewer_page.locator('#refresh').click()
            expect(viewer_page.locator('.answer-card')).to_have_count(0)
            expect(viewer_page.locator('#kb-select')).to_contain_text('尚无可访问')
            checked('Permission revocation clears rendered answer and visible KB state')
            viewer_context.close()
            page.locator('[data-view="library"]').click()
            page.locator('#refresh').click()
            page.get_by_role('button', name='删除', exact=True).click()
            expect(page.locator('#confirm-dialog')).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.locator('#document-rows')).to_contain_text('fixture.md')
            page.get_by_role('button', name='删除', exact=True).click()
            page.locator('#confirm-delete').click()
            expect(page.locator('#document-rows')).not_to_contain_text('fixture.md')
            page.locator('#show-deleted').check()
            expect(page.locator('#document-rows')).to_contain_text('fixture.md')
            page.get_by_role('button', name='恢复', exact=True).click()
            expect(page.locator('#document-rows')).to_contain_text('可查询')
            checked('Delete dialog cancellation, soft delete and admin restore')
            if page_errors:
                raise AssertionError('Browser JavaScript errors: ' + '; '.join(page_errors))
            report['status'] = 'passed'
            browser.close()
            browser = None
    except Exception as exc:
        report['status'] = 'failed'
        report['error_type'] = type(exc).__name__
        report['error'] = str(exc)
        if page is not None:
            try:
                page.screenshot(path=str(args.output / 'failure.png'), full_page=True)
            except Exception:
                pass
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        (args.output / 'status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=15)
        listener.close()
        database.close()
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{db_name}" WITH (FORCE)')
        admin_engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
