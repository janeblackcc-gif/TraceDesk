from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimit:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http' or scope['method'] in {'GET', 'HEAD', 'OPTIONS'}:
            await self.app(scope, receive, send)
            return
        maximum = 21 * 1024 * 1024 if scope['path'].endswith('/documents') else 16 * 1024
        buffered: list[Message] = []
        size = 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > maximum:
                await JSONResponse({'error': {'code': 'REQUEST_TOO_LARGE', 'message': '请求体超过上限。',
                    'request_id': scope.get('state', {}).get('request_id', ''), 'retryable': False, 'details': {}}},
                    status_code=413)(scope, receive, send)
                return
            buffered.append(message)
            if not message.get('more_body'):
                break
        offset = 0

        async def replay() -> Message:
            nonlocal offset
            if offset < len(buffered):
                value = buffered[offset]
                offset += 1
                return value
            return await receive()

        await self.app(scope, replay, send)
