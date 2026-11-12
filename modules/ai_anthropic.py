"""Claude (Anthropic Messages API) as the chat provider. Imported only when LUNARIA_CHAT_PROVIDER=anthropic.

Same contract as the OpenAI path in ai_conversation: one provider call per attempt (max_retries=0), the same prompt
and chart block, the same JSON answer envelope, AnswerCut with a closed outcome label for abnormal endings.
"""
import anthropic
from fastapi import HTTPException
from jiter import from_json
from modules.ai_conversation import (ANSWER_FORMAT, ANSWER_TOKENS, REASONING_EXTRA_TOKENS, SUMMARY_TOKENS, AnswerCut)
from modules.usage import OPENAI_TIMEOUT_SECONDS

DEFAULT_MODEL = 'claude-haiku-5-5'
EFFORTS = ('low', 'medium', 'high')     # xhigh/max: costlier, and Haiku 5.5 may return empty answers there
REFUSAL_LABELS = ('cyber', 'bio', 'frontier_llm', 'general_harms', 'reasoning_extraction')
CACHE = {'type': 'ephemeral'}
# Structured outputs reject these keywords with a 400. The server still enforces them:
# answer_parts and FollowUpSuggestion check exactly 3 suggestions, their types and lengths.
UNSUPPORTED_SCHEMA_KEYS = ('minItems', 'maxItems', 'minLength', 'maxLength', 'title')


def build_client(env):
    return anthropic.Anthropic(api_key=env['ANTHROPIC_API_KEY'], base_url=env.get('ANTHROPIC_BASE_URL') or None,
                               timeout=OPENAI_TIMEOUT_SECONDS, max_retries=0)


def chat_params(env):
    """(params, effort_valid). An unknown effort falls back to low instead of failing the import."""
    model = (env.get('ANTHROPIC_CHAT_MODEL') or '').strip() or DEFAULT_MODEL
    effort = (env.get('ANTHROPIC_CHAT_EFFORT') or 'low').strip().lower()
    valid = effort in EFFORTS
    return {'provider': 'anthropic', 'model': model, 'effort': effort if valid else 'low'}, valid


def supported_schema(node):
    if isinstance(node, list):
        return [supported_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    result = {}
    for key, value in node.items():
        if key in UNSUPPORTED_SCHEMA_KEYS:
            continue
        # property names are data, not keywords: a property called "title" must survive
        result[key] = ({name: supported_schema(sub) for name, sub in value.items()} if key == 'properties'
                       else supported_schema(value))
    if result.get('type') == 'object':
        result['additionalProperties'] = False
    return result


ANSWER_SCHEMA = supported_schema(ANSWER_FORMAT['json_schema']['schema'])


def to_request(params, messages, ceiling, *, structured):
    """Product messages ([system, user, ...]) -> Messages API arguments.

    Same-role neighbours become one turn of separate text blocks; empty history texts are skipped (an empty
    text block is an API error). The answer caches the prompt and the chart block (first user block)."""
    system, turns = messages[0]['content'], []
    for message in messages[1:]:
        if not message['content'].strip():
            continue
        block = {'type': 'text', 'text': message['content']}
        if structured and not turns:
            block['cache_control'] = CACHE
        if turns and turns[-1]['role'] == message['role']:
            turns[-1]['content'].append(block)
        else:
            turns.append({'role': message['role'], 'content': [block]})
    output_config = {'effort': params['effort']}
    if structured:
        system = [{'type': 'text', 'text': system, 'cache_control': CACHE}]
        output_config['format'] = {'type': 'json_schema', 'schema': ANSWER_SCHEMA}
    return {'model': params['model'], 'max_tokens': ceiling + REASONING_EXTRA_TOKENS, 'system': system,
            'messages': turns, 'thinking': {'type': 'adaptive'}, 'output_config': output_config}


def count(usage, field):
    number = getattr(usage, field, None)
    return number if type(number) is int and number >= 0 else None


def token_metadata(message, fallback_model):
    """gpt_usage columns: full input = input + cache write + cache read; output includes thinking."""
    usage = getattr(message, 'usage', None)
    model = getattr(message, 'model', None)
    fresh, output = count(usage, 'input_tokens'), count(usage, 'output_tokens')
    cached = (count(usage, 'cache_creation_input_tokens') or 0) + (count(usage, 'cache_read_input_tokens') or 0)
    full = None if fresh is None else fresh + cached
    return {'model': model if isinstance(model, str) else fallback_model, 'input_tokens': full,
            'output_tokens': output, 'total_tokens': None if full is None or output is None else full + output}


def refusal_label(message):
    details = getattr(message, 'stop_details', None)
    category = details.get('category') if isinstance(details, dict) else getattr(details, 'category', None)
    if category is None:
        return 'none'
    return category if category in REFUSAL_LABELS else 'other'


def final_text(message):
    """Branch on stop_reason (never on content position: a thinking block may come first)."""
    reason, spent = message.stop_reason, count(getattr(message, 'usage', None), 'output_tokens')
    if reason == 'max_tokens':
        raise AnswerCut('truncated', spent)
    if reason == 'refusal':
        raise AnswerCut('filtered', spent, refusal=refusal_label(message))
    if reason != 'end_turn':
        raise AnswerCut('interrupted', spent)
    text = ''.join(block.text for block in message.content if block.type == 'text')
    if not text.strip():
        raise HTTPException(502, "AI не вернул ответ. Квота не списана.")
    return text.strip()


def complete(client, params, messages):
    message = client.messages.create(**to_request(params, messages, ANSWER_TOKENS, structured=True))
    return final_text(message), token_metadata(message, params['model'])


def summarize(client, params, system, payload):
    """(summary or None, spent). A cut or refused summary is dropped; network errors propagate."""
    message = client.messages.create(**to_request(
        params, [{'role': 'system', 'content': system}, {'role': 'user', 'content': payload}], SUMMARY_TOKENS,
        structured=False))
    spent = token_metadata(message, params['model'])
    try:
        return final_text(message), spent
    except AnswerCut:
        return None, spent


def stream(client, params, messages):
    """Yields new pieces of the `answer` field; returns (text, metadata) or raises AnswerCut.
    Closing this generator exits the SDK stream context, which closes the provider connection."""
    snapshot, emitted = '', ''
    with client.messages.stream(**to_request(params, messages, ANSWER_TOKENS, structured=True)) as events:
        for event in events:
            if event.type != 'content_block_delta' or event.delta.type != 'text_delta':
                continue        # message_start, thinking/signature, ping, unknown future events
            snapshot += event.delta.text
            try:
                partial = from_json(snapshot.encode('utf-8'), partial_mode='trailing-strings')
            except ValueError:
                continue
            text = partial.get('answer') if isinstance(partial, dict) else None
            if isinstance(text, str) and text.startswith(emitted) and len(text) > len(emitted):
                delta, emitted = text[len(emitted):], text
                yield delta
        message = events.get_final_message()
    return final_text(message), token_metadata(message, params['model'])
