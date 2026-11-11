"""Attach explicit analytics headers; never inspect request/response bodies."""
from uuid import UUID
from modules import product_analytics as analytics
from modules.product_analytics_schema import ROUTE_KEYS


class ProductAnalyticsContext:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not analytics.enabled() or scope['path'].startswith('/analytics/'):
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        auth_paths = { '/auth/login': 'email', '/auth/verify-code': 'email', '/auth/google': 'google' }
        ctx = {'interactive': scope['path'] not in ('/payments/stripe/webhook', '/payments/lava/webhook')}
        ctx['auth'] = auth_paths.get(scope['path']) if scope['method'] == 'POST' else None
        ctx['reset'] = scope['path'] == '/auth/reset-password' and scope['method'] == 'POST'
        try:
            for name, header in (('visitor_id', b'x-lunaria-analytics-visitor'),
                                 ('revision', b'x-lunaria-analytics-revision'),
                                 ('session_id', b'x-lunaria-analytics-session')):
                value = UUID(headers[header].decode())
                if value.version != 4:
                    raise ValueError()
                ctx[name] = str(value)
            route = headers.get(b'x-lunaria-analytics-page', b'').decode()
            if route in ROUTE_KEYS:
                ctx['route_key'] = route
            viewport = headers.get(b'x-lunaria-analytics-viewport', b'unknown').decode()
            if viewport in ('small', 'medium', 'large', 'unknown'):
                ctx['viewport_class'] = viewport
        except (KeyError, ValueError, UnicodeError):
            ctx = {key: ctx[key] for key in ('interactive', 'auth', 'reset')}
        marker = analytics.context.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            analytics.context.reset(marker)
