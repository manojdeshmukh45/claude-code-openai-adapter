# claude-code-openai-adapter

A small, **dependency-free** Python server that exposes an **OpenAI-compatible** `/v1/chat/completions`
endpoint and answers it by running your locally installed, already logged-in **Claude Code** CLI
(`claude -p`).

It lets tools that only speak "OpenAI-compatible API + API key" (agent frameworks, pentest/triage tools,
scripts) talk to Claude through your Claude Code login instead of a separately billed API key.

```
your tool ──HTTP (OpenAI format)──▶ adapter.py ──spawns──▶ claude -p ──▶ Claude
```

> **Read [Limitations](#limitations) and [Terms and responsibility](#terms-and-responsibility) first.**
> Tool calling is emulated, replies are not truly streamed, and heavy automated use can burn through
> your Claude plan's usage limits quickly.

## Contents

- [Quick start](#quick-start)
- [Requirements](#requirements)
- [Configuration](#configuration)
- [Using it from a client](#using-it-from-a-client)
- [Using it with ARTEX](#using-it-with-artex)
- [Running as a service](#running-as-a-service)
- [How it works](#how-it-works)
- [API support](#api-support)
- [Limitations](#limitations)
- [Security](#security)
- [Terms and responsibility](#terms-and-responsibility)
- [Troubleshooting](#troubleshooting)
- [Development and tests](#development-and-tests)
- [License](#license)

## Quick start

```bash
# 1. Make sure Claude Code works and is logged in (run `claude` once, or `/login` inside it)
claude -p "say hi"

# 2. Get the adapter
git clone <this repo> && cd claude-code-openai-adapter

# 3. Run it (a random token is generated and printed if you don't set one)
ADAPTER_TOKEN=$(openssl rand -hex 16) python3 adapter.py
#   claude-code-openai-adapter 0.1.0 listening on http://127.0.0.1:8790/v1

# 4. Test it
curl -s http://127.0.0.1:8790/v1/chat/completions \
  -H "Authorization: Bearer $ADAPTER_TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"haiku","messages":[{"role":"user","content":"Reply with: pong"}]}'
```

Point your client at **base URL** `http://127.0.0.1:8790/v1`, **API key** = your token, **model**
`sonnet` / `opus` / `haiku`.

## Requirements

- Python 3.8+ (standard library only; nothing to `pip install`)
- Claude Code installed and logged in **as the same OS user that runs the adapter**
  (`claude --version` must work; `claude -p "hi"` must answer)

## Configuration

Every option is a flag or an environment variable. Flags win.

| Flag | Env var | Default | Meaning |
|---|---|---|---|
| `--host` | `ADAPTER_HOST` | `127.0.0.1` | Bind address. Keep loopback unless you must expose it (see [Security](#security)). |
| `--port` | `ADAPTER_PORT` | `8790` | Port. |
| (none) | `ADAPTER_TOKEN` | random per run | Bearer token clients must send as the API key. **Set your own for anything long-running.** |
| `--model` | `CLAUDE_MODEL` | `sonnet` | Model used when the request names none. Aliases: `sonnet`, `opus`, `haiku`. |
| `--timeout` | `CLAUDE_TIMEOUT` | `300` | Seconds allowed per call. |
| `--max-parallel` | `MAX_PARALLEL` | `2` | Concurrent `claude` processes. Each call spawns one. Keep low. |
| `--claude-bin` | `CLAUDE_BIN` | `claude` | Path to the CLI if it is not on `PATH`. |
| `--log-calls` | `ADAPTER_LOG_CALLS` | off | Append raw prompts/replies to this JSONL file. **Debug only: contains your data.** |

## Using it from a client

Any OpenAI-compatible client works with Chat Completions:

```python
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:8790/v1", api_key="<your ADAPTER_TOKEN>")
r = c.chat.completions.create(model="sonnet", messages=[{"role": "user", "content": "hello"}])
print(r.choices[0].message.content)
```

Tool calling:

```bash
curl -s http://127.0.0.1:8790/v1/chat/completions \
  -H "Authorization: Bearer $ADAPTER_TOKEN" -H "Content-Type: application/json" -d '{
    "model": "haiku",
    "messages": [{"role":"user","content":"What is the weather in Paris? Use the tool."}],
    "tools": [{"type":"function","function":{"name":"get_weather","description":"Get weather",
      "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]
  }'
# -> finish_reason "tool_calls", tool_calls[0].function = get_weather {"city":"Paris"}
```

`GET /health` (no auth) returns `{"status":"ok"}` for liveness checks. `GET /v1/models` lists the aliases.

## Using it with ARTEX

[ARTEX](https://github.com/BaaRai/ARTEX) wants an Anthropic or OpenAI-compatible endpoint. In
**System → LLM → New profile**:

| Field | Value |
|---|---|
| Format | **openai** (Chat Completions). **Not** `openai-responses`: that API is not implemented and returns 404. |
| Base URL | `http://<host the ARTEX container can reach>:8790/v1` |
| API key | your `ADAPTER_TOKEN` |
| Model | `sonnet` (or `haiku` / `opus`) |
| Streaming | Off (recommended; streaming is simulated, see below) |
| Rate limit | A low cap (for example 10 per minute) |

Then click **Set as active**. Notes:

- **Docker:** ARTEX in Docker cannot reach `127.0.0.1` on the host. Bind the adapter to the Docker network
  gateway (for example `--host 172.19.0.1`; find it with
  `docker network inspect <project>_default --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'`) and use that
  address in the Base URL. Do **not** bind to `0.0.0.0` unless the port is firewalled.
- **Budgets:** set `ARTEX_TASK_BUDGET_TOKENS` / `ARTEX_INTENT_BUDGET_TOKENS` in ARTEX's `.env`. An autonomous
  run makes hundreds of calls.
- **Workers:** set concurrent workers to 1 or 2 in ARTEX (matches `--max-parallel`).

## Running as a service

`deploy/claude-code-openai-adapter.service` is a systemd unit. Copy the repo to `/opt/claude-code-openai-adapter`,
edit `User`, the token and `PATH` (it must include the directory of `claude`), then:

```bash
sudo cp deploy/claude-code-openai-adapter.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now claude-code-openai-adapter
journalctl -u claude-code-openai-adapter -f
```

Without systemd: `nohup python3 adapter.py > adapter.log 2>&1 &` (it will not survive a reboot).

## How it works

1. The request's `messages` are flattened into one prompt (`USER:` / `ASSISTANT:` / `TOOL RESULT:` turns);
   `system` messages become the system prompt.
2. `claude -p --output-format json --tools "" --no-session-persistence --disable-slash-commands
   --strict-mcp-config --setting-sources ""` is run with that prompt, so Claude Code's own tools, settings,
   skills and MCP servers are **not** used. It is a plain model call.
3. **Tool calling is emulated.** If the request has `tools`, their schemas are added to the system prompt with
   an instruction to reply with exactly one JSON object: `{"tool_calls":[...]}` or `{"content":"..."}`.
   That object is parsed (tolerating code fences, extra text, and **missing closing brackets**, which models
   do produce) and returned as an OpenAI `tool_calls` message with `finish_reason: "tool_calls"`.
4. Token usage from the CLI is mapped to `usage` (cache tokens are counted as prompt tokens).

## API support

| Feature | Status |
|---|---|
| `POST /v1/chat/completions` | Supported |
| Tool / function calling | Emulated via prompting and JSON parsing |
| `stream: true` | **Simulated**: the full answer arrives as one SSE chunk, then `[DONE]` |
| `GET /v1/models`, `GET /health` | Supported |
| `/v1/responses` (OpenAI Responses API) | Not implemented (404) |
| Embeddings, images, audio, `n > 1`, logprobs | Not implemented |
| `temperature`, `max_tokens`, `response_format`, `tool_choice`, … | Ignored |

## Limitations

- **Reliability of tool calls.** Emulation depends on the model following the JSON instruction. It usually
  does, and the parser repairs common damage, but a malformed reply can still turn a tool call into plain text.
  Use `--log-calls` to see raw replies when debugging.
- **Latency.** Each call starts a CLI process: several seconds per turn.
- **Usage limits.** Calls draw on your Claude plan. Agentic loops resend a growing context on every turn and
  can exhaust a plan window fast. Prefer `haiku` for tests; set budgets in the calling tool.
- **Context.** The whole conversation is re-sent every call.
- **No real streaming, no sampling controls.**

## Security

- **Authentication is mandatory.** Every endpoint except `/health` requires `Authorization: Bearer <token>`.
- **Loopback by default.** Binding elsewhere prints a warning. Anyone who can reach the port and holds the
  token can spend your Claude usage. Use a firewall or a private network, and a long random token.
- The model runs with **no tools** (`--tools ""`), so a prompt cannot make it touch the host. The *calling*
  tool may still act on tool calls it receives, so secure that side too.
- `--log-calls` writes prompts and replies to disk in clear text. Leave it off in normal use and delete the file.
- Do not commit your token. `.gitignore` already excludes `token`, `.env` and `*.log`.

## Terms and responsibility

This project runs the **official `claude` binary** on your own machine, using your own login. Even so,
Anthropic's terms and usage policies govern how a Claude subscription may be used, and **automated or
third-party use of a consumer subscription can conflict with them** or be rate-limited. This project is
unofficial, is not affiliated with or endorsed by Anthropic, and gives no assurance that this use is allowed
for your plan. **You are responsible for checking the terms that apply to you.** For production or heavy
automated workloads, use an Anthropic API key.

Only use it with systems and data you are authorized to work with.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `401 unauthorized` | Wrong or missing token. The API key must equal `ADAPTER_TOKEN` (or the token printed at startup). |
| `502 ... claude exited ...` | `claude` not on `PATH` (use `--claude-bin`), not logged in (run `claude`, then `/login`), or the model alias is wrong. |
| `404` from the client | The client is calling `/v1/responses`. Switch it to Chat Completions. |
| Client says "no action" / replies as text instead of calling tools | The model's JSON was unusable. Enable `--log-calls`, look at `raw_output`, and lower the number of tools if the list is huge. |
| `BrokenPipeError` in the log | The client timed out and closed the connection before the reply. Raise the client timeout or `--timeout`. |
| Works in a terminal, fails as a service | The service user is not the user that logged in to Claude Code, or `PATH` in the unit lacks `claude`. |
| Container cannot connect | The adapter is bound to `127.0.0.1`. Bind it to the Docker gateway address and use that in the client. |

## Development and tests

```bash
python3 -m unittest discover -s tests -v   # no dependencies, no network, does not call claude
```

The tests cover prompt building, JSON recovery (including missing brackets and brace characters inside
strings), and conversion to OpenAI tool-call messages. Contributions welcome: please keep it dependency-free.

## License

MIT, see [LICENSE](LICENSE).
