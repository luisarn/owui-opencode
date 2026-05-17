"""
title: OpenCode Agent
description: Run OpenCode's agent loop from inside OpenWebUI chats, sandboxed in Docker containers.
author: Assistant
version: 0.3
license: MIT
requirements: httpx, docker
"""

import asyncio
import json
import logging
import mimetypes
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Docker helper — uses docker-py when available, falls back to CLI
# ---------------------------------------------------------------------------
class _DockerHelper:
    """Abstracts Docker operations so the Pipe works both on the host
    (docker CLI) and inside a container (docker-py + mounted socket)."""

    def __init__(self) -> None:
        self._client = None
        try:
            import docker
            self._client = docker.DockerClient(base_url="unix://var/run/docker.sock")
            log.info("Using docker-py for container management")
        except Exception:
            log.info("docker-py unavailable; falling back to docker CLI")

    # -- Container lifecycle ------------------------------------------------

    def is_running(self, name: str) -> bool:
        if self._client:
            try:
                c = self._client.containers.get(name)
                return c.status == "running"
            except Exception:
                return False
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        running = result.stdout.strip().split("\n") if result.stdout.strip() else []
        return name in running

    def remove_container(self, name: str, force: bool = False) -> None:
        if self._client:
            try:
                c = self._client.containers.get(name)
                c.remove(force=force)
            except Exception:
                pass
            return
        subprocess.run(
            ["docker", "rm", "-f" if force else "", name],
            capture_output=True,
        )

    def run_container(
        self,
        name: str,
        image: str,
        port: int,
        workdir: Path,
        env: Dict[str, str],
        network: Optional[str] = None,
    ) -> None:
        if self._client:
            kwargs: Dict[str, Any] = {
                "image": image,
                "name": name,
                "detach": True,
                "volumes": {str(workdir): {"bind": "/workspace", "mode": "rw"}},
                "working_dir": "/workspace",
                "environment": env,
                "command": ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"],
            }
            if network:
                kwargs["network"] = network
            else:
                kwargs["ports"] = {"4096/tcp": port}
            self._client.containers.run(**kwargs)
            return

        # CLI fallback
        env_flags: List[str] = []
        for k, v in env.items():
            env_flags.extend(["-e", f"{k}={v}"])
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            *([] if network else ["-p", f"{port}:4096"]),
            *(["--network", network] if network else []),
            "-v",
            f"{workdir}:/workspace",
            "-w",
            "/workspace",
            *env_flags,
            image,
            "opencode",
            "serve",
            "--hostname",
            "0.0.0.0",
            "--port",
            "4096",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to start Docker container: {result.stderr or result.stdout}"
            )


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
# chat_id -> {"name": str, "port": int, "session_id": str | None, "system_injected": bool}
_chat_containers: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Artifact constants
# ---------------------------------------------------------------------------
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
_DOWNLOAD_EXTENSIONS = {
    ".pdf",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".xml",
    ".xlsx",
    ".docx",
    ".pptx",
    ".zip",
}
_ARTIFACT_EXTENSIONS = _IMAGE_EXTENSIONS | _DOWNLOAD_EXTENSIONS
_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024  # 50 MiB

# ---------------------------------------------------------------------------
# Tool rendering helpers
# ---------------------------------------------------------------------------
_TOOL_PREVIEW_FIELDS = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebSearch": "query",
    "WebFetch": "url",
    "Task": "description",
}


def _tool_preview(name: str, tool_input: Dict[str, Any]) -> str:
    key = _TOOL_PREVIEW_FIELDS.get(name)
    if key and key in tool_input:
        raw = str(tool_input[key])
    elif tool_input:
        raw = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(tool_input.items())[:2])
    else:
        return ""
    first = raw.split("\n", 1)[0]
    truncated = first if len(first) <= 120 else first[:117] + "…"
    return truncated


def _tool_input_block(name: str, tool_input: Dict[str, Any]) -> str:
    if not tool_input:
        return "```\n(no input)\n```"
    return f"```json\n{json.dumps(tool_input, indent=2, ensure_ascii=False)}\n```"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _extract_latest_user_prompt(body: Dict[str, Any]) -> str:
    messages = body.get("messages") or []
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return ""


def _extract_system_prompt(body: Dict[str, Any]) -> Optional[str]:
    parts: List[str] = []
    for msg in body.get("messages") or []:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and piece.get("type") == "text":
                    parts.append(piece.get("text", ""))
    merged = "\n\n".join(p for p in parts if p and p.strip())
    return merged or None


def _snapshot_artifacts(scan_dir: Path) -> Dict[str, int]:
    snapshot: Dict[str, int] = {}
    if not scan_dir.exists():
        return snapshot
    for path in scan_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in _ARTIFACT_EXTENSIONS:
            try:
                snapshot[str(path)] = path.stat().st_mtime_ns
            except OSError:
                pass
    return snapshot


def _iter_artifact_files(scan_dir: Path) -> List[Path]:
    seen: List[Path] = []
    if not scan_dir.exists():
        return seen
    for path in scan_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in _ARTIFACT_EXTENSIONS:
            seen.append(path)
    return seen


def _inline_new_artifacts(
    scan_dir: Path,
    before: Dict[str, int],
    user_id: Optional[str],
) -> List[str]:
    if not user_id:
        return ["\n\n_(Can't save artifacts: no user context.)_\n"]
    try:
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage
    except Exception as exc:
        return [f"\n\n_(File store unavailable: {exc})_\n"]

    chunks: List[str] = []
    for path in sorted(_iter_artifact_files(scan_dir)):
        try:
            mtime = path.stat().st_mtime_ns
            size = path.stat().st_size
        except OSError:
            continue
        if before.get(str(path)) == mtime:
            continue
        if size > _MAX_ARTIFACT_BYTES:
            chunks.append(
                f"\n\n_(Skipped {path.name}: {size // 1024 // 1024} MiB exceeds limit.)_\n"
            )
            continue

        ext = path.suffix.lower()
        is_image = ext in _IMAGE_EXTENSIONS
        mime = mimetypes.guess_type(path.name)[0] or (
            "image/png" if is_image else "application/octet-stream"
        )

        file_id = str(uuid.uuid4())
        storage_filename = f"{file_id}_{path.name}"
        try:
            with path.open("rb") as handle:
                contents, storage_path = Storage.upload_file(
                    handle,
                    storage_filename,
                    {
                        "OpenWebUI-User-Id": user_id,
                        "OpenWebUI-File-Id": file_id,
                    },
                )
        except Exception as exc:
            log.exception("Artifact upload failed: %s", path)
            chunks.append(f"\n\n_(Failed to save {path.name}: {exc})_\n")
            continue

        try:
            Files.insert_new_file(
                user_id,
                FileForm(
                    id=file_id,
                    filename=path.name,
                    path=storage_path,
                    data={},
                    meta={
                        "name": path.name,
                        "content_type": mime,
                        "size": len(contents),
                    },
                ),
            )
        except Exception as exc:
            log.exception("Artifact DB row failed: %s", path)
            chunks.append(f"\n\n_(Saved but not linkable: {path.name}: {exc})_\n")
            continue

        if is_image:
            chunks.append(f"\n\n![{path.name}](/api/v1/files/{file_id}/content)\n")
        else:
            kib = size // 1024
            chunks.append(
                f"\n\n📎 [{path.name}](/api/v1/files/{file_id}/content) · {kib} KiB\n"
            )
    return chunks


# ---------------------------------------------------------------------------
# Pipe
# ---------------------------------------------------------------------------
class Pipe:
    class Valves(BaseModel):
        ANTHROPIC_API_KEY: str = Field(
            default="",
            description="Anthropic API key. Used when PROVIDER=anthropic.",
        )
        OPENAI_API_KEY: str = Field(
            default="",
            description="OpenAI API key. Used when PROVIDER=openai.",
        )
        GOOGLE_API_KEY: str = Field(
            default="",
            description="Google API key. Used when PROVIDER=google.",
        )
        PROVIDER: str = Field(
            default="anthropic",
            description="LLM provider ID (e.g., anthropic, openai, google, or any opencode provider).",
        )
        MODEL: str = Field(
            default="claude-3-5-sonnet-20241022",
            description="Model ID. For anthropic, use e.g. claude-3-5-sonnet-20241022. Format depends on provider.",
        )
        WORKDIR_ROOT: str = Field(
            default="/tmp/opencode-pipe",
            description="Root directory for per-chat workspaces. One subdir per chat_id.",
        )
        DOCKER_IMAGE: str = Field(
            default="opencode-pipe:latest",
            description="Docker image to use for OpenCode containers.",
        )
        DOCKER_HOST: str = Field(
            default="127.0.0.1",
            description="Host address to reach spawned Docker containers. Use 'host.docker.internal' if OpenWebUI itself runs inside Docker and you're using port mapping.",
        )
        DOCKER_NETWORK: str = Field(
            default="",
            description="Docker network name to attach spawned containers to. If set, containers are reached by name instead of port mapping. Use 'openwebui-network' for Dockge setups.",
        )
        MAX_TURNS: int = Field(
            default=30,
            description="Maximum agent turns per user message. 0 disables the cap.",
        )
        CONTAINER_TIMEOUT: int = Field(
            default=60,
            description="Seconds to wait for the OpenCode server inside a new container to become healthy.",
        )
        STREAMING: bool = Field(
            default=True,
            description="Enable experimental SSE-based streaming. Disabling falls back to single-block responses.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._docker = _DockerHelper()

    def pipes(self) -> List[Dict[str, str]]:
        return [{"id": "opencode-agent", "name": "OpenCode Agent"}]

    # -- URL helper ---------------------------------------------------------

    def _container_url(self, name: str, port: int) -> str:
        """Return the HTTP URL for reaching an OpenCode container."""
        if self.valves.DOCKER_NETWORK:
            # Containers on the same Docker network are reachable by name.
            return f"http://{name}:4096"
        return f"http://{self.valves.DOCKER_HOST}:{port}"

    # -- Docker helpers -----------------------------------------------------

    async def _ensure_container(self, chat_id: str, workdir: Path) -> int:
        info = _chat_containers.get(chat_id)
        if info and self._docker.is_running(info["name"]):
            return info["port"]

        container_name = f"opencode-pipe-{chat_id}"
        self._docker.remove_container(container_name, force=True)

        port = _find_free_port()

        env: Dict[str, str] = {}
        provider = self.valves.PROVIDER.lower()
        if provider == "anthropic" and self.valves.ANTHROPIC_API_KEY:
            env["ANTHROPIC_API_KEY"] = self.valves.ANTHROPIC_API_KEY
        elif provider == "openai" and self.valves.OPENAI_API_KEY:
            env["OPENAI_API_KEY"] = self.valves.OPENAI_API_KEY
        elif provider == "google" and self.valves.GOOGLE_API_KEY:
            env["GOOGLE_API_KEY"] = self.valves.GOOGLE_API_KEY
        else:
            if self.valves.ANTHROPIC_API_KEY:
                env["ANTHROPIC_API_KEY"] = self.valves.ANTHROPIC_API_KEY
            if self.valves.OPENAI_API_KEY:
                env["OPENAI_API_KEY"] = self.valves.OPENAI_API_KEY
            if self.valves.GOOGLE_API_KEY:
                env["GOOGLE_API_KEY"] = self.valves.GOOGLE_API_KEY

        self._docker.run_container(
            name=container_name,
            image=self.valves.DOCKER_IMAGE,
            port=port,
            workdir=workdir,
            env=env,
            network=self.valves.DOCKER_NETWORK or None,
        )

        await self._wait_for_health(container_name, port)

        _chat_containers[chat_id] = {
            "name": container_name,
            "port": port,
            "session_id": None,
            "system_injected": False,
        }
        return port

    async def _wait_for_health(self, name: str, port: int) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required but not installed.")
        url = f"{self._container_url(name, port)}/health"
        async with httpx.AsyncClient() as client:
            deadline = time.time() + self.valves.CONTAINER_TIMEOUT
            while time.time() < deadline:
                try:
                    resp = await client.get(url, timeout=2.0)
                    if resp.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.5)
        raise TimeoutError(
            f"OpenCode server did not become healthy at {url} within {self.valves.CONTAINER_TIMEOUT}s"
        )

    # -- Session helpers ----------------------------------------------------

    async def _get_or_create_session(self, name: str, port: int, chat_id: str) -> str:
        info = _chat_containers.get(chat_id, {})
        session_id = info.get("session_id")
        base_url = self._container_url(name, port)

        if session_id:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{base_url}/session/{session_id}",
                        timeout=10.0,
                    )
                    if resp.status_code == 200:
                        return session_id
            except Exception:
                pass

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/session",
                json={"title": f"OpenWebUI Chat {chat_id[:8]}"},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

        session_id = self._pluck_id(data)
        if not session_id:
            raise ValueError(f"Could not create session. Response: {json.dumps(data)}")

        _chat_containers[chat_id]["session_id"] = session_id
        _chat_containers[chat_id]["system_injected"] = False
        return session_id

    def _pluck_id(self, data: Any) -> Optional[str]:
        if isinstance(data, dict):
            if "id" in data:
                return str(data["id"])
            for key in ("data", "session", "info", "result"):
                if key in data:
                    inner = self._pluck_id(data[key])
                    if inner:
                        return inner
        return None

    # -- System prompt injection --------------------------------------------

    async def _maybe_inject_system(
        self, name: str, port: int, session_id: str, system: Optional[str], chat_id: str
    ) -> None:
        info = _chat_containers.get(chat_id, {})
        if not system or info.get("system_injected"):
            return
        base_url = self._container_url(name, port)
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{base_url}/session/{session_id}/prompt",
                json={
                    "noReply": True,
                    "parts": [{"type": "text", "text": f"System instructions:\n{system}"}],
                },
                timeout=10.0,
            )
        info["system_injected"] = True

    # -- Non-streaming prompt / response ------------------------------------

    async def _send_prompt(self, name: str, port: int, session_id: str, text: str) -> Dict[str, Any]:
        if httpx is None:
            raise RuntimeError("httpx is required but not installed.")

        body: Dict[str, Any] = {
            "parts": [{"type": "text", "text": text}],
        }
        if self.valves.PROVIDER and self.valves.MODEL:
            body["model"] = {
                "providerID": self.valves.PROVIDER,
                "modelID": self.valves.MODEL,
            }

        base_url = self._container_url(name, port)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/session/{session_id}/prompt",
                json=body,
                timeout=300.0,
            )
            resp.raise_for_status()
            return resp.json()

    def _extract_response_text(self, data: Dict[str, Any]) -> str:
        candidates = [data.get("data", {}), data]
        for candidate in candidates:
            info = candidate.get("info", candidate)
            if isinstance(info, dict):
                parts = info.get("parts", [])
                if isinstance(parts, list):
                    texts = []
                    for part in parts:
                        if isinstance(part, dict):
                            if part.get("type") == "text":
                                texts.append(str(part.get("text", "")))
                            elif part.get("type") == "thinking":
                                thinking = str(part.get("thinking", "")).strip()
                                if thinking:
                                    texts.append(
                                        f"\n\n<details>\n"
                                        f"<summary>💭 Thinking</summary>\n\n"
                                        f"{thinking}\n\n"
                                        f"</details>\n\n"
                                    )
                    if texts:
                        return "\n".join(texts)
        return f"```json\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```"

    # -- Streaming (SSE) ----------------------------------------------------

    async def _sse_listener(self, base_url: str, queue: asyncio.Queue) -> None:
        """Background task that reads the OpenCode SSE event stream."""
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "GET",
                    f"{base_url}/event/subscribe",
                    headers={"Accept": "text/event-stream"},
                    timeout=None,
                ) as response:
                    current_event_type = ""
                    current_data_lines: List[str] = []

                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            current_event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            current_data_lines.append(line[5:].strip())
                        elif line == "":
                            if current_data_lines:
                                data_str = "\n".join(current_data_lines)
                                try:
                                    data = json.loads(data_str)
                                except json.JSONDecodeError:
                                    data = {"raw": data_str}
                                await queue.put({
                                    "type": current_event_type,
                                    "data": data,
                                })
                            current_event_type = ""
                            current_data_lines = []
        except Exception as exc:
            log.warning("SSE listener error: %s", exc)
        finally:
            await queue.put(None)  # sentinel

    # -- Event parsers (defensive — tries multiple shapes) ------------------

    def _extract_text_from_event(self, ev_type: str, data: Dict[str, Any]) -> Optional[str]:
        if ev_type in ("content_block_delta", "delta"):
            delta = data.get("delta", data)
            if isinstance(delta, dict):
                if delta.get("type") == "text_delta" or "text" in delta:
                    return str(delta.get("text", ""))

        if ev_type in ("text", "text_delta"):
            return str(data.get("text", data.get("content", "")))

        if ev_type in ("message", "content", "response"):
            content = data.get("content", data.get("text", data))
            if isinstance(content, str):
                return content
            if isinstance(content, dict):
                return content.get("text", "")

        return None

    def _is_thinking_start(self, ev_type: str, data: Dict[str, Any]) -> bool:
        if ev_type == "content_block_start":
            block = data.get("content_block", data)
            return isinstance(block, dict) and block.get("type") == "thinking"
        if ev_type in ("thinking_start", "thinking"):
            return True
        return False

    def _is_thinking_delta(self, ev_type: str, data: Dict[str, Any]) -> bool:
        if ev_type == "content_block_delta":
            delta = data.get("delta", data)
            return isinstance(delta, dict) and delta.get("type") == "thinking_delta"
        if ev_type in ("thinking_delta", "thinking"):
            return "thinking" in data
        return False

    def _extract_thinking(self, data: Dict[str, Any]) -> str:
        delta = data.get("delta", data)
        if isinstance(delta, dict):
            return str(delta.get("thinking", ""))
        return str(data.get("thinking", ""))

    def _is_thinking_stop(self, ev_type: str, data: Dict[str, Any]) -> bool:
        return ev_type == "content_block_stop" or ev_type in ("thinking_stop",)

    def _format_thinking(self, text: str) -> str:
        return (
            f"\n\n<details>\n"
            f"<summary>💭 Thinking</summary>\n\n"
            f"{text.strip()}\n\n"
            f"</details>\n\n"
        )

    def _extract_tool_use(self, ev_type: str, data: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
        if ev_type in ("tool_use", "tool_use_start", "tool_call", "tool"):
            name = data.get("name", "tool")
            input_data = data.get("input", data.get("arguments", data.get("params", {})))
            if not isinstance(input_data, dict):
                input_data = {}
            return (name, input_data)
        return None

    def _format_tool_use(self, name: str, tool_input: Dict[str, Any]) -> str:
        preview = _tool_preview(name, tool_input)
        summary = f"🔧 {name}" + (f" · {preview}" if preview else "")
        body = _tool_input_block(name, tool_input)
        return (
            f"\n\n<details>\n"
            f"<summary>{summary}</summary>\n\n"
            f"{body}\n\n"
            f"</details>\n\n"
        )

    def _is_completion_event(self, ev_type: str, data: Dict[str, Any]) -> bool:
        return ev_type in ("message_stop", "complete", "done", "finished", "end")

    # -- Streaming runner ---------------------------------------------------

    async def _run_streaming(
        self,
        name: str,
        port: int,
        session_id: str,
        text: str,
        event_emitter: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        """Send a prompt and stream the response via SSE events."""
        if httpx is None:
            raise RuntimeError("httpx is required but not installed.")

        body: Dict[str, Any] = {
            "parts": [{"type": "text", "text": text}],
        }
        if self.valves.PROVIDER and self.valves.MODEL:
            body["model"] = {
                "providerID": self.valves.PROVIDER,
                "modelID": self.valves.MODEL,
            }

        base_url = self._container_url(name, port)
        event_queue: asyncio.Queue = asyncio.Queue()
        listener_task = asyncio.create_task(self._sse_listener(base_url, event_queue))

        # Give the listener a moment to establish the SSE connection
        await asyncio.sleep(0.3)

        async def _post_prompt():
            async with httpx.AsyncClient() as client:
                return await client.post(
                    f"{base_url}/session/{session_id}/prompt",
                    json=body,
                    timeout=300.0,
                )

        post_task = asyncio.create_task(_post_prompt())

        text_seen = False
        thinking_buffer = ""
        in_thinking = False
        last_status = ""

        try:
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    if post_task.done():
                        try:
                            event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            break
                        if event is None:
                            break
                    else:
                        continue

                if event is None:
                    break

                ev_type = event.get("type", "")
                data = event.get("data", {})

                chunk = self._extract_text_from_event(ev_type, data)
                if chunk:
                    text_seen = True
                    yield chunk
                    continue

                if self._is_thinking_start(ev_type, data):
                    in_thinking = True
                    thinking_buffer = ""
                    if last_status != "thinking":
                        last_status = "thinking"
                        await self._emit_status(event_emitter, "💭 Thinking…")
                    continue

                if self._is_thinking_delta(ev_type, data):
                    thinking_buffer += self._extract_thinking(data)
                    continue

                if self._is_thinking_stop(ev_type, data):
                    if in_thinking and thinking_buffer.strip():
                        yield self._format_thinking(thinking_buffer)
                    in_thinking = False
                    thinking_buffer = ""
                    continue

                tool_info = self._extract_tool_use(ev_type, data)
                if tool_info:
                    name, tool_input = tool_info
                    preview = _tool_preview(name, tool_input)
                    status_text = f"🔧 {name}" + (f": {preview}" if preview else "")
                    await self._emit_status(event_emitter, status_text)
                    yield self._format_tool_use(name, tool_input)
                    continue

                if self._is_completion_event(ev_type, data):
                    break

        finally:
            if not listener_task.done():
                listener_task.cancel()
            if not post_task.done():
                post_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass
            try:
                await post_task
            except asyncio.CancelledError:
                pass

        if not text_seen and post_task.done() and not post_task.cancelled():
            try:
                resp = post_task.result()
                resp.raise_for_status()
                data = resp.json()
                yield self._extract_response_text(data)
            except Exception as exc:
                log.warning("Streaming fallback failed: %s", exc)

    # -- File handling ------------------------------------------------------

    async def _copy_files_to_workspace(
        self,
        files: List[Dict[str, Any]],
        workdir: Path,
    ) -> None:
        for f in files:
            file_id = f.get("id") or f.get("file_id")
            name = f.get("name") or f.get("filename") or "attachment"
            if not file_id:
                continue
            try:
                from open_webui.models.files import Files
                file_obj = Files.get_file_by_id(file_id)
                if file_obj and file_obj.path:
                    src = Path(file_obj.path)
                    if src.exists():
                        dst = workdir / name
                        dst.write_bytes(src.read_bytes())
            except Exception as exc:
                log.warning("Failed to copy attachment %s: %s", name, exc)

    # -- Status emitter -----------------------------------------------------

    async def _emit_status(
        self,
        emitter: Optional[Callable],
        description: str,
        done: bool = False,
    ) -> None:
        if emitter is None:
            return
        try:
            await emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )
        except Exception:
            pass

    # -- Main pipe ----------------------------------------------------------

    async def pipe(
        self,
        body: Dict[str, Any],
        __chat_id__: Optional[str] = None,
        __event_emitter__: Optional[Callable] = None,
        __files__: Optional[List[Dict[str, Any]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __metadata__: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        if httpx is None:
            yield "_OpenCode Agent Pipe requires `httpx`. Add it to the Function requirements._"
            return

        chat_id = __chat_id__ or "default"
        workdir = Path(self.valves.WORKDIR_ROOT) / chat_id
        workdir.mkdir(parents=True, exist_ok=True)

        if __files__:
            await self._copy_files_to_workspace(__files__, workdir)

        prompt_text = _extract_latest_user_prompt(body)
        if not prompt_text:
            yield "_No user message to send to OpenCode._"
            return

        system_prompt = _extract_system_prompt(body)

        try:
            await self._emit_status(__event_emitter__, "Starting OpenCode container…")
            port = await self._ensure_container(chat_id, workdir)
            container_name = _chat_containers[chat_id]["name"]

            await self._emit_status(__event_emitter__, "Creating session…")
            session_id = await self._get_or_create_session(container_name, port, chat_id)

            await self._maybe_inject_system(container_name, port, session_id, system_prompt, chat_id)

            await self._emit_status(__event_emitter__, "OpenCode is thinking…")
            artifact_snapshot = _snapshot_artifacts(workdir)

            if self.valves.STREAMING:
                async for chunk in self._run_streaming(
                    container_name, port, session_id, prompt_text, __event_emitter__
                ):
                    yield chunk
            else:
                response_data = await self._send_prompt(container_name, port, session_id, prompt_text)
                yield self._extract_response_text(response_data)

            await self._emit_status(__event_emitter__, "Checking for artifacts…")
            for chunk in _inline_new_artifacts(
                workdir,
                artifact_snapshot,
                (__user__ or {}).get("id"),
            ):
                yield chunk

            await self._emit_status(__event_emitter__, "Done.", done=True)

        except Exception as exc:
            log.exception("OpenCode pipe failed")
            await self._emit_status(__event_emitter__, f"Error: {exc}", done=True)
            yield f"\n\n**OpenCode error:** `{type(exc).__name__}: {exc}`\n"
