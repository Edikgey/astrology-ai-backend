"""SSE transport around the existing reservation/finalization transaction."""
import json
import logging
import time
from contextlib import closing
import anyio
import httpx
import openai
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from modules import usage
from modules.ai_conversation import AIAnswer, AnswerCut, stream_answer
from modules.ai_transit_guard import topic_of

logger = logging.getLogger(__name__)


def chat_subject(relationship_id, question):
    """Closed label of the journal line: natal|relationship|topic_career|topic_money|topic_love (TOPICS-1 DESIGN §2.6)."""
    if relationship_id is not None:
        return 'relationship'
    topic = topic_of(question)
    return f'topic_{topic}' if topic else 'natal'


def failure_reason(error):
    """Closed (reason, status) of an interrupted chat (BE-CHAT-OBS DESIGN §3.1). Pure, never raises.
    Reads only the class, status_code and, for 429, code compared with a constant; never the error text,
    body, request, response, headers or request id. Order matters: APITimeoutError is an APIConnectionError,
    httpx.TimeoutException is an httpx.TransportError."""
    if isinstance(error, AnswerCut):
        return ('unfinished' if error.outcome == 'interrupted' else 'other'), None
    if isinstance(error, (openai.APITimeoutError, httpx.TimeoutException, TimeoutError)):
        return 'timeout', None
    if isinstance(error, (openai.APIConnectionError, httpx.TransportError)):
        return 'connection', None
    if isinstance(error, openai.APIStatusError):
        status = getattr(error, 'status_code', None)
        if type(status) is not int or not 400 <= status <= 599:
            return 'other', None
        if status in (401, 403):
            return 'auth', status
        if status == 404:
            return 'not_found', status
        if status == 429:
            return ('quota' if getattr(error, 'code', None) == 'insufficient_quota' else 'rate_limit'), status
        return ('bad_request' if status < 500 else 'api_status_5xx'), status
    if isinstance(error, openai.APIError):
        return 'api_error', None
    return 'other', None


def log_outcome(outcome, subject, model, output_tokens, started, error=None):
    """One line per abnormal chat outcome: closed labels and numbers only, never text or ids.
    outcome=interrupted also carries reason= and status= from failure_reason(error)."""
    cause = ''
    if outcome == 'interrupted':
        reason, status = failure_reason(error)
        cause = f' reason={reason} status={status if type(status) is int else "none"}'
    logger.warning('AI chat outcome=%s%s subject=%s model=%s output_tokens=%s duration_ms=%d',
                   outcome, cause, subject, model or 'none',
                   output_tokens if type(output_tokens) is int else 'none',
                   round((time.monotonic() - started) * 1000))


def chat_events(bind, user_id, chart_id, question, reservation_id, client, params, *, relationship_id=None):
    # FastAPI may close its dependency session before the response is streamed.
    # This session belongs solely to the stream and is never used concurrently.
    started = time.monotonic()
    subject = chat_subject(relationship_id, question)
    with Session(bind=bind, autoflush=False) as db:
        settled = False
        try:
            yield {'type': 'start'}
            with closing(stream_answer(db, user_id, chart_id, question, client, params, **({"relationship_id": relationship_id} if relationship_id is not None else {}))) as parts:
                for part in parts:
                    if isinstance(part, AIAnswer):
                        usage.finalize(db, user_id, reservation_id, question, part)
                        settled = True
                        if any(item.get('discarded') for item in part.summary_usage):
                            log_outcome('summary_degraded', subject, params['model'],
                                        part.metadata.get('output_tokens'), started)
                        yield {'type': 'done', 'response': str(part),
                               'follow_up_suggestions': part.follow_up_suggestions}
                    else:
                        yield {'type': 'delta', 'text': part}
        except Exception as error:
            # Refusals before the call (ownership, budget, history) are not chat outcomes.
            if isinstance(error, AnswerCut):
                log_outcome(error.outcome, subject, params['model'], error.output_tokens, started, error)
            elif not isinstance(error, HTTPException):
                log_outcome('interrupted', subject, params['model'], None, started, error)
            usage.release(db, user_id, reservation_id)
            settled = True
            yield {'type': 'error', 'status': error.status_code if isinstance(error, HTTPException) else 502,
                   'detail': error.detail if isinstance(error, HTTPException) else
                   'Ответ прервался. Обновите историю перед повторной отправкой.'}
        finally:
            if not settled:
                usage.release(db, user_id, reservation_id)


def streaming_response(bind, user_id, chart_id, question, reservation_id, client, params, *, relationship_id=None):
    worker = chat_events(bind, user_id, chart_id, question, reservation_id, client, params, relationship_id=relationship_id)
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
