"""
Hermes Web Chat Server
A simple browser-based chat UI for Hermes Agent via ACP stdio bridge.
"""
import asyncio
import json
import os
import queue
import select
import subprocess
import threading
from pathlib import Path
from typing import Optional
import datetime

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import json

# ─── Paths ────────────────────────────────────────────────────────────────────
HERMES_HOME = Path.home() / ".hermes"
HERMES_AGENT = HERMES_HOME / "hermes-agent"
SKILLS_DIR = HERMES_HOME / "skills"
CUSTOM_SKILLS_DIR = SKILLS_DIR / "custom"
PYTHON = str(HERMES_AGENT / "venv/bin/python")


# ─── Bridge Protocol ─────────────────────────────────────────────────────────
from abc import ABC, abstractmethod

class Bridge(ABC):
    @abstractmethod
    async def start(self): ...

    @abstractmethod
    async def prompt(self, text: str) -> str: ...

    @abstractmethod
    async def prompt_stream(self, text: str):
        """Yield dicts: {event: 'streaming'|'done'|'error', data: str}"""
        ...

    @abstractmethod
    def stop(self): ...


# ─── Hermes Bridge (existing ACP stdio bridge) ─────────────────────────────────
class HermesBridge(Bridge):
    """Manages a persistent ACP session with Hermes Agent."""

    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.session_id: Optional[str] = None
        self._running = False
        self._update_queue: queue.Queue = queue.Queue()
        self._response_events: dict = {}
        self._responses: dict = {}
        self._read_thread: Optional[threading.Thread] = None

    def _build_env(self):
        import os
        dotenv_path = HERMES_HOME / ".env"
        env = dict(os.environ)
        if dotenv_path.exists():
            try:
                from hermes_cli.env_loader import _load_dotenv
                extra = _load_dotenv(dotenv_path)
                if extra:
                    env.update(extra)
            except Exception:
                pass
        return env

    async def start(self):
        """Start the ACP adapter and create a session."""
        if self.proc and self.proc.poll() is None:
            return

        self.proc = subprocess.Popen(
            [PYTHON, "-m", "acp_adapter.entry"],
            cwd=str(HERMES_AGENT),
            env=self._build_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )

        await asyncio.sleep(2)

        self._running = True
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()

        result = await self._send_request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "hermes-web-chat", "version": "1.0.0"}
        })

        result = await self._send_request("session/new", {
            "cwd": os.getcwd(),
            "mcpServers": []
        })
        self.session_id = result.get("sessionId")

    def _read_loop(self):
        """Background thread reading ACP stdout."""
        while self._running and self.proc and self.proc.poll() is None:
            try:
                ready = select.select([self.proc.stdout], [], [], 0.5)
                if ready[0]:
                    line = self.proc.stdout.readline()
                    if not line:
                        break
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                    self._dispatch(msg)
            except Exception as e:
                print(f"[ACP read error] {e}", flush=True)
                break

    def _dispatch(self, msg: dict):
        """Route incoming ACP messages from the read thread."""
        msg_id = msg.get("id")
        method = msg.get("method")

        if method == "session/update":
            self._update_queue.put(msg)
        elif msg_id is not None:
            if msg_id in self._response_events:
                self._responses[msg_id] = msg
                self._response_events[msg_id].set()

    async def _send_request(self, method: str, params: dict, timeout: float = 120) -> dict:
        """Send JSON-RPC request over ACP stdio and wait for response."""
        msg_id = hash(method + str(params)) % 99999
        event = asyncio.Event()
        self._response_events[msg_id] = event
        self._responses[msg_id] = None

        request = json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        ).encode() + b"\n"

        def do_write():
            self.proc.stdin.write(request)
            self.proc.stdin.flush()

        await asyncio.get_event_loop().run_in_executor(None, do_write)

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            del self._response_events[msg_id]
            raise TimeoutError(f"ACP request {method} timed out after {timeout}s")

        resp = self._responses.pop(msg_id, None)
        if resp is None:
            raise RuntimeError(f"No response for {method}")
        if "error" in resp:
            raise RuntimeError(f"ACP error: {resp['error']}")
        return resp.get("result", {})

    async def prompt(self, text: str) -> str:
        """Send a prompt and collect the complete response."""
        if not self.session_id:
            raise RuntimeError("No active session")

        # Clear stale updates
        while True:
            try:
                self._update_queue.get_nowait()
            except queue.Empty:
                break

        result = await self._send_request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}]
        })

        response_text = ""
        last_update_time = asyncio.get_event_loop().time()

        while True:
            try:
                # Wait for update with timeout
                update = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, self._update_queue.get, True, 2
                    ),
                    timeout=60
                )
                last_update_time = asyncio.get_event_loop().time()
                session_update = update.get("params", {}).get("update", {})
                update_type = session_update.get("sessionUpdate", "")

                if update_type == "agent_message_chunk":
                    content = session_update.get("content", {})
                    if content.get("type") == "text":
                        response_text += content.get("text", "")
                elif update_type == "usage_update":
                    break
            except (asyncio.TimeoutError, queue.Empty):
                # No update for 60s — assume stream is complete
                break

        return response_text

    async def prompt_stream(self, text: str):
        """Send a prompt and yield streaming events (SSE format)."""
        if not self.session_id:
            yield {"event": "error", "data": "No active session"}
            return

        # Clear stale updates
        while True:
            try:
                self._update_queue.get_nowait()
            except queue.Empty:
                break

        await self._send_request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}]
        })

        while True:
            try:
                update = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, self._update_queue.get, True, 2
                    ),
                    timeout=60
                )
                session_update = update.get("params", {}).get("update", {})
                update_type = session_update.get("sessionUpdate", "")

                if update_type == "agent_message_chunk":
                    content = session_update.get("content", {})
                    if content.get("type") == "text":
                        yield {"event": "streaming", "data": content.get("text", "")}
                elif update_type == "usage_update":
                    usage = session_update.get("usage", {})
                    yield {"event": "done", "data": json.dumps(usage)}
                    break
            except (asyncio.TimeoutError, queue.Empty):
                yield {"event": "done", "data": ""}
                break

    def stop(self):
        self._running = False
        if self.proc:
            self.proc.terminate()
            self.proc = None


# ─── Direct Bridge (OpenAI-compatible API) ────────────────────────────────────
class DirectBridge(Bridge):
    """Calls OpenAI-compatible APIs directly via HTTP."""

    def __init__(self, api_url: str, api_key: str, model_name: str):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name or "gpt-4"
        self._client = None

    async def start(self):
        import httpx
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=10),
            headers={"Authorization": f"Bearer {self.api_key}"}
            if self.api_key else {},
            follow_redirects=True,
        )

    async def prompt(self, text: str) -> str:
        async for event in self.prompt_stream(text):
            if event["event"] == "done":
                return event.get("full_content", "")
        return ""

    async def prompt_stream(self, text: str):
        if not self._client:
            yield {"event": "error", "data": "Bridge not started"}
            return
        try:
            payload = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": text}],
                "stream": True,
            }
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            async with self._client.stream("POST", f"{self.api_url}/chat/completions",
                                           json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield {"event": "error", "data": f"API error {resp.status_code}: {body.decode()}"}
                    return

                accumulated = ""
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or line == "data: [DONE]" or line == "data: done":
                        continue
                    if line.startswith("data: "):
                        raw = line[6:]
                        try:
                            chunk = json.loads(raw)
                            delta = (chunk.get("choices", [{}])[0]
                                     .get("delta", {}).get("content", "")
                                     or (chunk.get("choices", [{}])[0]
                                         .get("message", {}).get("content", "")))
                            if delta:
                                accumulated += delta
                                yield {"event": "streaming", "data": delta}
                        except json.JSONDecodeError:
                            pass
                yield {"event": "done", "data": "", "full_content": accumulated}
        except Exception as e:
            import traceback; traceback.print_exc()
            yield {"event": "error", "data": str(e)}

    def stop(self):
        if self._client:
            asyncio.create_task(self._client.aclose())


# ─── MCP Bridge (MCP JSON-RPC stdio) ──────────────────────────────────────────
class MCPBridge(Bridge):
    """Calls MCP servers via JSON-RPC over stdio."""

    def __init__(self, command: str, args: list, env: dict = None):
        self.command = command
        self.args = args
        self.extra_env = env or {}
        self.proc: Optional[subprocess.Popen] = None
        self._running = False
        self._response_events: dict = {}
        self._responses: dict = {}
        self._read_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _build_env(self):
        env = dict(os.environ)
        env.update(self.extra_env)
        return env

    async def start(self):
        if self.proc and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(
            [self.command] + self.args,
            env=self._build_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        await asyncio.sleep(1)
        self._running = True
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()
        # Initialize MCP
        await self._send_request("initialize", {"protocolVersion": "2024-11-05",
                                                 "clientInfo": {"name": "hermes-web-chat", "version": "1.0.0"}})
        # Send initialised notification
        await self._send_raw({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _read_loop(self):
        while self._running and self.proc and self.proc.poll() is None:
            try:
                ready = select.select([self.proc.stdout], [], [], 0.5)
                if ready[0]:
                    line = self.proc.stdout.readline()
                    if not line:
                        break
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                    msg_id = msg.get("id")
                    with self._lock:
                        if msg_id in self._response_events:
                            self._responses[msg_id] = msg
                            self._response_events[msg_id].set()
            except Exception as e:
                print(f"[MCP read error] {e}", flush=True)
                break

    async def _send_raw(self, obj: dict):
        data = json.dumps(obj).encode() + b"\n"
        def do_write():
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        await asyncio.get_event_loop().run_in_executor(None, do_write)

    async def _send_request(self, method: str, params: dict, timeout: float = 60) -> dict:
        msg_id = hash(method + str(params)) % 99999
        event = asyncio.Event()
        with self._lock:
            self._response_events[msg_id] = event
            self._responses[msg_id] = None
        await self._send_raw({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            with self._lock:
                del self._response_events[msg_id]
            raise TimeoutError(f"MCP request {method} timed out after {timeout}s")
        with self._lock:
            resp = self._responses.pop(msg_id, None)
        if resp is None:
            raise RuntimeError(f"No response for {method}")
        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")
        return resp.get("result", {})

    async def prompt(self, text: str) -> str:
        result = ""
        async for ev in self.prompt_stream(text):
            if ev["event"] == "streaming":
                result += ev["data"]
        return result

    async def prompt_stream(self, text: str):
        try:
            # Use the MCP completions/call_tool if available, else fall back to a simple chat tool
            # We'll call a "chat" tool if the server has one, otherwise send a tools/call
            result = await self._send_request("tools/call", {
                "name": "chat",
                "arguments": {"message": text}
            }, timeout=60)
            content = result.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        yield {"event": "streaming", "data": item["text"]}
            elif isinstance(content, str):
                yield {"event": "streaming", "data": content}
        except Exception as e:
            yield {"event": "error", "data": str(e)}
        yield {"event": "done", "data": ""}

    def stop(self):
        self._running = False
        if self.proc:
            self.proc.terminate()
            self.proc = None


# ─── Bridge Router ────────────────────────────────────────────────────────────
class BridgeRouter:
    """Routes chat requests to the appropriate bridge based on provider."""

    def __init__(self):
        self._bridge: Optional[Bridge] = None
        self._current_provider = "hermes"

    def _create_bridge(self, provider: str, api_url: str = "", api_key: str = "",
                       model_name: str = "") -> Bridge:
        if provider == "openai" and api_url:
            return DirectBridge(api_url, api_key, model_name)
        elif provider == "mcp" and api_url:
            parts = api_url.split()
            cmd = parts[0]
            args = parts[1:] if len(parts) > 1 else []
            return MCPBridge(cmd, args)
        else:
            return HermesBridge()

    async def reconfigure(self, provider: str, api_url: str = "", api_key: str = "",
                          model_name: str = ""):
        """Switch to a new bridge (reconfigure on model change)."""
        if self._bridge:
            self._bridge.stop()
        self._bridge = self._create_bridge(provider, api_url, api_key, model_name)
        self._current_provider = provider
        await self._bridge.start()

    async def prompt(self, text: str) -> str:
        return await self._bridge.prompt(text)

    async def prompt_stream(self, text: str):
        async for ev in self._bridge.prompt_stream(text):
            yield ev

    def stop(self):
        if self._bridge:
            self._bridge.stop()


# ─── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(title="Hermes Web Chat")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

router = BridgeRouter()


class ChatRequest(BaseModel):
    message: str


@app.on_event("startup")
async def startup():
    asyncio.create_task(router.reconfigure("hermes"))
    _ensure_skills_state()


@app.on_event("shutdown")
async def shutdown():
    router.stop()


@app.get("/")
async def root():
    return FileResponse(str(Path(__file__).parent / "index.html"))


@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        reply = await router.prompt(req.message)
        return {"reply": reply or "(无响应)", "session_id": "direct"}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(500, str(e))


async def event_generator(text: str):
    """Generator for SSE events."""
    try:
        async for event in router.prompt_stream(text):
            if event["event"] == "streaming":
                yield f"event: streaming\ndata: {json.dumps({'text': event['data']})}\n\n"
            elif event["event"] == "done":
                yield f"event: done\ndata: {event['data']}\n\n"
                break
            elif event["event"] == "error":
                yield f"event: error\ndata: {json.dumps({'error': event['data']})}\n\n"
                break
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE streaming endpoint for real-time chat."""
    return StreamingResponse(
        event_generator(req.message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


class ModelConfigRequest(BaseModel):
    api_url: str = ""
    api_key: str = ""
    model_name: str = ""
    provider: str = "hermes"  # "hermes" | "openai" | "mcp"

class SkillToggleRequest(BaseModel):
    skill: str = ""
    enabled: Optional[bool] = None  # None = flip current state


class SkillUploadRequest(BaseModel):
    name: str = ""
    content: str = ""
    category: str = "custom"  # "official" or "custom"


# ─── Skills Persistence ──────────────────────────────────────────────────────
SKILLS_STATE_FILE = None  # Set after HISTORY_DIR is defined


def _load_skills_state() -> dict:
    """Load persisted skills state."""
    if SKILLS_STATE_FILE is None:
        return {"active_skills": []}
    return _load_json(SKILLS_STATE_FILE, {"active_skills": []})


def _ensure_skills_state():
    """Ensure skills state is loaded (call before using _active_skills)."""
    global _active_skills, _skills_state
    if not _skills_state.get("_loaded"):
        _skills_state = _load_skills_state()
        _active_skills = set(_skills_state.get("active_skills", []))
        _skills_state["_loaded"] = True


def _save_skills_state(state: dict):
    _save_json(SKILLS_STATE_FILE, state)


def _scan_skills_dir(base_dir: Path) -> list:
    """Scan a skills directory and return list of skill metadata."""
    skills = []
    if not base_dir.exists():
        return skills
    for item in base_dir.iterdir():
        if item.is_symlink() and not item.resolve().exists():
            continue
        if item.is_dir() or (item.is_file() and item.suffix == ".md"):
            skill_name = item.stem if item.is_file() else item.name
            # Try to read frontmatter description
            description = skill_name
            trigger = f"Use when: {skill_name.replace('-', ' ')} is relevant"
            skill_file = item / "SKILL.md" if item.is_dir() else item
            if skill_file.exists():
                try:
                    text = skill_file.read_text(encoding="utf-8", errors="replace")
                    # Parse YAML frontmatter
                    if text.startswith("---"):
                        end = text.find("\n---", 3)
                        if end > 0:
                            fm = text[4:end]
                            for line in fm.splitlines():
                                if line.startswith("description:"):
                                    description = line.split(":", 1)[1].strip().strip('"').strip("'")
                                elif line.startswith("trigger:"):
                                    trigger = line.split(":", 1)[1].strip().strip('"').strip("'")
                except Exception:
                    pass
            skills.append({
                "name": skill_name,
                "path": str(item),
                "description": description,
                "trigger": trigger,
            })
    return skills


def get_all_skills() -> dict:
    """Get all skills (official + custom) with metadata."""
    official = _scan_skills_dir(SKILLS_DIR)
    # Filter out custom dir from official
    official = [s for s in official if s["name"] != "custom"]
    custom = _scan_skills_dir(CUSTOM_SKILLS_DIR)
    return {"official": official, "custom": custom}


# In-memory state (persisted to file)
_model_config = {"api_url": "", "api_key": "", "model_name": "MiniMax-M2", "provider": "hermes"}
_skills_state = {"active_skills": []}  # Lazy-loaded after HISTORY_DIR is ready
_active_skills: set = set()

@app.get("/api/model/config")
async def get_model_config():
    """Get current model configuration."""
    return {"ok": True, "data": _model_config}

@app.post("/api/model/config")
async def set_model_config(req: ModelConfigRequest):
    """Update model configuration and reconfigure the bridge."""
    global _model_config
    _model_config = {
        "api_url": req.api_url,
        "api_key": req.api_key,
        "model_name": req.model_name or "MiniMax-M2",
        "provider": req.provider or "hermes",
    }
    # Reconfigure bridge to use new settings
    asyncio.create_task(router.reconfigure(
        req.provider or "hermes",
        req.api_url,
        req.api_key,
        req.model_name or "MiniMax-M2",
    ))
    return {"ok": True, "data": _model_config}

@app.get("/api/skills")
async def get_skills():
    """Get all skills with their enabled state (legacy, returns list of active)."""
    return {"ok": True, "skills": list(_active_skills)}


@app.get("/api/skills/list")
async def list_skills():
    """Get all skills with metadata, grouped by category."""
    _ensure_skills_state()
    all_skills = get_all_skills()
    # Attach enabled state to each skill
    for s in all_skills["official"] + all_skills["custom"]:
        s["enabled"] = s["name"] in _active_skills
    return {"ok": True, **all_skills}


@app.get("/api/skills/{skill_name}")
async def get_skill_detail(skill_name: str):
    """Get full content of a skill by name."""
    # Check official skills
    for base_dir in [SKILLS_DIR, CUSTOM_SKILLS_DIR]:
        skill_path = base_dir / skill_name / "SKILL.md"
        if skill_path.exists():
            content = skill_path.read_text(encoding="utf-8", errors="replace")
            return {"ok": True, "name": skill_name, "content": content, "source": "official" if base_dir == SKILLS_DIR else "custom"}
        # Also check .md file directly
        md_path = base_dir / f"{skill_name}.md"
        if md_path.exists():
            content = md_path.read_text(encoding="utf-8", errors="replace")
            return {"ok": True, "name": skill_name, "content": content, "source": "official" if base_dir == SKILLS_DIR else "custom"}
    raise HTTPException(404, f"Skill '{skill_name}' not found")


@app.post("/api/skills/toggle")
async def toggle_skill(req: SkillToggleRequest):
    """Toggle a skill on/off and persist.
    
    If req.enabled is explicitly provided, set to that value.
    Otherwise flip the current state.
    """
    _ensure_skills_state()
    if req.enabled is None:
        # No explicit value → flip current state
        if req.skill in _active_skills:
            _active_skills.discard(req.skill)
            new_enabled = False
        else:
            _active_skills.add(req.skill)
            new_enabled = True
    elif req.enabled:
        _active_skills.add(req.skill)
        new_enabled = True
    else:
        _active_skills.discard(req.skill)
        new_enabled = False
    _save_skills_state({"active_skills": list(_active_skills)})
    return {"ok": True, "skill": req.skill, "enabled": new_enabled, "active_skills": list(_active_skills)}


@app.post("/api/skills/upload")
async def upload_skill(req: SkillUploadRequest):
    """Upload a custom skill (SKILL.md content)."""
    _ensure_skills_state()
    import re
    name = re.sub(r"[^a-z0-9\-_]", "-", req.name.lower()).strip("-")
    if not name:
        raise HTTPException(400, "Invalid skill name")

    loop = asyncio.get_event_loop()

    def _do_upload():
        CUSTOM_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        skill_dir = CUSTOM_SKILLS_DIR / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(req.content, encoding="utf-8")
        return str(skill_file)

    skill_file_path = await loop.run_in_executor(None, _do_upload)
    _active_skills.add(name)
    _save_skills_state({"active_skills": list(_active_skills)})
    return {"ok": True, "name": name, "path": skill_file_path}


class MemoryRequest(BaseModel):
    content: str = ""
    type: str = "manual"  # "auto" or "manual"

class SessionCreateRequest(BaseModel):
    title: str = "新对话"

class SessionRenameRequest(BaseModel):
    title: str = ""

class MessageSearchRequest(BaseModel):
    query: str = ""

# ─── Session & Memory Storage ────────────────────────────────────────────────
HISTORY_DIR = HERMES_HOME / "web_chat_history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
SKILLS_STATE_FILE = HISTORY_DIR / "skills_state.json"

MEMORY_FILE = HISTORY_DIR / "memory.json"
SESSIONS_INDEX = HISTORY_DIR / "sessions.json"

def _load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

def _save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_sessions_index() -> list:
    return _load_json(SESSIONS_INDEX, [])

def save_sessions_index(sessions: list):
    _save_json(SESSIONS_INDEX, sessions)

def get_session_file(session_id: str):
    return HISTORY_DIR / f"{session_id}.json"

def load_session(session_id: str) -> dict:
    return _load_json(get_session_file(session_id), {"id": session_id, "title": "新对话", "messages": [], "memory": [], "created_at": "", "updated_at": ""})

def save_session(session: dict):
    _save_json(get_session_file(session["id"]), session)
    # Update index
    sessions = get_sessions_index()
    if not any(s["id"] == session["id"] for s in sessions):
        sessions.insert(0, {"id": session["id"], "title": session.get("title", "新对话"), "created_at": session.get("created_at", "")})
        save_sessions_index(sessions)
    else:
        for s in sessions:
            if s["id"] == session["id"]:
                s["title"] = session.get("title", "新对话")
                break
        save_sessions_index(sessions)

def load_memory() -> list:
    return _load_json(MEMORY_FILE, [])

def save_memory(memory: list):
    _save_json(MEMORY_FILE, memory)

def extract_memory_from_messages(messages: list) -> list:
    """Auto-extract key info from conversation (simple heuristic)."""
    memory = load_memory()
    memory_types = {"user": [], "project": [], "pref": [], "info": []}
    for m in memory:
        memory_types.get(m.get("type", "info"), memory_types["info"]).append(m.get("content", ""))

    for msg in messages[-10:]:  # Only look at last 10 messages
        if msg.get("role") == "user":
            content = msg.get("content", "")[:200]
            if any(k in content.lower() for k in ["我叫", "我的名字", "i'm", "i am", "username"]):
                if content not in memory_types["user"]:
                    memory.append({"type": "user", "content": content, "auto": True})
            elif any(k in content.lower() for k in ["项目", "project", "工作", "workspace"]):
                if content not in memory_types["project"]:
                    memory.append({"type": "project", "content": content, "auto": True})
    return memory[:50]  # Max 50 memory items

@app.get("/api/sessions")
async def list_sessions():
    """List all sessions (summary only)."""
    sessions = get_sessions_index()
    return {"ok": True, "sessions": sessions}

@app.post("/api/sessions")
async def create_session(req: SessionCreateRequest):
    """Create a new session."""
    import uuid, datetime
    now = datetime.datetime.now().isoformat()
    session_id = uuid.uuid4().hex[:12]
    session = {
        "id": session_id,
        "title": req.title or "新对话",
        "messages": [],
        "memory": [],
        "created_at": now,
        "updated_at": now
    }
    save_session(session)
    return {"ok": True, "session": session}

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Get full session data."""
    session = load_session(session_id)
    return {"ok": True, "session": session}

@app.patch("/api/sessions/{session_id}")
async def rename_session(session_id: str, req: SessionRenameRequest):
    """Rename a session."""
    session = load_session(session_id)
    session["title"] = req.title or session["title"]
    session["updated_at"] = datetime.datetime.now().isoformat()
    save_session(session)
    return {"ok": True, "session": session}

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session."""
    sessions = get_sessions_index()
    sessions = [s for s in sessions if s["id"] != session_id]
    save_sessions_index(sessions)
    sf = get_session_file(session_id)
    if sf.exists():
        sf.unlink()
    return {"ok": True}

@app.post("/api/sessions/{session_id}/messages")
async def add_message_to_session(session_id: str, msg: dict):
    """Add a message to session and auto-extract memory."""
    session = load_session(session_id)
    session["messages"].append(msg)
    session["updated_at"] = datetime.datetime.now().isoformat()
    # Auto title from first user message
    if len(session["messages"]) == 1 and msg.get("role") == "user":
        title = msg.get("content", "")[:30].replace("\n", " ")
        session["title"] = title or "新对话"
    # Auto-extract memory
    session["memory"] = extract_memory_from_messages(session["messages"])
    save_session(session)
    return {"ok": True, "session": session}

@app.get("/api/memory")
async def get_memory():
    """Get all extracted memories."""
    return {"ok": True, "memory": load_memory()}

@app.post("/api/memory")
async def add_memory(req: MemoryRequest):
    """Manually add a memory item."""
    memory = load_memory()
    memory.insert(0, {"type": req.type, "content": req.content, "auto": False})
    memory = memory[:50]
    save_memory(memory)
    return {"ok": True, "memory": memory}

@app.delete("/api/memory/{idx}")
async def delete_memory(idx: int):
    """Delete a memory item by index."""
    memory = load_memory()
    if 0 <= idx < len(memory):
        memory.pop(idx)
        save_memory(memory)
    return {"ok": True, "memory": memory}

@app.post("/api/search")
async def search_messages(req: MessageSearchRequest):
    """Search across all session messages."""
    query = req.query.lower()
    results = []
    for sf in HISTORY_DIR.glob("*.json"):
        if sf.name == "sessions.json" or sf.name == "memory.json":
            continue
        try:
            session = json.loads(sf.read_text(encoding="utf-8"))
            for i, msg in enumerate(session.get("messages", [])):
                if query in msg.get("content", "").lower():
                    results.append({
                        "session_id": session["id"],
                        "session_title": session.get("title", ""),
                        "msg_index": i,
                        "role": msg.get("role", ""),
                        "content": msg.get("content", "")[:200],
                        "updated_at": session.get("updated_at", "")
                    })
        except Exception:
            pass
    results.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"ok": True, "results": results[:20]}

@app.get("/api/health")
async def health():
    """Health check endpoint."""
    return {
        "ok": True,
        "connected": bridge.session_id is not None,
        "session_id": bridge.session_id
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
