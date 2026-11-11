"""Bound auth/guest JSON before parsing; payment/webhook traffic is untouched."""
from starlette.responses import JSONResponse


class AuthBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        limited = (scope['type'] == 'http' and scope['method'] == 'POST' and
                   (scope['path'].startswith('/auth/') or scope['path'] == '/natal-chart'))
        if not limited:
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] != 'http.request':
                return
            size += len(message.get('body', b''))
            if size > 32768:
                return await JSONResponse({'detail': 'Запрос слишком большой.'}, status_code=413)(scope, receive, send)
            chunks.append(message.get('body', b''))
            if not message.get('more_body'):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()

        return await self.app(scope, bounded_receive, send)
