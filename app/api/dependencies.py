from collections.abc import Callable

from fastapi import Request, Security
from fastapi.security import APIKeyCookie

from app.services.auth_service import AuthService, Principal

COOKIE = '__Host-tracedesk-session'


def authentication(service: AuthService) -> Callable[[Request, str | None], Principal]:
    cookie = APIKeyCookie(name=COOKIE, auto_error=False, scheme_name='SessionCookie')
    def authenticate(request: Request, token: str | None = Security(cookie)) -> Principal:
        principal = service.authenticate(token, request.headers.get('X-CSRF-Token'),
                                         write=request.method not in {'GET', 'HEAD', 'OPTIONS'})
        request.state.user_id = principal.user_id
        return principal
    return authenticate
