#!/usr/bin/env python3
"""WeCom callback -> local Codex bridge."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import socketserver
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from Crypto.Cipher import AES


LOG = logging.getLogger("codex-wecom-bridge")


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def sha1_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    parts = sorted([token, timestamp, nonce, encrypted])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("empty padded data")
    pad = data[-1]
    if pad < 1 or pad > 32:
        raise ValueError("invalid padding")
    return data[:-pad]


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str) -> None:
        self.token = token
        self.corp_id = corp_id
        self.aes_key = base64.b64decode(encoding_aes_key + "=")
        if len(self.aes_key) != 32:
            raise ValueError("invalid WECOM_ENCODING_AES_KEY")
        self.iv = self.aes_key[:16]

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        expected = sha1_signature(self.token, timestamp, nonce, echostr)
        if expected != msg_signature:
            raise ValueError("invalid msg_signature")
        return self._decrypt(echostr)

    def decrypt_message(self, msg_signature: str, timestamp: str, nonce: str, encrypted: str) -> str:
        expected = sha1_signature(self.token, timestamp, nonce, encrypted)
        if expected != msg_signature:
            raise ValueError("invalid msg_signature")
        return self._decrypt(encrypted)

    def _decrypt(self, encrypted: str) -> str:
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.iv)
        plain = pkcs7_unpad(cipher.decrypt(base64.b64decode(encrypted)))
        content = plain[16:]
        xml_len = struct.unpack("!I", content[:4])[0]
        xml_bytes = content[4 : 4 + xml_len]
        receive_id = content[4 + xml_len :].decode("utf-8")
        if receive_id != self.corp_id:
            raise ValueError("corp_id mismatch")
        return xml_bytes.decode("utf-8")


@dataclass(frozen=True)
class CodexSessionInfo:
    session_id: str
    thread_name: str = ""
    updated_at: str = ""
    cwd: str = ""


@dataclass
class PendingJob:
    user_id: str
    prompt: str


@dataclass
class UserState:
    workdir: str
    codex_session_id: str = ""
    process: Optional[subprocess.Popen] = None
    worker: Optional[threading.Thread] = None
    pending_jobs: Deque[PendingJob] = field(default_factory=deque)
    session_candidates: List[CodexSessionInfo] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True)
class Settings:
    corp_id: str
    agent_id: int
    corp_secret: str
    token: str
    encoding_aes_key: str
    bind_host: str
    bind_port: int
    callback_path: str
    codex_bin: str
    codex_default_cwd: str
    codex_home: str
    codex_sandbox: str
    codex_auto_resume: bool
    session_state_file: str
    allowed_user_ids: Tuple[str, ...]
    command_token: str
    rate_limit_per_minute: int
    max_user_text_chars: int
    status_received_text: str


class WeComClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token = ""
        self._expire_at = 0.0
        self._lock = threading.Lock()

    def _request_json(self, url: str, payload: Optional[dict] = None, *, method: str = "GET") -> dict:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    def access_token(self) -> str:
        now = time.time()
        with self._lock:
            if self._token and now < self._expire_at - 60:
                return self._token
            qs = urllib.parse.urlencode(
                {"corpid": self.settings.corp_id, "corpsecret": self.settings.corp_secret}
            )
            resp = self._request_json(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken?" + qs,
                method="GET",
            )
            if resp.get("errcode") != 0:
                raise RuntimeError(f"gettoken failed: {resp}")
            token = str(resp.get("access_token", "") or "")
            if not token:
                raise RuntimeError(f"missing access_token: {resp}")
            self._token = token
            self._expire_at = now + int(resp.get("expires_in", 7200))
            return token

    def send_text(self, user_id: str, text: str) -> None:
        token = self.access_token()
        url = "https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=" + urllib.parse.quote(
            token, safe=""
        )
        chunks = self._chunk_text(text, 1800)
        for chunk in chunks:
            payload = {
                "touser": user_id,
                "msgtype": "text",
                "agentid": self.settings.agent_id,
                "text": {"content": chunk},
                "safe": 0,
            }
            resp = self._request_json(url, payload, method="POST")
            if resp.get("errcode") != 0:
                raise RuntimeError(f"message/send failed: {resp}")

    @staticmethod
    def _chunk_text(text: str, max_chars: int) -> List[str]:
        if len(text) <= max_chars:
            return [text]
        chunks: List[str] = []
        remaining = text
        while len(remaining) > max_chars:
            cut = remaining.rfind("\n", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks


class CodexWeComBridge:
    SEEN_LIMIT = 2000

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.crypto = WeComCrypto(settings.token, settings.encoding_aes_key, settings.corp_id)
        self.client = WeComClient(settings)
        self._state_lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._user_states: Dict[str, UserState] = {}
        self._session_store: Dict[str, Dict[str, str]] = {}
        self._rate_hits: Dict[str, List[float]] = {}
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._server: Optional[ThreadingHTTPServer] = None
        self._stop_event = threading.Event()
        self._load_session_store()

    def start(self) -> None:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                bridge.handle_http_get(self)

            def do_POST(self) -> None:
                bridge.handle_http_post(self)

            def log_message(self, fmt: str, *args: object) -> None:
                LOG.info("http " + fmt, *args)

        self._server = ThreadingHTTPServer((self.settings.bind_host, self.settings.bind_port), Handler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        LOG.info(
            "wecom callback server started host=%s port=%s path=%s",
            self.settings.bind_host,
            self.settings.bind_port,
            self.settings.callback_path,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def handle_http_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlparse(handler.path)
        if parsed.path != self.settings.callback_path:
            self._write_plain(handler, 404, "not found")
            return
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            echostr = self.crypto.verify_url(
                qs.get("msg_signature", [""])[0],
                qs.get("timestamp", [""])[0],
                qs.get("nonce", [""])[0],
                qs.get("echostr", [""])[0],
            )
        except Exception as exc:
            LOG.warning("verify url failed err=%s", exc)
            self._write_plain(handler, 403, "invalid")
            return
        self._write_plain(handler, 200, echostr)

    def handle_http_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlparse(handler.path)
        if parsed.path == "/health":
            self._write_plain(handler, 200, "ok")
            return
        if parsed.path != self.settings.callback_path:
            self._write_plain(handler, 404, "not found")
            return

        body = handler.rfile.read(int(handler.headers.get("Content-Length", "0") or "0"))
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            encrypted = ET.fromstring(body.decode("utf-8")).findtext("Encrypt", default="")
            plain_xml = self.crypto.decrypt_message(
                qs.get("msg_signature", [""])[0],
                qs.get("timestamp", [""])[0],
                qs.get("nonce", [""])[0],
                encrypted,
            )
            message = self._parse_plain_message(plain_xml)
            self._write_plain(handler, 200, "success")
        except Exception as exc:
            LOG.warning("callback handling failed err=%s", exc)
            self._write_plain(handler, 200, "success")
            return

        if message is None:
            return
        threading.Thread(target=self._handle_message, args=(message,), daemon=True).start()

    @staticmethod
    def _write_plain(handler: BaseHTTPRequestHandler, status: int, text: str) -> None:
        payload = text.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    @staticmethod
    def _parse_plain_message(xml_text: str) -> Optional[dict]:
        root = ET.fromstring(xml_text)
        msg_type = (root.findtext("MsgType", "") or "").strip()
        if msg_type != "text":
            return None
        return {
            "msg_id": (root.findtext("MsgId", "") or "").strip(),
            "user_id": (root.findtext("FromUserName", "") or "").strip(),
            "content": (root.findtext("Content", "") or "").strip(),
        }

    def _mark_seen(self, msg_id: str) -> bool:
        with self._state_lock:
            if msg_id and msg_id in self._seen:
                return False
            if msg_id:
                self._seen[msg_id] = time.time()
                while len(self._seen) > self.SEEN_LIMIT:
                    self._seen.popitem(last=False)
            return True

    def _consume_rate_limit(self, user_id: str) -> bool:
        limit = max(0, self.settings.rate_limit_per_minute)
        if limit <= 0:
            return True
        now = time.time()
        cutoff = now - 60.0
        with self._state_lock:
            hits = [ts for ts in self._rate_hits.get(user_id, []) if ts >= cutoff]
            if len(hits) >= limit:
                self._rate_hits[user_id] = hits
                return False
            hits.append(now)
            self._rate_hits[user_id] = hits
            return True

    def _authorize_text(self, user_id: str, text: str) -> Tuple[bool, str]:
        if self.settings.allowed_user_ids and user_id not in self.settings.allowed_user_ids:
            return False, ""
        normalized = text.strip()
        token = self.settings.command_token.strip()
        if token:
            prefix = token + " "
            if normalized == token or not normalized.startswith(prefix):
                return False, ""
            normalized = normalized[len(prefix) :].strip()
        if not normalized:
            return False, ""
        if self.settings.max_user_text_chars > 0 and len(normalized) > self.settings.max_user_text_chars:
            return False, ""
        return True, normalized

    def _handle_message(self, message: dict) -> None:
        msg_id = str(message.get("msg_id", "") or "")
        user_id = str(message.get("user_id", "") or "")
        text = str(message.get("content", "") or "")
        if not user_id or not self._mark_seen(msg_id):
            return
        ok, normalized = self._authorize_text(user_id, text)
        if not ok:
            return
        if not self._consume_rate_limit(user_id):
            self._send_safe(user_id, "⏱ 请求过于频繁，请稍后再试")
            return
        self._route_user_text(user_id, normalized)

    def _send_safe(self, user_id: str, text: str) -> None:
        try:
            self.client.send_text(user_id, text)
        except Exception as exc:
            LOG.error("send text failed user=%s err=%s", user_id, exc)

    def _get_or_create_user_state(self, user_id: str) -> UserState:
        with self._state_lock:
            state = self._user_states.get(user_id)
            if state is None:
                persisted = self._session_store.get(user_id, {})
                workdir = persisted.get("workdir") or self.settings.codex_default_cwd
                session_id = persisted.get("codex_session_id", "")
                state = UserState(workdir=workdir, codex_session_id=session_id)
                self._user_states[user_id] = state
            return state

    def _session_store_path(self) -> Path:
        return Path(self.settings.session_state_file).expanduser()

    def _load_session_store(self) -> None:
        path = self._session_store_path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("load session store failed path=%s err=%s", path, exc)
            return
        if isinstance(raw, dict):
            self._session_store = {
                str(k): v
                for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, dict)
            }

    def _save_session_store(self) -> None:
        path = self._session_store_path()
        with self._persist_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._session_store, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)

    def _persist_state(self, user_id: str, state: UserState) -> None:
        with state.lock:
            payload = {
                "codex_session_id": state.codex_session_id,
                "workdir": state.workdir,
            }
        with self._persist_lock:
            self._session_store[user_id] = payload
        self._save_session_store()

    def _route_user_text(self, user_id: str, text: str) -> None:
        if text in {"/help", "help", "h", "?"}:
            self._send_safe(user_id, self._help_text())
            return
        if text == "/status":
            state = self._get_or_create_user_state(user_id)
            with state.lock:
                running = state.process is not None and state.process.poll() is None
                queued = len(state.pending_jobs)
                sid = state.codex_session_id or "(none)"
                wd = state.workdir
            self._send_safe(
                user_id,
                f"workdir={wd}\nrunning={'yes' if running else 'no'}\nqueued_jobs={queued}\ncodex_session_id={sid}",
            )
            return
        if text == "/session":
            self._show_session_history(user_id, 8)
            return
        if text.startswith("/session "):
            self._handle_session_command(user_id, text)
            return
        if text in {"/new", "/reset"}:
            self._reset_state(user_id)
            return
        if text == "/stop":
            self._stop_job(user_id)
            return
        if text.startswith("/setwd "):
            self._set_workdir(user_id, text[len("/setwd ") :].strip())
            return
        if text.startswith("/codex "):
            self._start_prompt(user_id, text[len("/codex ") :].strip())
            return
        self._start_prompt(user_id, text)

    @staticmethod
    def _help_text() -> str:
        return (
            "Commands:\n"
            "/codex <prompt>  run prompt in current workdir\n"
            "/status          show current status\n"
            "/session         show recent local codex sessions\n"
            "/session current show current bound codex session id\n"
            "/session list [n] show recent local codex sessions\n"
            "/session use <n|id> bind this chat to a local codex session\n"
            "/new             start a new chat context\n"
            "/setwd <path>    set workdir\n"
            "/stop            stop current running codex job\n"
            "/help            show this help"
        )

    def _start_prompt(self, user_id: str, prompt: str) -> None:
        if not prompt.strip():
            self._send_safe(user_id, "empty prompt")
            return
        state = self._get_or_create_user_state(user_id)
        with state.lock:
            if state.process is not None and state.process.poll() is None:
                state.pending_jobs.append(PendingJob(user_id=user_id, prompt=prompt))
                ahead = len(state.pending_jobs) - 1
                self._send_safe(user_id, f"⏳ 已加入队列，前面还有 {ahead} 个任务")
                return
        self._send_safe(user_id, self.settings.status_received_text)
        self._spawn_process(user_id, prompt)

    def _spawn_process(self, user_id: str, prompt: str) -> None:
        state = self._get_or_create_user_state(user_id)
        with state.lock:
            argv = self._build_prompt_argv(state.workdir, state.codex_session_id, prompt)
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=state.workdir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )
            except FileNotFoundError:
                self._send_safe(user_id, f"codex binary not found: {self.settings.codex_bin}")
                return
            except Exception as exc:
                self._send_safe(user_id, f"failed to start codex: {exc}")
                return
            state.process = proc
            worker = threading.Thread(
                target=self._stream_process_output,
                args=(user_id, proc, prompt),
                daemon=True,
            )
            state.worker = worker
            worker.start()

    def _build_prompt_argv(self, workdir: str, session_id: str, prompt: str) -> List[str]:
        argv = [
            self.settings.codex_bin,
            "exec",
            "-s",
            self.settings.codex_sandbox,
            "--json",
            "--skip-git-repo-check",
            "-C",
            workdir,
        ]
        if self.settings.codex_auto_resume and session_id:
            argv.extend(["resume", session_id])
        argv.extend(["--", prompt])
        return argv

    def _parse_codex_line(self, line: str, saw_delta: bool) -> Tuple[str, bool, str]:
        stripped = line.strip()
        if not stripped:
            return "", saw_delta, ""
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped + "\n", saw_delta, ""
        thread_id = ""
        if event.get("type") == "thread.started":
            maybe = event.get("thread_id")
            if isinstance(maybe, str):
                thread_id = maybe
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            return delta, True, thread_id
        item = event.get("item")
        if isinstance(item, dict):
            item_delta = item.get("delta")
            if isinstance(item_delta, str) and item_delta:
                return item_delta, True, thread_id
            if event.get("type") == "item.completed" and not saw_delta:
                text = self._extract_item_text(item)
                if text:
                    return text + "\n", saw_delta, thread_id
        if event.get("type") == "turn.failed":
            return "\n[turn.failed]\n", saw_delta, thread_id
        return "", saw_delta, thread_id

    @staticmethod
    def _extract_item_text(item: dict) -> str:
        chunks: List[str] = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key in ("text", "value"):
                    value = node.get(key)
                    if isinstance(value, str):
                        chunks.append(value)
                content = node.get("content")
                if isinstance(content, (dict, list)):
                    walk(content)
            elif isinstance(node, list):
                for sub in node:
                    walk(sub)

        walk(item)
        return "".join(chunks)

    def _stream_process_output(self, user_id: str, proc: subprocess.Popen, prompt: str) -> None:
        state = self._get_or_create_user_state(user_id)
        full_output = ""
        saw_delta = False
        assert proc.stdout is not None
        for raw in proc.stdout:
            text, saw_delta, thread_id = self._parse_codex_line(raw, saw_delta)
            if thread_id:
                with state.lock:
                    if state.codex_session_id != thread_id:
                        state.codex_session_id = thread_id
                self._persist_state(user_id, state)
            if text:
                full_output += text
        rc = proc.wait()
        final = full_output.strip() or "[empty]"
        if rc != 0:
            final += f"\n\n[codex exit={rc}]"
        self._send_safe(user_id, final)

        next_job: Optional[PendingJob] = None
        with state.lock:
            if state.process is proc:
                state.process = None
                state.worker = None
                if state.pending_jobs:
                    next_job = state.pending_jobs.popleft()
        self._persist_state(user_id, state)
        if next_job is not None:
            self._spawn_process(user_id, next_job.prompt)

    def _set_workdir(self, user_id: str, raw_path: str) -> None:
        state = self._get_or_create_user_state(user_id)
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (Path(self.settings.codex_default_cwd) / path).resolve()
        if not path.exists() or not path.is_dir():
            self._send_safe(user_id, f"invalid directory: {path}")
            return
        with state.lock:
            running = state.process is not None and state.process.poll() is None
            queued = len(state.pending_jobs)
            if running or queued > 0:
                self._send_safe(user_id, "cannot change workdir while a job is running or queued")
                return
            state.workdir = str(path)
            state.codex_session_id = ""
        self._persist_state(user_id, state)
        self._send_safe(user_id, f"workdir updated: {path}\ncontext reset: yes")

    def _reset_state(self, user_id: str) -> None:
        state = self._get_or_create_user_state(user_id)
        with state.lock:
            running = state.process is not None and state.process.poll() is None
            if running:
                self._send_safe(user_id, "cannot reset while a job is running")
                return
            state.codex_session_id = ""
            state.pending_jobs.clear()
        self._persist_state(user_id, state)
        self._send_safe(user_id, "started a fresh context")

    def _stop_job(self, user_id: str) -> None:
        state = self._get_or_create_user_state(user_id)
        proc: Optional[subprocess.Popen] = None
        dropped = 0
        with state.lock:
            proc = state.process
            dropped = len(state.pending_jobs)
            state.pending_jobs.clear()
        if proc is None or proc.poll() is not None:
            self._send_safe(user_id, f"no running job; cleared queued jobs={dropped}")
            return
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._send_safe(user_id, f"stopped; cleared queued jobs={dropped}")

    def _codex_home_path(self) -> Path:
        return Path(self.settings.codex_home).expanduser()

    def _codex_session_index_path(self) -> Path:
        return self._codex_home_path() / "session_index.jsonl"

    def _codex_sessions_root(self) -> Path:
        return self._codex_home_path() / "sessions"

    def _load_codex_session_index(self) -> List[CodexSessionInfo]:
        path = self._codex_session_index_path()
        if not path.exists():
            return []
        latest: Dict[str, CodexSessionInfo] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = str(data.get("id", "") or "").strip()
            if not session_id:
                continue
            item = CodexSessionInfo(
                session_id=session_id,
                thread_name=str(data.get("thread_name", "") or "").strip(),
                updated_at=str(data.get("updated_at", "") or "").strip(),
            )
            current = latest.get(session_id)
            if current is None or (item.updated_at, item.session_id) >= (
                current.updated_at,
                current.session_id,
            ):
                latest[session_id] = item
        sessions = list(latest.values())
        sessions.sort(key=lambda x: (x.updated_at, x.session_id), reverse=True)
        return sessions

    def _find_session_file(self, session_id: str) -> Optional[Path]:
        root = self._codex_sessions_root()
        if not root.exists():
            return None
        pattern = "*" + session_id + ".jsonl"
        for path in root.rglob(pattern):
            if path.is_file():
                return path
        return None

    def _load_session_info(self, session_id: str) -> Optional[CodexSessionInfo]:
        base = None
        for item in self._load_codex_session_index():
            if item.session_id == session_id:
                base = item
                break
        session_file = self._find_session_file(session_id)
        cwd = ""
        if session_file is not None:
            for idx, raw_line in enumerate(session_file.read_text(encoding="utf-8").splitlines()):
                if idx >= 32:
                    break
                try:
                    data = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if str(data.get("type", "") or "") != "session_meta":
                    continue
                payload = data.get("payload")
                if isinstance(payload, dict):
                    cwd = str(payload.get("cwd", "") or "").strip()
                    if base is None:
                        base = CodexSessionInfo(
                            session_id=session_id,
                            thread_name=str(payload.get("thread_name", "") or "").strip(),
                            updated_at=str(payload.get("timestamp", "") or "").strip(),
                        )
                    break
        if base is None and not cwd:
            return None
        if base is None:
            base = CodexSessionInfo(session_id=session_id)
        return CodexSessionInfo(
            session_id=base.session_id,
            thread_name=base.thread_name,
            updated_at=base.updated_at,
            cwd=cwd,
        )

    def _show_session_history(self, user_id: str, limit: int) -> None:
        sessions = self._load_codex_session_index()[:limit]
        state = self._get_or_create_user_state(user_id)
        with state.lock:
            state.session_candidates = list(sessions)
            current_sid = state.codex_session_id
        if not sessions:
            self._send_safe(user_id, f"no local codex sessions found\nsession_index_file={self._codex_session_index_path()}")
            return
        lines = ["recent local codex sessions"]
        for idx, item in enumerate(sessions, start=1):
            marker = " [current]" if current_sid and item.session_id == current_sid else ""
            title = item.thread_name.strip() or "(untitled)"
            lines.append(f"{idx}. {title[:48]}{marker}")
            lines.append(f"id={item.session_id}")
            if item.updated_at:
                lines.append(f"updated_at={item.updated_at}")
            lines.append("")
        lines.append("use: /session use <index|id>")
        self._send_safe(user_id, "\n".join(lines).strip())

    def _handle_session_command(self, user_id: str, text: str) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            self._send_safe(user_id, "usage: /session [current|list [n]|use <id|index>]")
            return
        sub = parts[1].strip().lower()
        state = self._get_or_create_user_state(user_id)
        if sub == "current":
            with state.lock:
                sid = state.codex_session_id or "(none)"
                wd = state.workdir
            self._send_safe(user_id, f"codex_session_id={sid}\nworkdir={wd}")
            return
        if sub == "list":
            limit = 8
            if len(parts) == 3 and parts[2].strip():
                try:
                    limit = max(1, min(20, int(parts[2].strip())))
                except ValueError:
                    self._send_safe(user_id, "usage: /session list [1-20]")
                    return
            self._show_session_history(user_id, limit)
            return
        if sub == "use":
            if len(parts) < 3 or not parts[2].strip():
                self._send_safe(user_id, "usage: /session use <index|session_id>")
                return
            self._use_session(user_id, parts[2].strip())
            return
        self._send_safe(user_id, "usage: /session [current|list [n]|use <id|index>]")

    def _use_session(self, user_id: str, reference: str) -> None:
        state = self._get_or_create_user_state(user_id)
        if reference.isdigit():
            idx = int(reference)
            with state.lock:
                candidates = list(state.session_candidates)
            if not candidates:
                self._send_safe(user_id, "run /session or /session list before using a numeric index")
                return
            if idx < 1 or idx > len(candidates):
                self._send_safe(user_id, f"session index out of range: 1-{len(candidates)}")
                return
            info = self._load_session_info(candidates[idx - 1].session_id) or candidates[idx - 1]
        else:
            info = self._load_session_info(reference)
            if info is None:
                self._send_safe(user_id, f"session not found: {reference}")
                return

        with state.lock:
            running = state.process is not None and state.process.poll() is None
            queued = len(state.pending_jobs)
            if running or queued > 0:
                self._send_safe(user_id, "cannot change session while a job is running or queued")
                return
            state.codex_session_id = info.session_id
            if info.cwd and Path(info.cwd).expanduser().is_dir():
                state.workdir = str(Path(info.cwd).expanduser())
        self._persist_state(user_id, state)
        lines = [f"bound codex_session_id={info.session_id}"]
        if info.thread_name:
            lines.append(f"title={info.thread_name}")
        if info.updated_at:
            lines.append(f"updated_at={info.updated_at}")
        if info.cwd and Path(info.cwd).expanduser().is_dir():
            lines.append(f"workdir={Path(info.cwd).expanduser()}")
        self._send_safe(user_id, "\n".join(lines))


def parse_csv(name: str) -> Tuple[str, ...]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    load_dotenv(Path(".env"))
    required = [
        "WECOM_CORP_ID",
        "WECOM_AGENT_ID",
        "WECOM_CORP_SECRET",
        "WECOM_TOKEN",
        "WECOM_ENCODING_AES_KEY",
    ]
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise SystemExit("missing env vars: " + ", ".join(missing))
    return Settings(
        corp_id=os.getenv("WECOM_CORP_ID", "").strip(),
        agent_id=int(os.getenv("WECOM_AGENT_ID", "0")),
        corp_secret=os.getenv("WECOM_CORP_SECRET", "").strip(),
        token=os.getenv("WECOM_TOKEN", "").strip(),
        encoding_aes_key=os.getenv("WECOM_ENCODING_AES_KEY", "").strip(),
        bind_host=os.getenv("WECOM_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0",
        bind_port=int(os.getenv("WECOM_BIND_PORT", "8080")),
        callback_path=os.getenv("WECOM_CALLBACK_PATH", "/wecom/callback").strip() or "/wecom/callback",
        codex_bin=os.getenv("CODEX_BIN", "codex").strip() or "codex",
        codex_default_cwd=os.getenv("CODEX_DEFAULT_CWD", str(Path.home())).strip() or str(Path.home()),
        codex_home=os.getenv("CODEX_HOME", str(Path.home() / ".codex")).strip() or str(Path.home() / ".codex"),
        codex_sandbox=os.getenv("CODEX_SANDBOX", "workspace-write").strip() or "workspace-write",
        codex_auto_resume=parse_bool("CODEX_AUTO_RESUME", False),
        session_state_file=os.getenv("SESSION_STATE_FILE", ".wecom_session_map.json").strip() or ".wecom_session_map.json",
        allowed_user_ids=parse_csv("ALLOWED_USER_IDS"),
        command_token=os.getenv("COMMAND_TOKEN", "").strip(),
        rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "20")),
        max_user_text_chars=int(os.getenv("MAX_USER_TEXT_CHARS", "8000")),
        status_received_text=os.getenv("STATUS_RECEIVED_TEXT", "⏳ 已收到，开始处理").strip() or "⏳ 已收到，开始处理",
    )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    settings = load_settings()
    try:
        subprocess.run(
            [settings.codex_bin, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        raise SystemExit(f"codex not usable ({settings.codex_bin}): {exc}")

    bridge = CodexWeComBridge(settings)
    bridge.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
