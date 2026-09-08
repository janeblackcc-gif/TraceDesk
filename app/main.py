from __future__ import annotations
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .ingest import InputError, MAX_BYTES
from . import __version__
from .config import Settings
from .providers import ModelUnavailable, Ollama
from .service import ROOT, Service, IndexRequired

class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    collection: str = Field(min_length=1, max_length=60)
    version: str | None = Field(default=None, max_length=60)
    profile: Literal['evidence', 'ollama'] = 'evidence'
    method: Literal['bm25', 'hybrid', 'dense'] = 'hybrid'
    conversation_id: str | None = Field(default=None, max_length=80)

class ScopeBody(BaseModel):
    collection: str = Field(min_length=1, max_length=60)
    version: str | None = Field(default=None, max_length=60)

class ActivateBody(ScopeBody):
    version: str = Field(min_length=1, max_length=60)

class BodyLimit:
    """Bound request bytes before multipart parsing; does not trust Content-Length."""
    def __init__(self, app, max_bytes=MAX_BYTES + 128 * 1024):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] in {'GET', 'HEAD', 'OPTIONS'}:
            return await self.app(scope, receive, send)
        length = dict(scope['headers']).get(b'content-length', b'0')
        try:
            too_large = int(length) > self.max_bytes
        except ValueError:
            too_large = True
        if too_large:
            return await JSONResponse({'detail': '请求体超过上传上限。'}, status_code=413)(scope, receive, send)
        # The local single-user service caps the entire body before form-parser CPU work.
        messages, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > self.max_bytes:
                return await JSONResponse({'detail': '请求体超过上传上限。'}, status_code=413)(scope, receive, send)
            messages.append(message)
            if not message.get('more_body'):
                break
        async def replay():
            return messages.pop(0) if messages else await receive()
        await self.app(scope, replay, send)

def create_app(db_path: str | Path | None = None, provider=None) -> FastAPI:
    settings = Settings.load()
    service = Service(db_path or settings.data_dir / 'tracedesk.db', provider or Ollama(settings=settings))
    @asynccontextmanager
    async def lifespan(app):
        yield
        service.store.close()
        if hasattr(service.provider, 'client'):
            service.provider.client.close()

    app = FastAPI(title='TraceDesk · 溯知 API', version=__version__,
                  description='单人本地文档证据工作台。证据模式与本地模型模式严格区分。', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])

    @app.middleware('http')
    async def secure(request: Request, call_next):
        origin = request.headers.get('origin')
        if origin and request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            parsed = urlparse(origin)
            if parsed.scheme not in {'http', 'https'} or parsed.netloc != request.headers.get('host'):
                return JSONResponse({'detail': '禁止跨站写入请求。请从本机页面操作。'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    def run(func, *args, **kwargs):
        if not service.lock.acquire(timeout=.05):
            raise HTTPException(409, '正在处理另一个操作，请在完成后重试。')
        try:
            return func(*args, **kwargs)
        except IndexRequired as exc:
            raise HTTPException(409, str(exc)) from exc
        except InputError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ModelUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            service.lock.release()

    @app.get('/api/health', tags=['system'])
    def health():
        return {'status': 'ok', 'version': __version__, 'single_user': True, 'default_profile': 'evidence'}

    @app.get('/api/library', tags=['library'])
    def library():
        return run(service.store.library)

    @app.post('/api/demo', tags=['library'])
    def load_demo():
        return run(service.load_demo)

    @app.post('/api/documents', tags=['library'])
    def upload(file: UploadFile = File(...), collection: str = Form(..., max_length=60),
               version: str = Form(..., max_length=60)):
        try:
            data = file.file.read(MAX_BYTES + 1)
            return run(service.store.import_document, file.filename or 'untitled', data, collection.strip(), version.strip())
        finally:
            file.file.close()

    @app.delete('/api/documents/{doc_id}', tags=['library'])
    def delete(doc_id: str):
        run(service.store.delete, doc_id)
        return {'deleted': True, 'note': '分块、向量已级联删除；本地追踪已清空。已导出的文件不受影响。'}

    @app.post('/api/active-version', tags=['library'])
    def activate(body: ActivateBody):
        run(service.store.activate, body.collection, body.version)
        return {'active_version': body.version, 'conversation_reset': True}

    @app.get('/api/sources/{doc_id}', tags=['evidence'])
    def source(doc_id: str, page: int = 1):
        return run(service.store.source, doc_id, page)

    @app.post('/api/ask', tags=['query'])
    def ask(body: AskBody):
        return run(service.ask, **body.model_dump())

    @app.get('/api/models', tags=['model'])
    def model_status():
        return run(service.provider.status)

    @app.post('/api/index', tags=['model'])
    def index(body: ScopeBody):
        return run(service.index, body.collection, body.version)

    @app.get('/api/traces/{trace_id}', tags=['evaluation'])
    def trace(trace_id: str):
        def fetch():
            row = service.store.db.execute('SELECT payload FROM traces WHERE id=?', (trace_id,)).fetchone()
            if not row:
                raise InputError('追踪不存在或已经清理。')
            return json.loads(row['payload'])
        return run(fetch)

    @app.get('/api/evaluation', tags=['evaluation'])
    def evaluation_report():
        path = ROOT / 'evidence/evaluation_report.json'
        if not path.exists():
            return {'notice': '尚无评测结果。请运行随包合成集回归。', 'methods': {}}
        return json.loads(path.read_text('utf-8'))

    @app.post('/api/evaluate', tags=['evaluation'])
    def evaluate_now():
        from .evaluation import evaluate
        # Runs an isolated demo DB and does NOT write into user's library or published baseline.
        return run(evaluate)

    app.mount('/static', StaticFiles(directory=ROOT / 'web'), name='static')

    @app.get('/', include_in_schema=False)
    def home():
        return FileResponse(ROOT / 'web/index.html')
    return app

app = create_app()
