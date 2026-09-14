"""SSE transport around the existing reservation/finalization transaction."""
import json
from contextlib import closing
import anyio
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from modules import usage
from modules.ai_conversation import AIAnswer, stream_answer


def chat_events(bind, user_id, chart_id, question, reservation_id, client, params):
    # FastAPI may close its dependency session before the response is streamed.
    # This session belongs solely to the stream and is never used concurrently.
    with Session(bind=bind, autoflush=False) as db:
        settled = False
        try:
            yield {'type': 'start'}
            with closing(stream_answer(db, user_id, chart_id, question, client, params)) as parts:
                for part in parts:
                    if isinstance(part, AIAnswer):
                        usage.finalize(db, user_id, reservation_id, question, part)
                        settled = True
                        yield {'type': 'done', 'response': str(part),
                               'follow_up_suggestions': part.follow_up_suggestions}
                    else:
                        yield {'type': 'delta', 'text': part}
        except Exception as error:
            usage.release(db, user_id, reservation_id)
            settled = True
            yield {'type': 'error', 'status': error.status_code if isinstance(error, HTTPException) else 502,
                   'detail': error.detail if isinstance(error, HTTPException) else
                   'Ответ прервался. Обновите историю перед повторной отправкой.'}
        finally:
            if not settled:
                usage.release(db, user_id, reservation_id)


def streaming_response(bind, user_id, chart_id, question, reservation_id, client, params):
    worker = chat_events(bind, user_id, chart_id, question, reservation_id, client, params)
    # Prime the cleanup scope even if sending HTTP headers fails before iteration.
    start = next(worker)

    class ChatStreamingResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                # Starlette can cancel/raise during send while body() is suspended
                # at yield. async-for does not close that generator automatically.
                with anyio.CancelScope(shield=True):
                    await self.body_iterator.aclose()
                    await run_in_threadpool(worker.close)

    async def body():
        end = object()
        try:
            yield 'data: ' + json.dumps(start) + '\n\n'
            while True:
                # Wait for each sync provider read to finish before closing the
                # generator on disconnect. No blocking network reads on the event loop.
                with anyio.CancelScope(shield=True):
                    item = await run_in_threadpool(next, worker, end)
                if item is end:
                    break
                yield 'data: ' + json.dumps(item, ensure_ascii=False) + '\n\n'
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(worker.close)

    return ChatStreamingResponse(body(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})
