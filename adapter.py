#!/usr/bin/env python3
"""claude-code-openai-adapter

A tiny, dependency-free OpenAI-compatible HTTP server that answers
`/v1/chat/completions` by running the locally installed, already logged-in
`claude` CLI in headless mode (`claude -p`).

Tool calling is *emulated*: the caller's tool definitions are placed in the
prompt and the model is told to answer with one JSON object, which is then
converted to OpenAI `tool_calls`. Replies are never streamed token by token; if a
client asks for `stream: true` the full answer is sent as a single SSE chunk.

See README.md for setup, configuration and limitations.
"""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "0.1.0"

# --------------------------------------------------------------------------- config
CFG = {
    "token": "",
    "model": "sonnet",
    "timeout": 300,
    "claude_bin": "claude",
    "log_calls": "",       # path of a JSONL file; empty = do not log prompts/replies
}
SEM = threading.Semaphore(2)
MODELS = ("sonnet", "opus", "haiku")

TOOL_PROMPT = """You are the model behind an OpenAI-style chat API. You have NO tools of your own.
The caller's tools are listed below. Reply with EXACTLY ONE JSON object and nothing else:
  to call tools: {"tool_calls":[{"name":"<tool name>","arguments":{...}}]}
  to answer:     {"content":"<your answer text>"}
Never wrap the JSON in markdown fences. Only call tools from the list. Make sure every
bracket and brace is closed.

TOOLS:
"""


# --------------------------------------------------------------------------- prompt building
def text_of(content):
    """OpenAI message content may be a string or a list of {type,text} parts."""
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def build_prompt(messages, tools):
    """Return (system_prompt, conversation_prompt) for `claude -p`."""
    system, convo = [], []
    for m in messages:
        role, content = m.get("role"), text_of(m.get("content"))
        if role in ("system", "developer"):
            system.append(content)
        elif role == "assistant":
            text = content
            for call in m.get("tool_calls") or []:
                fn = call.get("function", {})
                text += "\n[assistant called tool %s id=%s args=%s]" % (
                    fn.get("name"), call.get("id"), fn.get("arguments"))
            convo.append("ASSISTANT: " + text.strip())
        elif role == "tool":
            convo.append("TOOL RESULT (id=%s):\n%s" % (m.get("tool_call_id"), content))
        else:
            convo.append("USER: " + content)
    sysmsg = "\n\n".join(system)
    if tools:
        defs = [t.get("function", t) for t in tools]
        listing = [{"name": d["name"], "description": d.get("description", ""),
                    "parameters": d.get("parameters", {})} for d in defs]
        sysmsg += "\n\n" + TOOL_PROMPT + json.dumps(listing)
    return sysmsg, "\n\n".join(convo) + "\n\nASSISTANT:"


# --------------------------------------------------------------------------- JSON recovery
def repair_json(s):
    """Append any closing brackets/braces the model forgot (only outside strings).
    Returns None if the text ends inside a string (too risky to guess)."""
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        return None
    return s + "".join(reversed(stack))


def extract_json(text):
    """Best-effort: parse the model reply as a JSON object. Tolerates code fences,
    leading/trailing chatter and missing closing brackets. Returns dict or None."""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            pass
    if i >= 0:
        fixed = repair_json(s[i:])
        if fixed:
            try:
                return json.loads(fixed)
            except Exception:
                return None
    return None


def to_message(out, has_tools):
    """Convert the raw model text into an OpenAI message + finish_reason."""
    msg, finish = {"role": "assistant", "content": out}, "stop"
    if has_tools:
        j = extract_json(out)
        if isinstance(j, dict) and j.get("tool_calls"):
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_" + uuid.uuid4().hex[:16], "type": "function",
                 "function": {"name": c.get("name"),
                              "arguments": json.dumps(c.get("arguments", {}))}}
                for c in j["tool_calls"]]}
            finish = "tool_calls"
        elif isinstance(j, dict) and "content" in j:
            c = j["content"]
            msg["content"] = c if isinstance(c, str) else json.dumps(c)
    return msg, finish


# --------------------------------------------------------------------------- claude CLI
def run_claude(sysmsg, prompt, model):
    cmd = [CFG["claude_bin"], "-p", "--output-format", "json", "--tools", "",
           "--no-session-persistence", "--disable-slash-commands",
           "--strict-mcp-config", "--setting-sources", "", "--model", model]
    if sysmsg:
        cmd += ["--system-prompt", sysmsg]
    with SEM:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=CFG["timeout"], cwd="/tmp")
    if r.returncode != 0:
        raise RuntimeError("claude exited %d: %s" % (r.returncode, (r.stderr or r.stdout)[:500]))
    d = json.loads(r.stdout)
    if d.get("is_error"):
        raise RuntimeError(str(d.get("result"))[:500])
    return d.get("result", ""), d.get("usage", {})


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "claude-code-openai-adapter/" + __version__

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if self.headers.get("Authorization") != "Bearer " + CFG["token"]:
            self._json(401, {"error": {"message": "unauthorized", "type": "auth_error"}})
            return False
        return True

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/health":
            return self._json(200, {"status": "ok", "version": __version__})
        if not self._authed():
            return
        if path.endswith("/models"):
            return self._json(200, {"object": "list",
                                    "data": [{"id": m, "object": "model"} for m in MODELS]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self._authed():
            return
        if not self.path.split("?")[0].rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "not found (only /v1/chat/completions is implemented)"}})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            tools = req.get("tools") or []
            model = req.get("model") or CFG["model"]
            sysmsg, prompt = build_prompt(req.get("messages", []), tools)
            out, usage = run_claude(sysmsg, prompt, model)
        except Exception as e:  # noqa: BLE001
            return self._json(502, {"error": {"message": str(e), "type": "upstream_error"}})
        if CFG["log_calls"]:
            with open(CFG["log_calls"], "a") as f:
                f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "model": model,
                                    "tools": [t.get("function", t).get("name") for t in tools],
                                    "prompt_tail": prompt[-600:], "raw_output": out}) + "\n")
        msg, finish = to_message(out, bool(tools))
        pt = sum(usage.get(k, 0) for k in
                 ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        ct = usage.get("output_tokens", 0)
        cid, created = "chatcmpl-" + uuid.uuid4().hex[:12], int(time.time())
        if req.get("stream"):
            return self._sse(cid, created, model, msg, finish, pt, ct)
        self._json(200, {"id": cid, "object": "chat.completion", "created": created, "model": model,
                         "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                         "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}})

    def _sse(self, cid, created, model, msg, finish, pt, ct):
        """Fake streaming: the whole answer in one delta, then a final chunk and [DONE]."""
        delta = dict(msg)
        if "tool_calls" in delta:
            delta["tool_calls"] = [dict(tc, index=i) for i, tc in enumerate(delta["tool_calls"])]
        chunks = [
            {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
             "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
             "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for c in chunks:
            self.wfile.write(("data: " + json.dumps(c) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description="OpenAI-compatible endpoint backed by `claude -p`.")
    ap.add_argument("--host", default=env("ADAPTER_HOST", "127.0.0.1"), help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=int(env("ADAPTER_PORT", "8790")))
    ap.add_argument("--model", default=env("CLAUDE_MODEL", "sonnet"), help="default model alias")
    ap.add_argument("--timeout", type=int, default=int(env("CLAUDE_TIMEOUT", "300")), help="seconds per call")
    ap.add_argument("--max-parallel", type=int, default=int(env("MAX_PARALLEL", "2")))
    ap.add_argument("--claude-bin", default=env("CLAUDE_BIN", "claude"))
    ap.add_argument("--log-calls", default=env("ADAPTER_LOG_CALLS", ""),
                    help="append raw prompts/replies to this JSONL file (debug only; contains your data)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args()

    token = env("ADAPTER_TOKEN", "")
    generated = False
    if not token:
        token, generated = secrets.token_hex(16), True
    CFG.update(token=token, model=a.model, timeout=a.timeout, claude_bin=a.claude_bin, log_calls=a.log_calls)
    global SEM
    SEM = threading.Semaphore(max(1, a.max_parallel))

    print("claude-code-openai-adapter %s listening on http://%s:%d/v1" % (__version__, a.host, a.port), flush=True)
    if generated:
        print("No ADAPTER_TOKEN set; generated one for this run (use it as the API key):\n  " + token, flush=True)
    if a.host not in ("127.0.0.1", "localhost"):
        print("NOTE: bound to %s. Anyone who can reach this port and has the token can spend your Claude usage." % a.host, flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
