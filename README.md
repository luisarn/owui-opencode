# OpenWebUI OpenCode Agent Pipe

Run [OpenCode](https://opencode.ai/)'s agent loop from inside [OpenWebUI](https://openwebui.com/) chats, sandboxed in Docker containers.

This project is inspired by [openwebui-claude-code](https://github.com/tfriedel/openwebui-claude-code) but uses the open-source **OpenCode** agent instead of Claude Code, keeping everything inside disposable Docker containers for security.

---

## Features

- **Full OpenCode agent loop** — read, write, edit, bash, search, and web tools via OpenCode's native capabilities
- **Per-chat sandboxes** — each `chat_id` gets its own isolated Docker container with a dedicated workspace
- **Session persistence** — follow-up turns in the same chat resume the same OpenCode session
- **Streaming UI** — SSE-based streaming shows text, thinking blocks, and tool calls as they happen
- **Artifact upload** — images, PDFs, CSVs, and other files created by the agent are automatically detected and surfaced in the chat
- **Provider flexibility** — use Anthropic, OpenAI, Google, or any provider OpenCode supports
- **Docker sandboxed** — the agent runs inside a container with limited host access; no code executes on your host directly

---

## Prerequisites

1. **OpenWebUI** (any recent version with the Functions/Pipes framework)
2. **Docker** installed and available to the user running OpenWebUI's backend
3. API key for at least one LLM provider (Anthropic, OpenAI, Google, etc.)

> **Note:** If OpenWebUI itself runs inside a Docker container, see the **Dockge / Docker Compose** section below.

---

## Installation

### 1. Build the Docker image

Run this on the **Docker host** (where Dockge is running):

```bash
cd /path/to/this/repo
docker build -t opencode-pipe:latest .
```

This creates a small image with Node.js 20, `opencode-ai`, and common build tools installed.

### 2. Modify your Docker Compose (Dockge)

Since your OpenWebUI container needs to spawn sibling containers, add **two** things to your `docker-compose.yml`:

```yaml
  openwebui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: openwebui
    restart: unless-stopped
    ports:
      - 53000:8080
    volumes:
      - openwebui_data:/app/backend/data
      - /var/run/docker.sock:/var/run/docker.sock   # <-- ADD: Docker socket
    environment:
      - OLLAMA_API_BASE_URL=http://host.docker.internal:11434/api
      - WEBUI_SECRET_KEY=${WEBUI_SECRET_KEY:-your-secret-key-here}
      - DATABASE_URL=postgresql://openwebui:${POSTGRES_PASSWORD:-changeme}@postgres-pgvector:5432/openwebui
      - VECTOR_DB=pgvector
      - ENABLE_API_KEYS=True
    depends_on:
      postgres-pgvector:
        condition: service_healthy
    networks:
      - openwebui-network
    extra_hosts:
      - host.docker.internal:host-gateway
```

Then redeploy:

```bash
docker compose up -d openwebui
```

### 3. Install the Pipe in OpenWebUI

1. In OpenWebUI, go to **Workspace → Functions → +** (or **Admin Panel → Functions**).
2. Paste the contents of `opencode_pipe.py` into the editor.
3. Save and enable the function.
4. Open the function's **Valves** and configure at least one API key.
5. A new model named **OpenCode Agent** will appear in the model picker.

### 4. Valve configuration for Dockge

| Valve | Recommended Value | Why |
|-------|-------------------|-----|
| `ANTHROPIC_API_KEY` | your key | Provider auth |
| `PROVIDER` | `anthropic` | Provider ID |
| `MODEL` | `claude-3-5-sonnet-20241022` | Model |
| **DOCKER_NETWORK** | **`openwebui-network`** | Puts spawned containers on the same bridge network as OpenWebUI so they can talk by container name (cleanest — no port mapping needed). |
| DOCKER_HOST | `127.0.0.1` | Only used if `DOCKER_NETWORK` is empty. In your Dockge setup, leave this at default if you use `DOCKER_NETWORK`. |
| STREAMING | `True` | Real-time streaming |

> **Why `DOCKER_NETWORK`?** When set, the Pipe attaches each OpenCode container to the same Docker network as OpenWebUI. OpenWebUI reaches them directly by name (`http://opencode-pipe-{chat_id}:4096`) instead of going through the host's port mapping. This is simpler and more reliable for Docker-to-Docker setups.

---

## Configuration (Valves)

| Valve | Default | Description |
|-------|---------|-------------|
| `ANTHROPIC_API_KEY` | *(empty)* | Anthropic API key. |
| `OPENAI_API_KEY` | *(empty)* | OpenAI API key. |
| `GOOGLE_API_KEY` | *(empty)* | Google API key. |
| `PROVIDER` | `anthropic` | LLM provider ID (e.g. `anthropic`, `openai`, `google`). |
| `MODEL` | `claude-3-5-sonnet-20241022` | Model ID. Format depends on provider. |
| `WORKDIR_ROOT` | `/tmp/opencode-pipe` | Root directory for per-chat workspaces. |
| `DOCKER_IMAGE` | `opencode-pipe:latest` | Docker image used for agent containers. |
| `DOCKER_HOST` | `127.0.0.1` | Host address to reach spawned containers. Change to `host.docker.internal` if OpenWebUI runs in Docker. |
| `DOCKER_NETWORK` | *(empty)* | Docker network name to attach spawned containers to. If set, containers talk by name instead of port mapping. Use `openwebui-network` for Dockge setups. |
| `MAX_TURNS` | `30` | Max agent turns per user message. `0` disables. |
| `CONTAINER_TIMEOUT` | `60` | Seconds to wait for the OpenCode server inside a new container to become healthy. |
| `STREAMING` | `True` | Enable experimental SSE-based streaming. Falls back to single-block response if no text events are received. |

---

## How It Works

1. **First turn** in a chat:
   - The Pipe creates a new Docker container named `opencode-pipe-{chat_id}`.
   - The container runs `opencode serve --hostname 0.0.0.0 --port 4096`.
   - The Pipe waits for the health endpoint to respond.
   - A new OpenCode session is created inside the container.
   - Your prompt is sent to the session via HTTP.

2. **Follow-up turns** in the same chat:
   - The existing container is reused.
   - The same OpenCode session is resumed.
   - Context (files, prior tool calls) carries forward automatically.

3. **Docker networking**:
   - If `DOCKER_NETWORK` is set, spawned containers join that network and are reached by container name (no host port mapping).
   - If `DOCKER_NETWORK` is empty, the Pipe maps a random host port and uses `DOCKER_HOST` to reach the container.

4. **Streaming** (when `STREAMING=True`):
   - The Pipe opens an SSE connection to the container's event stream.
   - Text deltas, thinking blocks, and tool-use previews are yielded to OpenWebUI in real time.
   - If the event stream doesn't produce recognizable text, the Pipe automatically falls back to the full response.

5. **Artifacts**:
   - After each turn, the workspace directory is scanned for new or modified files.
   - Images are rendered inline; documents are offered as downloads.

6. **Cleanup**:
   - Containers are **not** auto-removed so that sessions stay alive across turns.
   - To clean up manually: `docker ps -f name=opencode-pipe-* -q | xargs docker rm -f`

---

## Architecture

```
┌─────────────────────────────────────┐
│         OpenWebUI Backend           │
│  ┌─────────────────────────────┐    │
│  │   OpenCode Agent Pipe       │    │
│  │   (Python, runs inside OWUI)│    │
│  └─────────────┬───────────────┘    │
└────────────────┼────────────────────┘
                 │ HTTP + Docker CLI
                 ▼
┌─────────────────────────────────────┐
│   Docker Container (per chat)       │
│   ┌─────────────────────────────┐   │
│   │   opencode serve            │   │
│   │   (Node.js HTTP server)     │   │
│   └─────────────┬───────────────┘   │
│                 │                   │
│   ┌─────────────▼───────────────┐   │
│   │   /workspace                │   │
│   │   (bind-mounted from host)  │   │
│   └─────────────────────────────┘   │
└─────────────────────────────────────┘
```

---

## Troubleshooting

### "Failed to start Docker container"
- Make sure the user running OpenWebUI has permission to run `docker` commands.
- Verify the image exists: `docker images | grep opencode-pipe`

### "OpenCode server did not become healthy"
- Check container logs: `docker logs opencode-pipe-{chat_id}`
- Increase the **Container Timeout** valve if your machine is slow.
- Ensure the port range used by the Pipe is not blocked by a firewall.

### "File store unavailable"
- This means the Pipe could not import OpenWebUI's internal file models. Artifact upload will be skipped, but the agent will still work.

### OpenWebUI itself runs in Docker
- Mount the Docker socket: `-v /var/run/docker.sock:/var/run/docker.sock`
- Set the **Docker Host** valve to `host.docker.internal` (Linux may need the host gateway IP instead).

---

## Security Notes

- Each chat runs in its **own container**. One chat cannot access another chat's workspace.
- The container only has access to its own `/workspace` directory plus whatever OpenCode's internal tools can reach.
- No host Docker socket is mounted **into** the agent container.
- API keys are passed as environment variables and are not written to disk inside the container (unless OpenCode persists them to its auth file).

---

## License

MIT
