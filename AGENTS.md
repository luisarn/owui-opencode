# AGENTS.md

This file provides guidance to Qoder (qoder.com) when working with code in this repository.

## Project Overview

This is an **OpenWebUI Pipe function** — a single-file Python module (`opencode_pipe.py`) that integrates the OpenCode agent into OpenWebUI chats. The code is not a standalone application; it is pasted into OpenWebUI's Workspace → Functions editor. Each chat gets its own Docker container running `opencode serve`, providing isolated sandboxed agent sessions.

## Build & Run Commands

### Build the Docker sandbox image
```bash
docker build -t opencode-pipe:latest .
```

### Run a sandbox container manually (for testing)
```bash
docker run -it --rm -p 4096:4096 -v $(pwd)/workspace:/workspace opencode-pipe:latest
```

### Cleanup all spawned containers
```bash
docker ps -f name=opencode-pipe-* -q | xargs docker rm -f
```

There is no test suite, no package manager, and no linting tooling. The only "build" step is the Docker image build. The Python code has no local dependency installation — its requirements (`httpx`, `docker`) are resolved by OpenWebUI's runtime environment when the Pipe is loaded.

## Architecture

### Single-file structure
All logic lives in `opencode_pipe.py`. There are no modules, packages, or separate source directories. The entire Pipe is one Python file that OpenWebUI imports as a Function.

### `_DockerHelper`
Abstracts Docker operations. Prefers `docker-py` (when available via mounted socket) and falls back to `subprocess` CLI calls. Handles container lifecycle: `is_running`, `remove_container`, `run_container`.

### `Pipe` class
The OpenWebUI Pipe entry point. Key methods:
- **`pipe()`** — main async generator called by OpenWebUI for each user message. Orchestrates the full lifecycle: container creation → session creation → prompt dispatch → streaming/fallback response → artifact detection.
- **`_ensure_container()`** — creates or reuses a Docker container per `chat_id`. When `PROVIDER=custom`, writes `.opencode.json` into the workdir and passes `OPENCODE_CONFIG` env var to the container. Containers persist across turns.
- **`_build_custom_provider_config()`** — generates the `opencode.json` content for a custom OpenAI-compatible provider. The API key is never written to disk; it uses `{env:CUSTOM_OPENCODE_API_KEY}` substitution.
- **`_model_body()`** — returns the correct `{providerID, modelID}` dict for prompt bodies, routing `PROVIDER=custom` to the `"custom"` provider ID registered in `opencode.json`.
- **`_get_or_create_session()`** — creates or resumes an OpenCode session inside the container via HTTP.
- **`_maybe_inject_system()`** — sends system prompt as a `noReply` prompt on first turn only.
- **`_run_streaming()`** — SSE-based real-time streaming with fallback to `_send_prompt()` (single-block) if no text events arrive.

### `Valves` (Pydantic model)
OpenWebUI's configuration mechanism. Key valves: `PROVIDER`, `MODEL`, `DOCKER_IMAGE`, `DOCKER_HOST`, `DOCKER_NETWORK`, `STREAMING`, `MAX_TURNS`, `CONTAINER_TIMEOUT`. API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) are passed as container environment variables.

### Per-chat container model
`_chat_containers` (global dict) maps `chat_id` → `{name, port, session_id, system_injected}`. Containers are named `opencode-pipe-{chat_id}` and are **not** auto-removed between turns.

### Docker networking
Two modes controlled by `DOCKER_NETWORK` valve:
- **Port mapping** (empty `DOCKER_NETWORK`): random host port mapped to container's 4096, reached via `DOCKER_HOST`.
- **Named networking** (`DOCKER_NETWORK` set): containers join the specified Docker network, reached by container name directly — no port mapping needed. Preferred for Docker-in-Docker setups.

### Artifact handling
After each turn, the workspace directory is scanned for new/modified files (images → inline, documents → download links). Files are uploaded to OpenWebUI's internal storage via `open_webui.models.files` and `open_webui.storage.provider`. This import is optional — the Pipe works without it, just skips artifact surfacing.

### SSE streaming
The Pipe subscribes to OpenCode's `/event/subscribe` SSE endpoint, parsing events defensively across multiple shapes (`content_block_delta`, `text`, `text_delta`, `thinking_start/delta/stop`, `tool_use`, `message_stop`). If the SSE stream produces no text, it falls back to the synchronous HTTP response.

## Development Notes

- The Dockerfile uses `node:20-slim` and installs `opencode-ai` globally via npm. Build tools (git, python3, g++, make) are included because coding agents often need them.
- The Pipe code relies on OpenWebUI internals (`open_webui.models.files`, `open_webui.storage.provider`) for artifact upload. These imports are wrapped in try/except so the Pipe still works in environments where they're unavailable.
- `httpx` is required for all HTTP communication with the OpenCode container. `docker-py` is optional — the CLI fallback works without it.
- The `__event_emitter__` callback (provided by OpenWebUI) is used for status updates shown in the chat UI. It's optional and silently ignored if unavailable.

## OpenCode HTTP API (v1.15+)

The pipe targets OpenCode v1.15+. Key API changes from older versions that are relevant if debugging:

| What | Old (pre-v1.15) | New (v1.15+) |
|---|---|---|
| Prompt endpoint | `POST /session/:id/prompt` | `POST /session/:id/message` |
| SSE endpoint | `/event/subscribe` | `/event` |
| SSE event envelope | `event: <type>\ndata: <json>` | `data: {"type": "...", "properties": {...}}` |
| Text streaming | `content_block_delta` / `text_delta` events | `message.part.delta` with `field="text"` |
| Thinking/reasoning | `thinking_start/delta/stop` events | `message.part.updated` (type=reasoning) + `message.part.delta` on the reasoning partID |
| Tool calls | `tool_use` event | `message.part.updated` (type=tool) with `state.input` |
| Completion signal | `message_stop` event | `session.idle` event |
| Health check | `GET /health` returns JSON 200 | `GET /health` returns web UI HTML; use `GET /session` (returns `[]`) |

The SSE `message.part.delta` events use the same `field="text"` for both reasoning and visible text. The streaming runner distinguishes them by tracking which `partID` values belong to `reasoning` parts (registered when `message.part.updated` with `type=reasoning` and empty `text` first arrives).