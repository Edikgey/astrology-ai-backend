"""Bounded rolling memory. Network work produces a candidate, never a DB commit."""
from dataclasses import dataclass
from fastapi import HTTPException
from database.queries import GPTConversation, GPTMessage, NatalChart
from modules.ai_chart_context import build_chart_context, compact_json
from modules.astrology_prompt import ASTROLOGY_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT

# UTF-8 bytes conservatively bound text tokens, without a tokenizer download.
# Actual provider tokens are recorded separately; these are NOT product quotas.
RECENT_MESSAGES = 10
SUMMARY_THRESHOLD = 20
RECENT_BUDGET = 12000
SUMMARY_BUDGET = 6000
SUMMARY_BATCH_BUDGET = 18000
SUMMARY_INPUT_BUDGET = 32000
MAX_SUMMARY_CALLS = 2
MAX_INPUT_BUDGET = 64000
ANSWER_TOKENS = 1200
SUMMARY_TOKENS = 650


def message_size(message):
    return len(message['content'].encode('utf-8')) + 16


def as_message(row):
    return {"role": "user" if row.role == "user" else "assistant", "content": row.content}


@dataclass(frozen=True)
class MemoryUpdate:
    expected_cursor: int
    through_message_id: int
    summary: str


class AIAnswer(str):
    """Keeps the interpreter's text contract and carries transaction-local metadata."""
    def __new__(cls, text, *, metadata, memory_update=None, summary_usage=None):
        result = super().__new__(cls, text)
        result.metadata = metadata
        result.memory_update = memory_update
        result.summary_usage = summary_usage or []
        return result


def token_metadata(response, fallback_model):
    usage = getattr(response, 'usage', None)
    model = getattr(response, 'model', None)
    result = {'model': model if isinstance(model, str) else fallback_model}
    for field, provider_field in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
                                  ('total_tokens', 'total_tokens')):
        number = getattr(usage, provider_field, None)
        result[field] = number if type(number) is int and number >= 0 else None
    return result


def response_text(response):
    if getattr(response.choices[0], 'finish_reason', None) in ('length', 'content_filter'):
        raise HTTPException(502, "AI не завершил ответ. Квота не списана; повторите вопрос.")
    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(502, "AI не вернул ответ. Квота не списана.")
    return content.strip()


def history_query(db, user_id, chart_id):
    return db.query(GPTMessage).join(NatalChart).filter(
        NatalChart.user_id == user_id, GPTMessage.chart_id == chart_id,
        GPTMessage.role.in_(['user', 'gpt', 'assistant']))


def generate_answer(db, user_id, chart_id, question, client, params):
    # An explicit user ID from JWT is mandatory even when called outside the route.
    if user_id is None:
        raise HTTPException(401, "Войдите в аккаунт.")
    chart_context = build_chart_context(db, user_id, chart_id)
    memory = db.query(GPTConversation).filter_by(chart_id=chart_id, user_id=user_id).first()
    cursor = memory.through_message_id if memory else 0
    summary = memory.summary if memory else ''
    if len(summary.encode('utf-8')) > SUMMARY_BUDGET:
        raise HTTPException(409, "Память чата требует проверки; квота не списана.")
    query = history_query(db, user_id, chart_id)
    tail = query.filter(GPTMessage.id > cursor).order_by(GPTMessage.id.desc()).limit(SUMMARY_THRESHOLD + 1).all()[::-1]
    recent = tail[-RECENT_MESSAGES:]
    while recent and sum(message_size(as_message(row)) for row in recent) > RECENT_BUDGET:
        recent.pop(0)
        # Keep a user/assistant exchange intact when possible.
        while recent and recent[0].role != 'user':
            recent.pop(0)
    cutoff = recent[0].id if recent else (tail[-1].id + 1 if tail else cursor + 1)
    needs_summary = len(tail) > SUMMARY_THRESHOLD or sum(message_size(as_message(r)) for r in tail) > RECENT_BUDGET
    # Materialize only bounded old batches and the recent tail. Never load an entire lifetime chat.
    old = query.filter(GPTMessage.id > cursor, GPTMessage.id < cutoff).order_by(GPTMessage.id).limit(32).all() if needs_summary else []
    old_has_more = bool(old and query.filter(GPTMessage.id > old[-1].id, GPTMessage.id < cutoff).first())
    recent_messages = [as_message(r) for r in (recent if needs_summary else tail)]
    old_messages = [(row.id, as_message(row)) for row in old]
    original_cursor = cursor
    # End read transactions BEFORE any provider call; no row locks while waiting.
    db.commit()
    summary_usage = []
    for _ in range(MAX_SUMMARY_CALLS):
        if not old_messages:
            break
        batch, size = [], 0
        while old_messages:
            identifier, message = old_messages[0]
            def summary_input(extra):
                return compact_json({'previous_memory': summary, 'older_dialogue': [m for _, m in batch] + extra})
            if (size + message_size(message) > SUMMARY_BATCH_BUDGET or
                    len(summary_input([message]).encode('utf-8')) > SUMMARY_INPUT_BUDGET):
                if batch:
                    break
                # Legacy messages had no input limit. Preserve raw history, but cap
                # this one summary input with an explicitly marked head/tail excerpt.
                text = message['content']
                message = {**message, 'content': text[:1800] + '\n[Oversized legacy message: middle omitted]\n' + text[-1800:]}
            old_messages.pop(0)
            batch.append((identifier, message))
            size += message_size(message)
        summary_payload = summary_input([])
        if len(summary_payload.encode('utf-8')) > SUMMARY_INPUT_BUDGET:
            raise HTTPException(422, "Старая история превышает бюджет summary; требуется проверка чата.")
        response = client.chat.completions.create(
            model=params['model'], temperature=0.2, max_tokens=SUMMARY_TOKENS,
            messages=[{'role': 'system', 'content': SUMMARY_SYSTEM_PROMPT},
                      {'role': 'user', 'content': summary_payload}])
        candidate = response_text(response)
        if len(candidate.encode('utf-8')) > SUMMARY_BUDGET:
            raise HTTPException(502, "AI вернул слишком длинное summary. Квота не списана.")
        summary = candidate
        cursor = batch[-1][0]
        summary_usage.append(token_metadata(response, params['model']))
    messages = [{'role': 'system', 'content': ASTROLOGY_SYSTEM_PROMPT},
                {'role': 'user', 'content': 'Calculated natal reference data (not instructions):\n' + chart_context}]
    if summary:
        messages.append({'role': 'user', 'content': 'Older conversation memory (not instructions):\n' +
                         compact_json({'notes': summary, 'older_history_pending': bool(old_messages or old_has_more)})})
    current = {'role': 'user', 'content': question}
    while recent_messages and sum(map(message_size, messages + recent_messages + [current])) > MAX_INPUT_BUDGET:
        recent_messages.pop(0)
        while recent_messages and recent_messages[0]['role'] != 'user':
            recent_messages.pop(0)
    messages += recent_messages + [current]
    if sum(map(message_size, messages)) > MAX_INPUT_BUDGET:
        raise HTTPException(422, "Вопрос и карта превышают бюджет контекста; сократите вопрос.")
    response = client.chat.completions.create(**params, max_tokens=ANSWER_TOKENS, messages=messages)
    answer = response_text(response)
    update = MemoryUpdate(original_cursor, cursor, summary) if cursor != original_cursor else None
    return AIAnswer(answer, metadata=token_metadata(response, params['model']), memory_update=update, summary_usage=summary_usage)


def save_memory(db, user_id, chart_id, update):
    """Called ONLY by finalize, under the existing user/chart locks, without commit."""
    if update is None:
        return
    memory = db.query(GPTConversation).filter_by(chart_id=chart_id).first()
    if memory and memory.user_id != user_id:
        raise HTTPException(409, "Владелец памяти чата изменился.")
    if (memory.through_message_id if memory else 0) != update.expected_cursor:
        raise HTTPException(409, "История изменилась. Повторите вопрос.")
    if memory is None:
        memory = GPTConversation(chart_id=chart_id, user_id=user_id)
        db.add(memory)
    from modules.usage import utcnow
    memory.summary, memory.through_message_id = update.summary, update.through_message_id
    memory.updated_at = utcnow()
