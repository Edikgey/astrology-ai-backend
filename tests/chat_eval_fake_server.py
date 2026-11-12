"""Tiny loopback stand-ins for the OpenAI and Anthropic APIs, used only by the chat_eval tests (stdlib, no network).

Modes: ok | http500 | bad_request | unauthorized_echo (401 whose message contains the received key, like a real
provider that echoes a key fragment). judge_valid=True makes the judge schema request return a valid judgment.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RUBRIC_V2 = ("specific", "facts", "language", "structure", "usefulness", "no_filler", "depth", "balance")
SUGGESTIONS = [{"type": "deepen", "text": "Расскажи об этом подробнее"},
               {"type": "personalize", "text": "Как это проявляется в моей жизни?"},
               {"type": "explore", "text": "А что говорят другие планеты?"}]
PARAMS_FOR_TEST = {"model": "gpt-4o-mini", "temperature": 0.1}   # a parameter source for the --params-func test
ANSWER = "**Коротко.** Луна в Водолее в 12 доме.\n\n- первое\n- второе"


def judgment(winner="2"):
    scores = {c: 4 for c in RUBRIC_V2}
    one = {"scores": scores, "fact_errors": [], "boundary_kept": "n/a"}
    return {"answer_1": one, "answer_2": one, "winner": winner, "reason": "Второй конкретнее."}


class FakeOpenAI(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, expected_key="", mode="ok", usage=(100, 40), judge_valid=False, judge_winner="2"):
        super().__init__(("127.0.0.1", 0), Handler)
        self.expected_key, self.mode, self.usage = expected_key, mode, usage
        self.judge_valid, self.judge_winner = judge_valid, judge_winner
        self.requests, self.auth_seen, self.lock = [], [], threading.Lock()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d/v1" % self.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()

    def count(self, kind=None):
        with self.lock:
            return len([r for r in self.requests if kind is None or r == kind])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        server = self.server
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        schema = ((payload.get("response_format") or {}).get("json_schema") or {}).get("name")
        kind = "judge" if schema == "blind_judgment" else ("generation" if payload.get("response_format") else "summary")
        with server.lock:
            server.requests.append(kind)
            server.auth_seen.append(self.headers.get("Authorization") == "Bearer " + server.expected_key)
        if server.mode == "unauthorized_echo":
            return self.send_json(401, {"error": {"message": "Incorrect API key provided: " + server.expected_key,
                                                  "type": "invalid_request_error", "code": "invalid_api_key"}})
        if server.mode == "http500":
            return self.send_json(500, {"error": {"message": "boom", "type": "server_error"}})
        if server.mode == "bad_request":
            return self.send_json(400, {"error": {"message": "Unsupported parameter", "type": "invalid_request_error",
                                                  "code": "unsupported_parameter", "param": "max_tokens"}})
        if kind == "judge":
            content = json.dumps(judgment(server.judge_winner) if server.judge_valid else
                                 {"answer": "x", "follow_up_suggestions": SUGGESTIONS})
        elif kind == "generation":
            content = json.dumps({"answer": ANSWER, "follow_up_suggestions": SUGGESTIONS}, ensure_ascii=False)
        else:
            content = "Краткая память беседы."
        usage = {"prompt_tokens": server.usage[0], "completion_tokens": server.usage[1],
                 "total_tokens": server.usage[0] + server.usage[1]}
        base = {"id": "chatcmpl-test", "created": int(time.time()), "model": payload.get("model", "m")}
        if not payload.get("stream"):
            return self.send_json(200, {**base, "object": "chat.completion", "usage": usage, "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(choices, **extra):
            data = json.dumps({**base, "object": "chat.completion.chunk", "choices": choices, **extra}, ensure_ascii=False)
            self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
            self.wfile.flush()

        chunk([{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}])
        for start in range(0, len(content), 16):
            chunk([{"index": 0, "delta": {"content": content[start:start + 16]}, "finish_reason": None}])
        chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}])
        chunk([], usage=usage)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


# --------------------------------------------------------------------------------------------- Anthropic
ANTHROPIC_MODEL = "claude-haiku-5-5"


class FakeAnthropic(ThreadingHTTPServer):
    """POST /v1/messages in the official wire format (plain and server-sent events), on 127.0.0.1 only.

    usage = (input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens).
    Modes: ok | max_tokens | refusal | refusal_mid | overloaded (529) | rate_limit (429) | bad_request (400) | http500 |
    wrong_model | unauthorized_echo (401 that repeats the received key) | stream_error (an error event after some text).
    delay: seconds before the first text. Judge and attribution requests are told apart by their prompt; judge_valid=True
    answers them with a schema-valid reply, otherwise with a foreign envelope (the stand does the same).
    Bodies are kept in `bodies` for the tests only.
    """
    daemon_threads = True

    def __init__(self, expected_key="", mode="ok", usage=(120, 800, 0, 300), judge_valid=False, judge_winner="2",
                 text=None, delay=0.0, attribution="1"):
        super().__init__(("127.0.0.1", 0), AnthropicHandler)
        self.expected_key, self.mode, self.usage = expected_key, mode, usage
        self.judge_valid, self.judge_winner, self.text, self.delay, self.attribution = judge_valid, judge_winner, text, delay, attribution
        self.requests, self.bodies, self.headers_seen, self.lock = [], [], [], threading.Lock()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()

    def count(self, kind=None):
        with self.lock:
            return len([r for r in self.requests if kind is None or r == kind])


class AnthropicHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def error(self, status, kind, message):
        self.send_json(status, {"type": "error", "error": {"type": kind, "message": message}})

    def sse(self, event, data):
        self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
        self.wfile.flush()

    def do_POST(self):
        server = self.server
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        system = payload.get("system")
        system_text = system if isinstance(system, str) else " ".join(b.get("text", "") for b in (system or []))
        if "blind evaluator" in system_text:
            kind = "judge"
        elif "ATTRIBUTION TASK" in system_text:
            kind = "attribution"
        else:
            kind = "generation" if (payload.get("output_config") or {}).get("format") else "summary"
        with server.lock:
            server.requests.append(kind)
            server.bodies.append(payload)
            server.headers_seen.append({"x-api-key": self.headers.get("x-api-key"),
                                        "authorization": self.headers.get("Authorization"),
                                        "anthropic-version": self.headers.get("anthropic-version")})
        mode = server.mode
        if mode == "unauthorized_echo":
            return self.error(401, "authentication_error", "invalid x-api-key: " + (self.headers.get("x-api-key") or ""))
        if mode == "overloaded":
            return self.error(529, "overloaded_error", "Overloaded")
        if mode == "rate_limit":
            return self.error(429, "rate_limit_error", "slow down")
        if mode == "bad_request":
            return self.error(400, "invalid_request_error", "temperature: not allowed for this model")
        if mode == "http500":
            return self.error(500, "api_error", "boom")
        if kind == "judge":
            content = json.dumps(judgment(server.judge_winner) if server.judge_valid else
                                 {"answer": "x", "follow_up_suggestions": SUGGESTIONS})
        elif kind == "attribution":
            content = json.dumps({"answer_for_chart_x": server.attribution} if server.judge_valid else {"answer": "x"})
        elif kind == "generation":
            content = server.text or json.dumps({"answer": ANSWER, "follow_up_suggestions": SUGGESTIONS}, ensure_ascii=False)
        else:
            content = "Краткая память беседы."
        model = "claude-sonnet-5-5" if mode == "wrong_model" else payload.get("model", ANTHROPIC_MODEL)
        stop, details = "end_turn", None
        if mode == "max_tokens":
            stop = "max_tokens"
        if mode in ("refusal", "refusal_mid"):
            stop, details = "refusal", {"type": "refusal", "category": "general_harms", "explanation": "CANARY-explanation"}
        fresh, written, read, out = server.usage
        usage = {"input_tokens": fresh, "cache_creation_input_tokens": written, "cache_read_input_tokens": read,
                 "output_tokens": out}
        message = {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "stop_sequence": None,
                   "stop_reason": stop, "stop_details": details, "usage": usage}
        pieces = [] if mode == "refusal" else [content[i:i + 9] for i in range(0, len(content), 9)]
        if mode == "refusal_mid":
            pieces = pieces[:2]
        if not payload.get("stream"):
            blocks = [{"type": "thinking", "thinking": "", "signature": "sig"}]
            if pieces:
                blocks.append({"type": "text", "text": "".join(pieces)})
            return self.send_json(200, {**message, "content": blocks})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.sse("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None,
                                                                         "stop_details": None, "usage": {**usage, "output_tokens": 1}}})
        self.sse("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
        self.sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": ""}})
        self.sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}})
        self.sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        self.sse("ping", {"type": "ping"})
        if pieces:
            self.sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
            if server.delay:
                time.sleep(server.delay)
            for number, piece in enumerate(pieces):
                self.sse("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": piece}})
                if mode == "stream_error" and number == 1:
                    self.sse("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
                    self.close_connection = True
                    return
            self.sse("content_block_stop", {"type": "content_block_stop", "index": 1})
        self.sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None, "stop_details": details},
                                   "usage": usage})
        self.sse("message_stop", {"type": "message_stop"})
        self.close_connection = True
