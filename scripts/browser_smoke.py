"""Real HTTP/Edge smoke against an explicitly started, isolated EMPTY app.

Requires the optional Python playwright package and installed Microsoft Edge.
Does not start the app, install dependencies, download browsers, or run on import.
Example: python scripts/browser_smoke.py --url http://127.0.0.1:8766 --output evidence/browser-run-01
The run loads demo data and creates/replaces/deletes its own synthetic document.
Use a disposable app data directory; demo data remain available for inspection.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

TIMEOUT_MS = 120_000


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def save_json(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def source_quote_spans(lines: list[str], quote: str, start_line: int,
                       end_line: int) -> list[tuple[int, str]]:
    """Expected visible marks with original line numbers, including blank-line gaps.

    Empty lines contain no characters to mark. Comparing numbered spans preserves
    those gaps without inserting or trimming characters inside the visible marks.
    The source chunk bounds select the same occurrence as the UI source drawer.
    """
    require(1 <= start_line <= end_line <= len(lines), 'Invalid source line range')
    full = '\n'.join(lines)
    range_start = sum(len(line) + 1 for line in lines[:start_line - 1])
    range_end = len('\n'.join(lines[:end_line]))
    first = full.find(quote, range_start)
    require(quote and first >= range_start and first + len(quote) <= range_end,
            'Quote does not occur within the cited source line range')
    last = first + len(quote)
    spans = []
    offset = 0
    for number, line in enumerate(lines, 1):
        if line and offset < last and offset + len(line) > first:
            spans.append((number, line[max(0, first - offset):min(len(line), last - offset)]))
        offset += len(line) + 1
    return spans


class Run:
    def __init__(self, url, output):
        self.url = url
        self.output = output
        self.started = time.perf_counter()
        self.log = (output / 'milestones.jsonl').open('x', encoding='utf-8')
        self.snapshots = (output / 'api_results.jsonl').open('x', encoding='utf-8')
        self.checks = []
        self.console_errors = []
        self.page_errors = []
        self.request_failures = []
        self.page = None
        self.counter = 0

    def emit(self, event, **fields):
        row = {'timestamp': timestamp(), 'event': event, **fields}
        line = json.dumps(row, ensure_ascii=False)
        self.log.write(line + '\n')
        self.log.flush()
        print(line, flush=True)

    def checked(self, name, **details):
        self.checks.append({'name': name, **details})
        self.emit('check_passed', name=name, **details)

    def snapshot(self, name, status, body, request=None):
        row = {'timestamp': timestamp(), 'name': name, 'status': status,
               'request': request, 'body': body}
        self.snapshots.write(json.dumps(row, ensure_ascii=False) + '\n')
        self.snapshots.flush()
        return body

    def preflight_library(self):
        # Read-only before browser launch or any mutation; no environment proxies.
        self.emit('preflight_empty_library', url=self.url, timeout_seconds=120)
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request(self.url + '/api/library'), timeout=120) as response:
            body = json.loads(response.read().decode('utf-8'))
            self.snapshot('initial_library', response.status, body)
        require(isinstance(body, dict) and body.get('documents') == []
                and body.get('collections') == [],
                'Refusing to mutate a nonempty or unexpected library. Start a fresh isolated app data directory.')
        self.checked('initial_library_empty')

    def ui_action(self, name, method, path, action):
        self.emit('ui_action_started', name=name, method=method, path=path, timeout_seconds=120)
        with self.page.expect_response(
            lambda response: urlparse(response.url).path == path
            and response.request.method == method, timeout=TIMEOUT_MS
        ) as pending:
            action()
        response = pending.value
        # Browser fetch stays native; expect_response only observes the HTTP exchange.
        error = response.finished()
        require(error is None, f'{name}: response body failed: {error}')
        body = response.json()
        self.snapshot(name, response.status, body,
                      {'method': method, 'url': response.url,
                       'post_data': response.request.post_data if method != 'GET' else None})
        require(response.ok, f'{name}: HTTP {response.status}: {body}')
        self.page.wait_for_function("!document.querySelector('#ask-button').disabled", timeout=TIMEOUT_MS)
        self.emit('ui_action_finished', name=name)
        return body

    def api(self, name, path, method='GET', data=None, expected_status=200):
        self.emit('http_check_started', name=name, method=method, path=path)
        response = self.page.request.fetch(self.url + path, method=method, data=data,
                                           timeout=TIMEOUT_MS)
        body = response.json()
        self.snapshot(name, response.status, body, {'method': method, 'path': path, 'data': data})
        require(response.status == expected_status,
                f'{name}: expected HTTP {expected_status}, got {response.status}: {body}')
        return body

    def navigate(self, view):
        self.page.locator(f'.nav-item[data-view="{view}"]').click()
        self.page.locator(f'#{view}-view.active-view').wait_for(state='visible')

    def screenshot(self, name):
        self.counter += 1
        path = self.output / f'{self.counter:02d}_{name}.png'
        self.page.locator('#toast').wait_for(state='hidden', timeout=TIMEOUT_MS)
        self.page.screenshot(path=str(path), full_page=True, timeout=TIMEOUT_MS)
        widths = self.page.evaluate('''() => ({viewport: innerWidth,
            document: document.documentElement.scrollWidth, body: document.body.scrollWidth})''')
        require(max(widths['document'], widths['body']) <= widths['viewport'] + 1,
                f'Horizontal page overflow at {name}: {widths}')
        self.checked('screenshot_and_no_horizontal_overflow', name_of_view=name,
                     path=path.name, widths=widths)

    def scope(self, collection, version, profile):
        self.navigate('workspace')
        self.page.locator('#collection-select').select_option(collection)
        self.page.locator('#version-select').select_option(version)
        self.page.locator('#profile-select').select_option(profile)
        self.page.locator('#new-chat').click()
        require(self.page.locator('.answer-card').count() == 0, 'Scope/new chat did not clear old answer')

    def ask(self, name, question, collection, version, profile):
        self.scope(collection, version, profile)
        self.page.locator('#question').fill(question)
        body = self.ui_action(name, 'POST', '/api/ask', lambda: self.page.locator('#ask-button').click())
        self.page.locator('.answer-card').wait_for(state='visible')
        require(body.get('collection') == collection and body.get('version') == version,
                f'{name}: response escaped selected scope')
        require(body.get('actual_profile') == profile, f'{name}: unexpected fallback: {body.get("warning")}')
        sources = {source['id']: source for source in body.get('sources', [])}
        for source in sources.values():
            require(source['collection'] == collection and source['version'] == version,
                    f'{name}: retrieved source escaped scope')
        for claim in body.get('claims', []):
            require(claim.get('citations'), f'{name}: uncited claim')
            for cite in claim['citations']:
                source = sources.get(cite['chunk_id'])
                require(source is not None and cite['quote'] in source['text'],
                        f'{name}: citation is not exact source text')
        require(self.page.locator('.claim-text').count() == len(body.get('claims', [])),
                f'{name}: UI/API claim counts differ')
        self.checked(name, status=body.get('status'), profile=body.get('actual_profile'),
                     generation_model=body.get('generation_model'), trace_id=body.get('trace_id'))
        return body

    def highlight(self, name, answer):
        source_ids = {source['id']: source for source in answer['sources']}
        cite = answer['claims'][0]['citations'][0]
        source = source_ids[cite['chunk_id']]
        source_body = self.ui_action(name, 'GET', f'/api/sources/{source["doc_id"]}',
                                     lambda: self.page.locator('.citation-button').first.click())
        self.page.locator('#source-dialog[open]').wait_for(state='visible')
        marks = self.page.locator('#source-lines mark.quote-mark').evaluate_all('''nodes =>
            nodes.filter(node => node.textContent !== '').map(node =>
                [Number(node.closest('.source-line').dataset.line), node.textContent])''')
        actual_spans = [(number, text) for number, text in marks]
        expected_spans = source_quote_spans(source_body['lines'], cite['quote'],
                                             source['start_line'], source['end_line'])
        require(expected_spans and actual_spans == expected_spans,
                f'{name}: UI highlight differs from exact source spans: '
                f'expected={expected_spans!r}, actual={actual_spans!r}')
        require(source['version'] in self.page.locator('#source-meta').inner_text(),
                f'{name}: wrong source metadata')
        self.screenshot(name)
        self.page.locator('#close-source').click()
        self.page.locator('#source-dialog').wait_for(state='hidden')
        self.checked('exact_quote_highlight', quote=cite['quote'], source=source['filename'])

    def upload(self, name, collection, filename, hours, replacement):
        self.navigate('library')
        self.page.locator('#upload-collection').fill(collection)
        self.page.locator('#upload-version').fill('v1')
        text = ('# 云杉公开验收项目\n\n## 备份间隔\n'
                f'云杉公开验收项目的备份间隔为 {hours} 小时。\n'
                '本文是原创公开浏览器验收用虚构资料，只用于验证文档更新和引用。\n')
        (self.output / f'{name}_fixture.md').write_text(text, encoding='utf-8')
        self.page.locator('#upload-form input[type="file"]').set_input_files(
            {'name': filename, 'mimeType': 'text/markdown', 'buffer': text.encode('utf-8')})
        result = self.ui_action(name, 'POST', '/api/documents',
                                lambda: self.page.locator('#upload-form button[type="submit"]').click())
        require(result.get('status') == 'ready' and result.get('replaced') is replacement,
                f'{name}: upload did not produce the expected ready/replaced state')
        library = self.api(name + '_library', '/api/library')
        own = [doc for doc in library['documents'] if doc['collection'] == collection]
        require(len(own) == 1 and own[0]['id'] == result['id'] and own[0]['filename'] == filename,
                f'{name}: duplicate or wrong uploaded document')
        self.checked(name, document_id=result['id'], hours=hours)
        return result['id']

    def index(self, name, collection):
        self.scope(collection, 'v1', 'ollama')
        self.navigate('library')
        result = self.ui_action(name, 'POST', '/api/index',
                                lambda: self.page.locator('#index-vectors').click())
        require(result.get('total', 0) > 0 and result.get('indexed', 0) > 0
                and result.get('dimension', 0) > 0, f'{name}: real vectors were not newly indexed')
        self.checked(name, **result)

    def interval(self, name, collection, expected, forbidden=None):
        result = self.ask(name, '云杉公开验收项目的备份间隔是多少小时？', collection, 'v1', 'ollama')
        require(result.get('status') == 'answered' and result.get('generation_model'),
                f'{name}: no real generated answer')
        content = '\n'.join(claim['text'] for claim in result['claims'])
        alternatives = {7: '(?:7|七)', 13: '(?:13|十三)'}
        require(re.search(alternatives[expected] + r'\s*(?:个\s*)?小时', content),
                f'{name}: expected {expected} hours in answer: {content}')
        if forbidden is not None:
            require(not re.search(alternatives[forbidden] + r'\s*(?:个\s*)?小时', content),
                    f'{name}: stale interval in answer: {content}')
        self.checked('controlled_fixture_interval', hours=expected,
                     limitation='Checks a known numeric fact, not general semantic correctness')
        return result

    def execute(self):
        from playwright.sync_api import sync_playwright

        self.preflight_library()
        collection = '浏览器公开验收_' + uuid.uuid4().hex[:12]
        filename = 'public_browser_backup.md'
        with sync_playwright() as playwright:
            self.emit('launch_edge', channel='msedge', headless=True)
            browser = playwright.chromium.launch(channel='msedge', headless=True, timeout=TIMEOUT_MS)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                          device_scale_factor=1)
            context.set_default_timeout(TIMEOUT_MS)
            context.set_default_navigation_timeout(TIMEOUT_MS)
            self.page = context.new_page()
            self.page.on('pageerror', lambda error: self.page_errors.append(str(error)))
            self.page.on('console', lambda message: self.console_errors.append(message.text)
                         if message.type == 'error' else None)
            self.page.on('requestfailed', lambda request: self.request_failures.append(
                {'url': request.url, 'method': request.method, 'failure': request.failure}))
            try:
                # Navigate the real server page: no set_content, routing, init scripts, or fetch bridge.
                with self.page.expect_response(lambda r: urlparse(r.url).path == '/api/library') as pending:
                    response = self.page.goto(self.url + '/', wait_until='domcontentloaded')
                require(response is not None and response.ok, 'App navigation failed')
                initial = pending.value.json()
                self.snapshot('browser_initial_library', pending.value.status, initial)
                require(initial.get('documents') == [] and initial.get('collections') == [],
                        'Library changed since preflight; refusing to mutate it')
                self.page.wait_for_function("document.querySelector('#nav-doc-count').textContent === '0'")
                self.screenshot('desktop_empty_workspace')
                self.navigate('library')
                require(self.page.locator('#library-empty').is_visible(), 'Missing empty-library UI')
                self.screenshot('desktop_empty_library')
                self.navigate('workspace')
                self.ui_action('load_demo', 'POST', '/api/demo', lambda: self.page.locator('#load-demo').click())
                self.page.wait_for_function("document.querySelector('#nav-doc-count').textContent === '8'")
                demo = self.ask('demo_v2_port', '默认服务端口是多少？', 'Atlas 演示项目', 'v2', 'evidence')
                require('8088' in '\n'.join(c['text'] for c in demo['claims']), 'v2 port missing')
                self.highlight('desktop_exact_source_highlight', demo)
                demo_old = self.ask('demo_v1_port', '默认服务端口是多少？', 'Atlas 演示项目', 'v1', 'evidence')
                require('8000' in '\n'.join(c['text'] for c in demo_old['claims']), 'v1 port missing')
                self.screenshot('desktop_evidence_v1')
                self.navigate('settings')
                models = self.ui_action('models_ready', 'GET', '/api/models',
                                        lambda: self.page.locator('#check-models').click())
                require(models.get('ready') is True, f'Configured local models unavailable: {models}')
                self.checked('configured_models_ready', models=models)
                self.screenshot('desktop_models')
                first_id = self.upload('upload_original', collection, filename, 7, False)
                self.index('index_original', collection)
                original = self.interval('model_original_interval', collection, 7)
                self.highlight('desktop_model_highlight', original)
                refusal = self.ask('model_no_answer', '云杉公开验收项目备份服务的正式联系电话是多少？',
                                   collection, 'v1', 'ollama')
                require(refusal.get('status') == 'no_evidence' and refusal.get('claims') == [],
                        'Model did not refuse a missing fact')
                require(refusal.get('sources') and '模型判断' in refusal.get('warning', ''),
                        'No-answer stopped at retrieval; did not exercise model refusal')
                second_id = self.upload('replace_same_filename', collection, filename, 13, True)
                require(second_id != first_id, 'Replacement retained stale document identity')
                self.index('index_replacement', collection)
                updated = self.interval('model_updated_interval', collection, 13, forbidden=7)
                require(all(s['doc_id'] == second_id for s in updated['sources']),
                        'Replacement answer includes a stale source')
                self.screenshot('desktop_updated_answer')
                self.page.set_viewport_size({'width': 390, 'height': 844})
                self.screenshot('mobile_updated_answer')
                self.highlight('mobile_exact_source_highlight', updated)
                self.navigate('library')
                self.screenshot('mobile_library')
                self.page.set_viewport_size({'width': 1440, 'height': 1000})
                self.navigate('library')
                self.screenshot('desktop_library_before_delete')
                # Confirm only the uniquely named test collection's one uploaded document.
                row = self.page.locator('#document-rows tr').filter(has_text=collection)
                require(row.count() == 1, 'Cannot uniquely identify owned smoke document')
                def confirm_delete(dialog):
                    require(dialog.type == 'confirm' and filename in dialog.message,
                            'Unexpected deletion confirmation')
                    self.emit('delete_confirmation', message=dialog.message)
                    dialog.accept()
                self.page.once('dialog', confirm_delete)
                deleted = self.ui_action('delete_owned_document', 'DELETE', f'/api/documents/{second_id}',
                                         lambda: row.get_by_role('button', name=f'删除 v1 {filename}', exact=True).click())
                require(deleted.get('deleted') is True, 'Deletion was not confirmed by API')
                library = self.api('library_after_delete', '/api/library')
                require(len(library['documents']) == 8 and not any(
                    doc['collection'] == collection for doc in library['documents']),
                    'Deleted smoke document remains or demo documents changed')
                require(self.page.locator('.answer-card').count() == 0, 'Deletion retained stale UI answers')
                self.api('deleted_source_unavailable', f'/api/sources/{second_id}?page=1', expected_status=400)
                self.api('replaced_source_unavailable', f'/api/sources/{first_id}?page=1', expected_status=400)
                missing_scope = self.api('deleted_scope_cannot_answer', '/api/ask', method='POST',
                                         expected_status=400, data={
                                             'collection': collection, 'version': 'v1', 'profile': 'evidence',
                                             'question': '云杉公开验收项目的备份间隔是多少小时？', 'method': 'hybrid'})
                require(missing_scope.get('detail') == '该知识库没有所选版本，请重新选择。',
                        'Deleted scope failed for an unrelated reason instead of version validation')
                self.checked('deleted_document_has_no_stale_ui_or_api_source')
                self.screenshot('desktop_after_delete')
                self.navigate('workspace')
                self.page.set_viewport_size({'width': 390, 'height': 844})
                self.screenshot('mobile_after_delete')
                require(not self.page_errors, f'JavaScript page errors: {self.page_errors}')
                require(not self.console_errors, f'JavaScript console errors: {self.console_errors}')
                require(not self.request_failures, f'Browser network failures: {self.request_failures}')
                self.checked('no_javascript_or_network_errors')
            except Exception:
                try:
                    self.page.screenshot(path=str(self.output / 'failure.png'), full_page=True, timeout=10_000)
                except Exception as screenshot_error:
                    self.emit('failure_screenshot_unavailable', error=repr(screenshot_error))
                raise
            finally:
                context.close()
                browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Origin of a running isolated EMPTY local app')
    parser.add_argument('--output', required=True, type=Path, help='New output directory; existing paths rejected')
    args = parser.parse_args()
    origin = urlparse(args.url)
    if (origin.scheme != 'http' or origin.hostname not in {'127.0.0.1', 'localhost'}
            or origin.username or origin.password or origin.path not in {'', '/'}
            or origin.query or origin.fragment):
        parser.error('--url must be a loopback HTTP origin without credentials, path, query or fragment')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    run = Run(args.url.rstrip('/'), output)
    report = {'status': 'running', 'started_at': timestamp(), 'url': run.url,
              'browser': 'Microsoft Edge via Playwright channel=msedge, headless',
              'transport': 'real HTTP navigation and native browser fetch; no stubs or bridge',
              'request_timeout_seconds': 120,
              'scope': 'synthetic smoke; empty isolated app required; leaves demo data',
              'limitations': ['Controlled numeric fixture assertions are not a general semantic evaluation',
                              'Viewport emulation does not certify physical mobile/touch or other browser engines']}
    code = 1
    try:
        run.execute()
        report['status'] = 'passed'
        code = 0
    except Exception as error:
        report.update(status='failed', error=repr(error), traceback=traceback.format_exc())
        run.emit('failed', error=repr(error))
    finally:
        report.update(finished_at=timestamp(), elapsed_seconds=round(time.perf_counter() - run.started, 3),
                      exit_code=code, checks=run.checks, console_errors=run.console_errors,
                      page_errors=run.page_errors, request_failures=run.request_failures)
        save_json(output / 'report.json', report)
        run.emit('finished', status=report['status'], exit_code=code, report=str(output / 'report.json'))
        run.log.close()
        run.snapshots.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
