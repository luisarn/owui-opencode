# AGENTS.md

This file provides guidance to AI coding agents working with this repository.

## Project Overview

This is an **OpenWebUI Pipe function** — a single-file Python module (`opencode_pipe.py`) that integrates the [OpenCode](https://opencode.ai/) agent into [OpenWebUI](https://openwebui.com/) chats. The code is not a standalone application; it is pasted into OpenWebUI's **Workspace → Functions** editor. Each chat gets its own Docker container running `opencode serve`, providing isolated sandboxed agent sessions.

The project is inspired by [openwebui-claude-code](https://github.com/tfriedel/openwebui-claude-code) but uses the open-source OpenCode agent instead of Claude Code, keeping everything inside disposable Docker containers for security.

## Technology Stack

- **Python 3** — Single-file Pipe function (`opencode_pipe.py`)
- **OpenWebUI** — Host platform; the Pipe runs inside OpenWebUI's Python runtime
- **Docker** — Sandboxing layer; each chat spawns a sibling container
- **Node.js 20** — Inside the sandbox image, used to run `opencode-ai` globally
- **Pydantic** — Used for the `Valves` configuration model
- **httpx** — Required for all HTTP communication with OpenCode containers
- **docker-py** — Optional; the Pipe falls back to `docker` CLI subprocess calls if unavailable

There is no `pyproject.toml`, `package.json`, `setup.py`, or any package manifest. The only build artifact is the Docker image.

## Project Structure

```
.
├── opencode_pipe.py   # Entire Pipe logic — single Python file
├── Dockerfile         # Sandbox image definition (node:20-slim + opencode-ai)
├── README.md          # Human-facing documentation
├── AGENTS.md          # This file — agent guidance
└── .dockerignore      # Standard Docker ignore
```

There are no subdirectories, no modules, and no separate test files.

## Build and Test Commands

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

There is **no test suite**, **no linting tooling**, and **no package manager** involved. The only "build" step is the Docker image build. The Python code has no local dependency installation — its requirements (`httpx`, `docker`) are resolved by OpenWebUI's runtime environment when the Pipe is loaded.

## Architecture

### Single-file structure
All logic lives in `opencode_pipe.py`. There are no modules, packages, or separate source directories. The entire Pipe is one Python file that OpenWebUI imports as a Function.

### `_DockerHelper`
Abstracts Docker operations. Prefers `docker-py` (when available via mounted socket) and falls back to `subprocess` CLI calls. Handles container lifecycle:
- `is_running(name)` — check if a container is running
- `remove_container(name, force=False)` — remove a container
- `run_container(name, image, port, workdir, env, network)` — start a new container

### `Pipe` class
The OpenWebUI Pipe entry point. Key methods:
- **`pipe()`** — main async generator called by OpenWebUI for each user message. Orchestrates the full lifecycle: workspace setup → container creation → session creation → system prompt injection → prompt dispatch → streaming/fallback response → artifact detection → status updates.
- **`_ensure_container(chat_id, workdir)`** — creates or reuses a Docker container per `chat_id`. Writes `.opencode.json` into the workdir (when `PROVIDER` and `MODEL` are set) and passes `OPENCODE_CONFIG` env var to the container. Containers persist across turns.
- **`_build_provider_config(provider)`** — generates the `opencode.json` content that sets the default model. For custom providers, it also registers the provider with `{env:CUSTOM_OPENCODE_API_KEY}` substitution so the API key is never written to disk.
- **`_wait_for_health(name, port)`** — polls `GET /session` until the OpenCode server responds with HTTP 200.
- **`_get_or_create_session(name, port, chat_id)`** — creates or resumes an OpenCode session inside the container via HTTP. Stores the session ID in `_chat_containers`.
- **`_maybe_inject_system(name, port, session_id, system, chat_id)`** — sends the system prompt as a `noReply` message on the first turn only.
- **`_send_prompt(name, port, session_id, text)`** — synchronous (non-streaming) prompt dispatch via `POST /session/{id}/message`.
- **`_extract_response_text(data)`** — parses the JSON response into markdown, handling `text`, `reasoning`, and `tool` parts across both v1.15+ and legacy response shapes.
- **`_run_streaming(name, port, session_id, text, event_emitter)`** — SSE-based real-time streaming with fallback to `_send_prompt()` if no text events arrive.
- **`_sse_listener(base_url, queue)`** — background task that reads OpenCode's `/event` SSE stream and puts parsed events into an `asyncio.Queue`.

### `Valves` (Pydantic inner class)
OpenWebUI's configuration mechanism. Fields include:
- API keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `CUSTOM_API_KEY`
- Provider/model: `PROVIDER`, `MODEL`, `CUSTOM_BASE_URL`
- Docker: `DOCKER_IMAGE`, `DOCKER_HOST`, `DOCKER_NETWORK`, `WORKDIR_ROOT`, `CONTAINER_TIMEOUT`
- Behavior: `MAX_TURNS`, `STREAMING`

API keys are passed as container environment variables. For custom providers, the key is referenced via env-var substitution in `opencode.json` rather than written to disk.

### Per-chat container model
`_chat_containers` (global dict) maps `chat_id` → `{"name": str, "port": int, "session_id": str | None, "system_injected": bool}`. Containers are named `opencode-pipe-{chat_id}` and are **not** auto-removed between turns.

### Docker networking
Two modes controlled by `DOCKER_NETWORK` valve:
- **Port mapping** (empty `DOCKER_NETWORK`): random host port mapped to container port 4096, reached via `DOCKER_HOST`.
- **Named networking** (`DOCKER_NETWORK` set): containers join the specified Docker network, reached by container name directly — no port mapping needed. Preferred for Docker-in-Docker setups (e.g., Dockge).

### Artifact handling
After each turn, the workspace directory is scanned for new or modified files with extensions in `_ARTIFACT_EXTENSIONS` (images and common document formats). Files are uploaded to OpenWebUI's internal storage via `open_webui.models.files` and `open_webui.storage.provider`. This import is optional — the Pipe works without it, it just skips artifact surfacing. Images are rendered inline; documents are offered as download links.

### SSE streaming
The Pipe subscribes to OpenCode's `/event` SSE endpoint. It handles both v1.15+ events (`message.part.delta`, `message.part.updated`, `session.idle`) and legacy events (`content_block_delta`, `thinking_start/delta/stop`, `tool_use`, `message_stop`). Reasoning/thinking parts are tracked by `partID` so that text deltas can be correctly routed. If the SSE stream produces no visible text, it falls back to the synchronous HTTP response.

## Code Style Guidelines

- **Single file** — All changes must stay inside `opencode_pipe.py`. Do not introduce additional Python modules or packages.
- **Defensive imports** — Any import from `open_webui.*` must be wrapped in `try/except` because the Pipe may run in environments where OpenWebUI internals are unavailable.
- **Optional dependencies** — `httpx` and `docker` are checked at runtime with graceful fallbacks (error messages or subprocess CLI).
- **Type hints** — The codebase uses `typing` imports (`Optional`, `Dict`, `List`, `AsyncGenerator`, etc.) and type annotations.
- **Async throughout** — The `pipe()` method and all HTTP helpers are `async`. Use `asyncio` primitives where needed.
- **Logging** — Use `log = logging.getLogger(__name__)` rather than `print()`.
- **Constants at module level** — Artifact extensions, tool preview fields, and max size limits are defined as module-level constants prefixed with `_`.

## Testing Instructions

There is **no automated test suite**. Manual testing workflow:

1. Build the Docker image:
   ```bash
   docker build -t opencode-pipe:latest .
   ```
2. Paste `opencode_pipe.py` into OpenWebUI's Functions editor.
3. Configure Valves (at minimum set an API key, `PROVIDER`, and `MODEL`).
4. Start a new chat with the **OpenCode Agent** model and send a message.
5. Watch OpenWebUI logs and container logs (`docker logs opencode-pipe-{chat_id}`) for errors.
6. For SSE streaming issues, test with `STREAMING=False` to verify the non-streaming path works.

## Security Considerations

- **Per-chat isolation** — Each chat runs in its own container. One chat cannot access another chat's workspace.
- **No host Docker socket inside agent containers** — The agent container only has access to its own `/workspace` directory.
- **API keys in environment variables** — Keys are passed as env vars and are not written to disk inside the container (except if OpenCode itself persists them to its auth file).
- **Custom provider keys via env substitution** — For `PROVIDER=custom`, the API key is referenced as `{env:CUSTOM_OPENCODE_API_KEY}` in `opencode.json` rather than hardcoded.
- **File size limits** — Artifact uploads are capped at 50 MiB (`_MAX_ARTIFACT_BYTES`).
- **No code executes on the host** — All agent tool execution happens inside the Docker sandbox.

## OpenCode HTTP API (v1.15+)

The Pipe targets OpenCode v1.15+. Key API changes from older versions:

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

## Development Notes

- The Dockerfile uses `node:20-slim` and installs `opencode-ai` globally via npm. Build tools (git, python3, g++, make) are included because coding agents often need them.
- The Pipe code relies on OpenWebUI internals (`open_webui.models.files`, `open_webui.storage.provider`) for artifact upload. These imports are wrapped in `try/except` so the Pipe still works in environments where they're unavailable.
- `httpx` is required for all HTTP communication with the OpenCode container. `docker-py` is optional — the CLI fallback works without it.
- The `__event_emitter__` callback (provided by OpenWebUI) is used for status updates shown in the chat UI. It's optional and silently ignored if unavailable.
- When `PROVIDER=custom`, set `PROVIDER` to `"custom"`, `MODEL` to the model ID the custom API accepts, and `CUSTOM_BASE_URL` to the OpenAI-compatible endpoint URL (e.g., `http://host.docker.internal:11434/v1` for Ollama).
