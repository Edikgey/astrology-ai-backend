"""A tiny loopback stand-in for the OpenAI chat API, used only by the chat_eval tests (stdlib, no network).

Modes: ok | http500 | bad_request | unauthorized_echo (401 whose message contains the received key, like a real
provider that echoes a key fragment). judge_valid=True makes the judge schema request return a valid judgment.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SUGGESTIONS = [{"type": "deepen", "text": "Расскажи об этом подробнее"},
               {"type": "personalize", "text": "Как это проявляется в моей жизни?"},
               {"type": "explore", "text": "А что говорят другие планеты?"}]
ANSWER = "**Коротко.** Луна в Водолее в 12 доме.\n\n- первое\n- второе"


def judgment(winner="2"):
    scores = {c: 4 for c in ("specific", "facts", "language", "structure", "usefulness", "no_filler")}
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
