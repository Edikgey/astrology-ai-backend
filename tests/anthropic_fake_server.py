"""In-process fake of the Anthropic Messages API for tests (CHAT-CLAUDE S1).

Loopback only (127.0.0.1, free port), stdlib only, no real network and no real key.  The real ``anthropic`` SDK talks to it
over plain HTTP, so a test exercises the SDK's own SSE parsing, error mapping and retry settings.

What it serves: ``POST /v1/messages`` - a stream (``"stream": true``, server-sent events in the official order:
message_start, content blocks, message_delta, message_stop; ping and an unknown event may be mixed in) or a plain JSON message -
and ``GET /health``.  Every request is recorded (path, lower-cased headers, parsed JSON body, raw body bytes) in
``FakeAnthropic.requests``; nothing is ever written to a log.

A test queues ``Scenario`` objects (one is consumed per request).  With an empty queue the server answers with a valid
reply: the JSON answer envelope when the request carries ``output_config.format`` and a short plain text otherwise
(that is the memory-summary request).  ``Scenario`` can also model: HTTP errors, a dropped connection, a stall, an ``error``
event in the middle of a stream, a connection cut in the middle of a stream, refusals, ``max_tokens`` and odd stop reasons.
"""
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SUGGESTIONS = [
    {'type': 'deepen', 'text': 'Что усиливает эту реакцию?'},
    {'type': 'personalize', 'text': 'Как мне заметить это в повседневной жизни?'},
    {'type': 'explore', 'text': 'Как это связано с доверием?'},
]
DEFAULT_ANSWER = 'Ответ Claude про вашу карту.'
DEFAULT_SUMMARY = 'Краткая память разговора.'
DEFAULT_USAGE = {'input_tokens': 11, 'cache_creation_input_tokens': 3000, 'cache_read_input_tokens': 500, 'output_tokens': 77}
SIGNATURE = 'EuoBCkYIDBgCKkAfake-signature-not-a-real-one'


def envelope(answer=DEFAULT_ANSWER, suggestions=SUGGESTIONS, ascii_json=True):
    """The JSON the product asks the model to produce: the answer plus three follow-up suggestions."""
    return json.dumps({'answer': answer, 'follow_up_suggestions': suggestions}, ensure_ascii=ascii_json)


@dataclass
class Scenario:
    text: str = ''                      # whole assistant text; empty means no text block (a refusal before any text)
    chunks: list = None                 # explicit text_delta pieces; default: every 5 characters
    stop_reason: object = 'end_turn'    # None is a message_delta with "stop_reason": null
    stop_details: dict = None
    usage: dict = field(default_factory=lambda: dict(DEFAULT_USAGE))
    model: str = 'claude-haiku-5-5'
    thinking: bool = True               # a thinking block (empty thinking_delta + one signature_delta) before the text
    extras: bool = True                 # one ping and one unknown event inside the stream
    http_error: tuple = None            # (status, error type, message): answered before any stream starts
    drop: bool = False                  # close the socket without answering
    stall: float = 0.0                  # seconds to wait before answering (client read timeout tests)
    error_after: int = None             # after N text chunks send an SSE `error` event instead of going on
    abort_after: int = None             # after N text chunks cut the connection without finishing the HTTP body
    byte_chunk: int = None              # write the stream in pieces of N bytes (splits UTF-8 characters)
    delay: float = 0.0                  # seconds between events (to keep a stream open)

    def pieces(self):
        if self.chunks is not None:
            assert ''.join(self.chunks) == self.text, 'chunks must add up to the text'
            return list(self.chunks)
        return [self.text[i:i + 5] for i in range(0, len(self.text), 5)]

    def message(self, started=False):
        usage = dict(self.usage)
        if started:
            usage['output_tokens'] = 1
        return {'id': 'msg_01FakeFakeFakeFake', 'type': 'message', 'role': 'assistant', 'content': [], 'model': self.model,
                'stop_reason': None, 'stop_sequence': None, 'usage': usage}

    def stream_events(self):
        events = [('message_start', {'type': 'message_start', 'message': self.message(started=True)})]
        index = 0
        if self.thinking:
            events += [
                ('content_block_start', {'type': 'content_block_start', 'index': 0,
                                         'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                         'delta': {'type': 'thinking_delta', 'thinking': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                         'delta': {'type': 'signature_delta', 'signature': SIGNATURE}}),
                ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
            ]
            index = 1
        if self.text:
            events.append(('content_block_start', {'type': 'content_block_start', 'index': index,
                                                   'content_block': {'type': 'text', 'text': ''}}))
            if self.extras:
                events.append(('ping', {'type': 'ping'}))
            for number, piece in enumerate(self.pieces()):
                if self.error_after is not None and number == self.error_after:
                    events.append(('error', {'type': 'error', 'error': {
                        'type': 'overloaded_error', 'message': 'КАНАРЕЙКА-ОШИБКА-ПОТОКА sk-ant-canary-stream'}}))
                    return events
                if self.abort_after is not None and number == self.abort_after:
                    events.append((None, None))     # marker: cut the connection here
                    return events
                events.append(('content_block_delta', {'type': 'content_block_delta', 'index': index,
                                                       'delta': {'type': 'text_delta', 'text': piece}}))
                if self.extras and number == 0:
                    events.append(('future_event_from_the_api', {'type': 'future_event_from_the_api', 'detail': 1}))
            events.append(('content_block_stop', {'type': 'content_block_stop', 'index': index}))
        delta = {'stop_reason': self.stop_reason, 'stop_sequence': None}
        if self.stop_details is not None:
            delta['stop_details'] = self.stop_details
        events.append(('message_delta', {'type': 'message_delta', 'delta': delta, 'usage': dict(self.usage)}))
        events.append(('message_stop', {'type': 'message_stop'}))
        return events

    def json_message(self):
        message = self.message()
        if self.thinking:
            message['content'].append({'type': 'thinking', 'thinking': '', 'signature': SIGNATURE})
        if self.text:
            message['content'].append({'type': 'text', 'text': self.text})
        message['stop_reason'] = self.stop_reason
        if self.stop_details is not None:
            message['stop_details'] = self.stop_details
        return message


def answer(text=DEFAULT_ANSWER, suggestions=SUGGESTIONS, ascii_json=True, **kwargs):
    """A normal, complete JSON answer."""
    return Scenario(text=envelope(text, suggestions, ascii_json), **kwargs)


def summary(text=DEFAULT_SUMMARY, **kwargs):
    """A normal memory summary: plain text."""
    return Scenario(text=text, **kwargs)


def refusal(category='general_harms', text='', explanation='КАНАРЕЙКА-ОБЪЯСНЕНИЕ-ОТКАЗА', **kwargs):
    details = {'type': 'refusal', 'category': category, 'explanation': explanation}
    return Scenario(text=text, stop_reason='refusal', stop_details=details, **kwargs)


def error(status, kind='api_error', message='КАНАРЕЙКА-ТЕКСТ-ОШИБКИ sk-ant-canary-http'):
    return Scenario(http_error=(status, kind, message))


class Recorded:
    def __init__(self, path, headers, raw):
        self.path, self.headers, self.raw = path, headers, raw
        self.body = json.loads(raw) if raw else {}

    @property
    def stream(self):
        return self.body.get('stream') is True

    @property
    def is_summary(self):
        return 'format' not in (self.body.get('output_config') or {})


class FakeAnthropic:
    def __init__(self):
        self.requests = []
        self.scripted = []
        self.disconnected = threading.Event()   # set when a client went away while a stream was being written
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):        # nothing is ever logged: no bodies, no headers
                pass

            def do_GET(self):
                self.send_response(200 if self.path == '/health' else 404)
                self.send_header('Content-Length', '2')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b'ok')

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                record = Recorded(self.path, {k.lower(): v for k, v in self.headers.items()}, raw)
                with fake._lock:
                    fake.requests.append(record)
                    scenario = fake.scripted.pop(0) if fake.scripted else None
                if scenario is None:
                    scenario = summary() if record.is_summary else answer()
                try:
                    fake.respond(self, record, scenario)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    fake.disconnected.set()
                self.close_connection = True

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def queue(self, *scenarios):
        with self._lock:
            self.scripted.extend(scenarios)
        return self

    def answer_requests(self):
        return [r for r in self.requests if not r.is_summary]

    def summary_requests(self):
        return [r for r in self.requests if r.is_summary]

    # --- wire ---
    def respond(self, handler, record, scenario):
        if scenario.stall:
            time.sleep(scenario.stall)
        if scenario.drop:
            handler.connection.shutdown(socket.SHUT_RDWR)
            return
        if scenario.http_error:
            status, kind, message = scenario.http_error
            payload = json.dumps({'type': 'error', 'error': {'type': kind, 'message': message},
                                  'request_id': 'req_fake'}).encode()
            handler.send_response(status)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(payload)))
            handler.send_header('retry-after', '0')
            handler.send_header('Connection', 'close')
            handler.end_headers()
            handler.wfile.write(payload)
            return
        if not record.stream:
            payload = json.dumps(scenario.json_message()).encode()
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(payload)))
            handler.send_header('Connection', 'close')
            handler.end_headers()
            handler.wfile.write(payload)
            return
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('Transfer-Encoding', 'chunked')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        for name, data in scenario.stream_events():
            if name is None:                      # cut the connection without the closing chunk
                handler.wfile.flush()
                handler.connection.shutdown(socket.SHUT_RDWR)
                return
            raw = f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode('utf-8')
            step = scenario.byte_chunk or len(raw)
            for start in range(0, len(raw), step):
                piece = raw[start:start + step]
                handler.wfile.write(b'%x\r\n' % len(piece) + piece + b'\r\n')
                handler.wfile.flush()
            if scenario.delay:
                time.sleep(scenario.delay)
        handler.wfile.write(b'0\r\n\r\n')
        handler.wfile.flush()
