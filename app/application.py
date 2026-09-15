"""Team-mode entry point. Legacy anonymous routes are never mounted here."""
from contextlib import asynccontextmanager
import time
from uuid import uuid4
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import ROOT, Settings
from app.db.session import Database
from app.api.v1.auth import build_router
from app.api.v1.queries import build_query_router
from app.api.v1.indexes import build_index_router
from app.api.v1.catalog import build_catalog_router
from app.api.v1.operations import build_operations_router
from app.api.legacy import build_legacy_router
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.api.body_limit import BodyLimit
from app.audit.service import audit
from app.services.auth_service import AuthService
from app.services.errors import DomainError
from app.services.document_service import DocumentService
from app.storage.object_store import ObjectStore
from app.observability import logging as operational_log
from app.observability.metrics import Metrics
from app.observability.health import readiness
from app.observability.tracing import provider as telemetry_provider


def create_application(settings: Settings) -> FastAPI:
    if settings.database_url is None:
        raise ValueError('DATABASE_URL is required for team mode')
    database = Database(settings.database_url)
    operational_log.configure()
    telemetry = telemetry_provider('tracedesk-web')
    tracer = telemetry.get_tracer('tracedesk.http')
    metrics = Metrics(database, settings.data_dir / 'objects')

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            status = database.readiness()
            if not status.ready:
                raise RuntimeError(status.code)
            yield
        finally:
            telemetry.shutdown()
            database.close()

    app = FastAPI(title='TraceDesk Team API', version='0.3.0-dev', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.database = database
    app.state.telemetry = telemetry
    app.state.metrics = metrics
    app.state.auth = AuthService(database, settings.bootstrap_token)
    app.state.documents = DocumentService(database, ObjectStore(settings.data_dir / 'objects'))
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlparse(settings.public_origin).hostname, 'localhost', '127.0.0.1'])

    def error_response(request: Request, code: str, status: int, retryable: bool = False) -> JSONResponse:
        request.state.error_code = code
        headers = {'Retry-After': '60'} if status == 429 else None
        return JSONResponse({'error': {'code': code, 'message': code, 'request_id': request.state.request_id,
                                      'retryable': retryable, 'details': {}}}, status_code=status, headers=headers)

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        request.state.request_id = 'req_' + uuid4().hex
        started = time.perf_counter()
        method = request.method if request.method in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'} else 'OTHER'
        with tracer.start_as_current_span('http.request', record_exception=False, set_status_on_exception=False) as span:
            metrics.inflight.inc()
            try:
                if request.method not in {'GET', 'HEAD', 'OPTIONS'} and request.headers.get('origin') != settings.public_origin:
                    response = error_response(request, 'ORIGIN_REJECTED', 403)
                else:
                    response = await call_next(request)
            except Exception:
                response = error_response(request, 'INTERNAL_ERROR', 500)
            finally:
                metrics.inflight.dec()
            route = getattr(request.scope.get('route'), 'path', 'unmatched')
            elapsed = time.perf_counter() - started
            span.update_name(method + ' ' + route)
            span.set_attribute('http.response.status_code', response.status_code)
            try:
                metrics.requests.labels(method, route, str(response.status_code)).inc()
                metrics.latency.labels(method, route).observe(elapsed)
            except Exception:
                pass
            operational_log.event('http.request', request_id=request.state.request_id,
                user_id=getattr(request.state, 'user_id', None), route=route, status=response.status_code,
                latency_ms=elapsed * 1000, error_code=getattr(request.state, 'error_code', None),
                trace_id=f'{span.get_span_context().trace_id:032x}')
        response.headers.update({'X-Request-ID': request.state.request_id, 'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"})
        if request.url.path.startswith('/api/') and not request.url.path.startswith('/api/v1/'):
            response.headers.update({'Deprecation': 'true', 'Link': '</api/v1>; rel="successor-version"'})
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        if exc.status in {403, 404, 429}:
            try:
                with database.transaction() as session:
                    audit(session, 'access.denied', actor=getattr(request.state, 'user_id', None),
                          request_id=request.state.request_id, outcome='denied')
            except (SQLAlchemyError, DomainError):
                return error_response(request, 'AUDIT_UNAVAILABLE', 503, True)
        return error_response(request, exc.code, exc.status, exc.retryable)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's default response includes raw input (including passwords).
        return error_response(request, 'VALIDATION_ERROR', 422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        conflict = isinstance(exc, IntegrityError)
        return error_response(request, 'RESOURCE_CONFLICT' if conflict else 'DATABASE_UNAVAILABLE',
                              409 if conflict else 503, not conflict)

    app.include_router(build_router(database, app.state.auth, app.state.documents))
    app.include_router(build_query_router(database, app.state.auth))
    app.include_router(build_index_router(database, app.state.auth, settings))
    app.include_router(build_catalog_router(database, app.state.auth, app.state.documents))
    app.include_router(build_legacy_router(database, app.state.auth))
    app.include_router(build_operations_router(database, app.state.auth, metrics))
    app.mount('/static', StaticFiles(directory=ROOT / 'web'), name='static')

    @app.get('/', include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(ROOT / 'web' / 'team.html')

    @app.get('/livez')
    def livez() -> dict[str, str]:
        return {'status': 'alive'}

    @app.get('/readyz')
    def readyz() -> JSONResponse:
        status = readiness(database, app.state.documents.objects.root)
        return JSONResponse({'ready': status.ready, 'code': status.code}, status_code=200 if status.ready else 503)

    return app
