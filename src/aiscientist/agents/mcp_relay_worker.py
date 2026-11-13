"""Job-local chat facade for the gateway's guarded file relay (stdlib only).

This file is staged verbatim into a pinned MCP job. It never has a provider key
or chooses an upstream endpoint. Its random token protects only the loopback
listener, and its private queue contains requests for the session-bound gateway.
The supported text/function-tool subset is explicit; unknown API options fail.
"""
from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 2 * 1024 * 1024
_FIELDS = {"model", "messages", "max_tokens", "max_completion_tokens", "tools", "tool_choice",
           "response_format", "temperature", "reasoning_effort", "chat_template_kwargs",
           "stream", "stream_options", "n", "parallel_tool_calls"}


def validate_request(value: dict, model: str) -> dict:
    """Validate on BOTH sides of the queue; never trust requests from compute."""
    if not isinstance(value, dict) or set(value) - _FIELDS:
        raise ValueError("unsupported chat request field")
    if value.get("model") != model:
        raise ValueError("request must use the bound session model")
    messages = value.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 256:
        raise ValueError("messages must be a nonempty bounded list")
    for message in messages:
        if (not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}
                or set(message) - {"role", "content", "tool_calls", "tool_call_id", "name"}):
            raise ValueError("unsupported message shape")
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(part, dict) or set(part) != {"type", "text"}
                   or part["type"] != "text" or not isinstance(part["text"], str) for part in content):
                raise ValueError("this relay supports text content only")
        elif content is not None and not isinstance(content, str):
            raise ValueError("invalid message content")
        if message.get("tool_calls") is not None:
            _tool_calls(message["tool_calls"])
    cap = value.get("max_tokens", value.get("max_completion_tokens", 4096))
    if "max_tokens" in value and "max_completion_tokens" in value:
        raise ValueError("supply one token limit")
    if type(cap) is not int or not 1 <= cap <= 32768:
        raise ValueError("max_tokens must be between 1 and 32768")
    out = {"model": model, "messages": messages, "max_tokens": cap}
    tools = value.get("tools")
    names = []
    if tools is not None:
        if not isinstance(tools, list) or len(tools) > 64:
            raise ValueError("invalid tools")
        for tool in tools:
            fn = tool.get("function") if isinstance(tool, dict) else None
            if (not isinstance(fn, dict) or tool.get("type") != "function"
                    or not isinstance(fn.get("name"), str) or not re.fullmatch(r"[\w-]{1,128}", fn["name"])
                    or not isinstance(fn.get("parameters", {}), dict)):
                raise ValueError("invalid function tool")
            names.append(fn["name"])
        out["tools"] = tools
    choice = value.get("tool_choice")
    if choice is not None:
        valid = choice in ("auto", "none", "required") if isinstance(choice, str) else (
            isinstance(choice, dict) and set(choice) == {"type", "function"}
            and choice["type"] == "function" and isinstance(choice["function"], dict)
            and set(choice["function"]) == {"name"} and choice["function"]["name"] in names)
        if not valid or (not names and choice not in ("none", "auto")):
            raise ValueError("invalid tool_choice")
        out["tool_choice"] = choice
    for key in ("stream", "parallel_tool_calls"):
        if key in value and type(value[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    if "parallel_tool_calls" in value:
        out["parallel_tool_calls"] = value["parallel_tool_calls"]
    if value.get("stream_options") not in (None, {}, {"include_usage": True}, {"include_usage": False}):
        raise ValueError("unsupported stream_options")
    if value.get("n", 1) != 1 or type(value.get("n", 1)) is not int:
        raise ValueError("only one completion is supported")
    if "response_format" in value:
        if value["response_format"] not in ({"type": "json_object"}, {"type": "text"}):
            raise ValueError("only text or json_object response_format is supported")
        out["response_format"] = value["response_format"]
    if "temperature" in value:
        temperature = value["temperature"]
        if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
            raise ValueError("invalid temperature")
        out["temperature"] = temperature
    effort = value.get("reasoning_effort")
    if effort is not None:
        if effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("unsupported reasoning_effort")
        out["reasoning_effort"] = effort
    if "chat_template_kwargs" in value:
        if value["chat_template_kwargs"] != {"enable_thinking": False}:
            raise ValueError("unsupported chat_template_kwargs")
        out["reasoning_effort"] = "none"
    if len(json.dumps(out).encode()) > MAX_BODY:
        raise ValueError("chat request is too large")
    return out


def _tool_calls(calls):
    if not isinstance(calls, list) or len(calls) > 64:
        raise ValueError("invalid tool calls")
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if (not isinstance(fn, dict) or call.get("type") != "function"
                or not isinstance(call.get("id"), str) or not isinstance(fn.get("name"), str)
                or not isinstance(fn.get("arguments"), str)):
            raise ValueError("invalid tool call")


def private_json(path: Path, value):
    temp = path.with_name("." + uuid.uuid4().hex + ".tmp")
    with temp.open("x", encoding="utf-8") as file:
        os.chmod(temp, 0o600)
        json.dump(value, file)
    temp.replace(path)


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, directory: Path, model: str, *, timeout=600):
        if not directory.is_absolute() or any(p.is_symlink() for p in (directory, *directory.parents)):
            raise ValueError("relay directory must be an absolute non-symlink path")
        directory.mkdir(mode=0o700, exist_ok=False)
        self.directory, self.model, self.timeout = directory, model, timeout
        for name in ("req", "resp"):
            (directory / name).mkdir(mode=0o700)
        self.token = secrets.token_urlsafe(32)
        self.stopped = threading.Event()
        with (directory / "token").open("x") as file:
            os.chmod(directory / "token", 0o600)
            file.write(self.token)
        super().__init__(("127.0.0.1", 0), RelayHandler)

    def exchange(self, body):
        name = uuid.uuid4().hex + ".json"
        req, resp = self.directory / "req" / name, self.directory / "resp" / name
        private_json(req, body)
        deadline = time.monotonic() + self.timeout
        try:
            while not self.stopped.wait(0.1):
                if resp.is_symlink():
                    raise ValueError("invalid gateway response file")
                if resp.is_file():
                    if resp.stat().st_size > MAX_BODY:
                        raise ValueError("gateway response is too large")
                    return json.loads(resp.read_text())
                if time.monotonic() >= deadline:
                    raise TimeoutError("gateway model relay timed out")
            raise TimeoutError("gateway model relay stopped")
        finally:
            req.unlink(missing_ok=True)
            resp.unlink(missing_ok=True)

    def server_close(self):
        self.stopped.set()
        super().server_close()
        for name in ("token", "ready.env"):
            (self.directory / name).unlink(missing_ok=True)


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # no prompts, tokens, or HTTP headers in the job log

    def _json(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "unsupported route"}})
        if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.token):
            return self._json(401, {"error": {"message": "relay authentication required"}})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise ValueError("invalid request size")
            raw = json.loads(self.rfile.read(size))
            body = validate_request(raw, self.server.model)
        except (ValueError, TypeError) as exc:
            return self._json(400, {"error": {"message": str(exc)}})
        try:
            reply = self.server.exchange(body)
            accounting = ({"version": 1, "cost_usd": reply.get("cost_usd"), "billed": True}
                          if "usage" in reply else None)
            if reply.get("error"):
                return self._json(403 if reply.get("kind") == "stop" else 502,
                                  {"error": {"message": reply["error"], "type": reply.get("kind")},
                                   **({"aiscientist_usage": accounting, "usage": reply["usage"]} if accounting else {})})
            turn = reply["turn"]
            content, calls = turn.get("content"), turn.get("tool_calls") or []
            if content is not None and not isinstance(content, str):
                raise ValueError("invalid gateway text")
            _tool_calls(calls)
            message = {"role": "assistant", "content": content}
            if calls:
                message["tool_calls"] = calls
            result = {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
                      "created": int(time.time()), "model": self.server.model,
                      "choices": [{"index": 0, "message": message,
                                   "finish_reason": turn.get("finish_reason") or ("tool_calls" if calls else "stop")}],
                      "usage": reply.get("usage", {})}
            if accounting:
                result["aiscientist_usage"] = accounting
            if raw.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                chunk = {k: v for k, v in result.items() if k not in {"choices", "usage", "aiscientist_usage"}}
                chunk["object"] = "chat.completion.chunk"
                delta = dict(message)
                if calls:
                    delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(calls)]
                for choices in ([{"index": 0, "delta": delta, "finish_reason": None}],
                                [{"index": 0, "delta": {}, "finish_reason": result["choices"][0]["finish_reason"]}]):
                    self.wfile.write(("data: " + json.dumps({**chunk, "choices": choices}) + "\n\n").encode())
                if (raw.get("stream_options") or {}).get("include_usage"):
                    self.wfile.write(("data: " + json.dumps({**chunk, "choices": [], "usage": result["usage"]}) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self._json(200, result)
        except (ValueError, KeyError, TypeError, TimeoutError):
            self._json(502, {"error": {"message": "gateway model relay failed; inspect the gateway run log"}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prefix", required=True, choices=("LITERATURE", "PHENOTYPE", "DATA_UTILITIES"))
    parser.add_argument("--priced", action="store_true")
    parser.add_argument("--identity", default="")
    args = parser.parse_args()
    os.umask(0o077)
    server = RelayServer(args.directory, args.model)
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    values = {args.prefix + "_MODEL_ENDPOINT": endpoint, args.prefix + "_MODEL": args.model,
              args.prefix + "_MODEL_API_KEY_FILE": str(args.directory / "token"),
              args.prefix + "_MODEL_RELAY_PROTOCOL": "aisci-relay-v1",
              args.prefix + "_MODEL_IDENTITY": args.identity or hashlib.sha256(args.model.encode()).hexdigest()}
    if args.priced:
        values[args.prefix + "_MODEL_USAGE_PROTOCOL"] = "aisci-relay-v1"
    ready = args.directory / "ready.env"
    temporary = ready.with_suffix(".tmp")
    temporary.write_text("".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()))
    temporary.replace(ready)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_args: stopped.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stopped.wait()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
