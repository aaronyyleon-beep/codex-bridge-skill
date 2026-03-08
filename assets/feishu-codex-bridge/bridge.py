#!/usr/bin/env python3
"""Feishu long-connection <-> local Codex bridge.

No public callback URL is required. The bridge receives Feishu IM events via
WebSocket long connection and sends replies through Feishu Open API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import warnings
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

try:
    from urllib3.exceptions import NotOpenSSLWarning

    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except Exception:
    pass

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1


LOG = logging.getLogger("feishu-codex-bridge")


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
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


@dataclass
class Settings:
    feishu_app_id: str
    feishu_app_secret: str

    codex_bin: str = "codex"
    codex_default_cwd: str = str(Path.home())
    codex_home: str = str(Path.home() / ".codex")
    session_state_file: str = ".feishu_session_map.json"

    stream_chunk_chars: int = 800
    stream_flush_sec: float = 2.0
    stream_send_interval_sec: float = 0.12
    stream_pseudo_chunk_chars: int = 32
    stream_edit_in_place: bool = True
    stream_use_markdown: bool = True
    stream_update_interval_sec: float = 0.25
    stream_max_updates_per_message: int = 18
    stream_message_max_chars: int = 4000
    merge_window_sec: float = 0.3
    status_received_text: str = "⏳ 已收到，正在思考..."
    stream_markdown_title: str = ""
    require_p2p: bool = True
    allowed_open_ids: Tuple[str, ...] = ()
    allowed_chat_ids: Tuple[str, ...] = ()
    command_token: str = ""
    enable_raw_cmd: bool = False
    rate_limit_per_minute: int = 20
    max_user_text_chars: int = 8000
    send_unauthorized_notice: bool = False
    unauthorized_notice_text: str = "⛔️ 未授权访问"
    rate_limit_notice_text: str = "⏱ 请求过于频繁，请稍后再试"


@dataclass(frozen=True)
class CodexSessionInfo:
    session_id: str
    thread_name: str = ""
    updated_at: str = ""
    cwd: str = ""


@dataclass
class ChatState:
    workdir: str
    codex_session_id: str = ""
    process: Optional[subprocess.Popen] = None
    worker: Optional[threading.Thread] = None
    merge_target: Optional[FeishuTarget] = None
    merge_text_parts: List[str] = field(default_factory=list)
    merge_timer: Optional[threading.Timer] = field(default=None, repr=False)
    pending_jobs: Deque[PendingJob] = field(
        default_factory=deque
    )
    session_candidates: List[CodexSessionInfo] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class FeishuTarget:
    session_key: str
    receive_id: str
    receive_id_type: str  # open_id | chat_id
    chat_id: str
    chat_type: str
    sender_open_id: str


@dataclass
class PendingJob:
    target: FeishuTarget
    cwd: str
    argv: List[str]
    prompt_job: bool = False
    prompt_text: str = ""


class FeishuClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token_lock = threading.Lock()
        self._tenant_token = ""
        self._tenant_expire_at = 0.0

    @staticmethod
    def _request_json(
        url: str,
        payload: dict,
        headers: Optional[dict] = None,
        method: str = "POST",
        timeout: int = 20,
    ) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req_headers = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(
            url=url,
            data=data,
            headers=req_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    def _get_tenant_access_token(self) -> str:
        now = time.time()
        with self._token_lock:
            if self._tenant_token and now < self._tenant_expire_at - 60:
                return self._tenant_token

            url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            payload = {
                "app_id": self.settings.feishu_app_id,
                "app_secret": self.settings.feishu_app_secret,
            }
            resp = self._request_json(url, payload)
            if resp.get("code") != 0:
                raise RuntimeError(f"get token failed: {resp}")
            token = resp.get("tenant_access_token")
            if not token:
                raise RuntimeError(f"missing tenant_access_token: {resp}")

            expire = int(resp.get("expire", 7200))
            self._tenant_token = token
            self._tenant_expire_at = now + expire
            return token

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 1800) -> List[str]:
        if not text:
            return []
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

    @staticmethod
    def _post_content_markdown(title: str, markdown_text: str) -> str:
        payload = {
            "zh_cn": {
                "title": title,
                "content": [[{"tag": "md", "text": markdown_text}]],
            }
        }
        return json.dumps(payload, ensure_ascii=False)

    def send_text(
        self,
        receive_id: str,
        receive_id_type: str,
        text: str,
    ) -> Optional[str]:
        token = self._get_tenant_access_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?" + urllib.parse.urlencode(
            {"receive_id_type": receive_id_type}
        )
        headers = {"Authorization": f"Bearer {token}"}
        last_message_id: Optional[str] = None

        for chunk in self._chunk_text(text):
            payload = {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": chunk}, ensure_ascii=False),
            }
            resp = self._request_json(url, payload, headers=headers, method="POST")
            if resp.get("code") != 0:
                raise RuntimeError(f"send message failed: {resp}")
            data = resp.get("data") or {}
            msg_id = data.get("message_id")
            if isinstance(msg_id, str) and msg_id:
                last_message_id = msg_id
        return last_message_id

    def update_text(self, message_id: str, text: str) -> None:
        if not message_id:
            raise RuntimeError("empty message_id")
        token = self._get_tenant_access_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        resp = self._request_json(url, payload, headers=headers, method="PUT")
        if resp.get("code") != 0:
            raise RuntimeError(f"update message failed: {resp}")

    def send_markdown(
        self,
        receive_id: str,
        receive_id_type: str,
        markdown_text: str,
        *,
        title: str = "",
    ) -> Optional[str]:
        token = self._get_tenant_access_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?" + urllib.parse.urlencode(
            {"receive_id_type": receive_id_type}
        )
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "receive_id": receive_id,
            "msg_type": "post",
            "content": self._post_content_markdown(title, markdown_text),
        }
        resp = self._request_json(url, payload, headers=headers, method="POST")
        if resp.get("code") != 0:
            raise RuntimeError(f"send markdown failed: {resp}")
        data = resp.get("data") or {}
        msg_id = data.get("message_id")
        if isinstance(msg_id, str) and msg_id:
            return msg_id
        return None

    def update_markdown(
        self,
        message_id: str,
        markdown_text: str,
        *,
        title: str = "",
    ) -> None:
        if not message_id:
            raise RuntimeError("empty message_id")
        token = self._get_tenant_access_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "msg_type": "post",
            "content": self._post_content_markdown(title, markdown_text),
        }
        resp = self._request_json(url, payload, headers=headers, method="PUT")
        if resp.get("code") != 0:
            raise RuntimeError(f"update markdown failed: {resp}")


class CodexBridge:
    SEEN_MESSAGE_LIMIT = 2000

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.feishu = FeishuClient(settings)

        self._state_lock = threading.Lock()
        self._chat_states: Dict[str, ChatState] = {}
        self._persist_lock = threading.Lock()
        self._session_id_store: Dict[str, str] = {}

        self._seen_lock = threading.Lock()
        self._seen_message_ids: OrderedDict[str, float] = OrderedDict()

        self._rate_lock = threading.Lock()
        self._rate_hits: Dict[str, List[float]] = {}

        self._ws_client: Optional[object] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._load_session_store()

    def start(self) -> None:
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_sync)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self.settings.feishu_app_id,
            self.settings.feishu_app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        self._stop_event.clear()
        self._ws_thread = threading.Thread(target=self._run_ws_forever, daemon=True)
        self._ws_thread.start()
        LOG.info("long connection started (app_id=%s)", self.settings.feishu_app_id[:12])

    def stop(self) -> None:
        self._stop_event.set()
        with self._state_lock:
            states = list(self._chat_states.values())
        for state in states:
            with state.lock:
                if state.merge_timer is not None:
                    try:
                        state.merge_timer.cancel()
                    except Exception:
                        pass
                    state.merge_timer = None
                state.merge_target = None
                state.merge_text_parts.clear()
        if self._ws_client is not None:
            try:
                self._ws_client.stop()
            except Exception:
                pass
        if self._ws_thread is not None:
            self._ws_thread.join(timeout=5)
        LOG.info("bridge stopped")

    def _run_ws_forever(self) -> None:
        ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(ws_loop)
        try:
            import lark_oapi.ws.client as ws_client

            ws_client.loop = ws_loop
        except Exception:
            pass

        try:
            if self._ws_client is not None:
                LOG.info("connecting to Feishu long connection...")
                self._ws_client.start()
        except Exception:
            LOG.exception("long connection thread failed")
        finally:
            self._stop_event.set()

    def mark_seen_or_skip(self, message_id: str) -> bool:
        with self._seen_lock:
            if message_id in self._seen_message_ids:
                return False
            self._seen_message_ids[message_id] = time.time()
            while len(self._seen_message_ids) > self.SEEN_MESSAGE_LIMIT:
                self._seen_message_ids.popitem(last=False)
            return True

    def get_or_create_chat_state(self, session_key: str) -> ChatState:
        with self._state_lock:
            state = self._chat_states.get(session_key)
            if state is None:
                state = ChatState(workdir=self.settings.codex_default_cwd)
                with self._persist_lock:
                    state.codex_session_id = self._session_id_store.get(session_key, "")
                self._chat_states[session_key] = state
            return state

    def _session_store_path(self) -> Optional[Path]:
        raw = self.settings.session_state_file.strip()
        if not raw:
            return None
        return Path(raw).expanduser()

    def _load_session_store(self) -> None:
        p = self._session_store_path()
        if p is None:
            return
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("load session map failed path=%s err=%s", p, exc)
            return
        if not isinstance(data, dict):
            LOG.warning("invalid session map format path=%s", p)
            return
        cleaned: Dict[str, str] = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, str):
                key = k.strip()
                val = v.strip()
                if key and val:
                    cleaned[key] = val
        with self._persist_lock:
            self._session_id_store = cleaned
        LOG.info("loaded persisted session map path=%s count=%s", p, len(cleaned))

    def _save_session_store(self) -> None:
        p = self._session_store_path()
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with self._persist_lock:
                payload = dict(self._session_id_store)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            tmp.replace(p)
        except Exception as exc:
            LOG.warning("save session map failed path=%s err=%s", p, exc)

    def _persist_session_binding(self, session_key: str, codex_session_id: str) -> None:
        key = session_key.strip()
        if not key:
            return
        value = codex_session_id.strip()
        changed = False
        with self._persist_lock:
            old = self._session_id_store.get(key)
            if value:
                if old != value:
                    self._session_id_store[key] = value
                    changed = True
            else:
                if key in self._session_id_store:
                    del self._session_id_store[key]
                    changed = True
        if changed:
            self._save_session_store()

    def _codex_home_path(self) -> Path:
        raw = self.settings.codex_home.strip()
        if raw:
            return Path(raw).expanduser()
        return Path.home() / ".codex"

    def _codex_session_index_path(self) -> Path:
        return self._codex_home_path() / "session_index.jsonl"

    def _codex_sessions_root(self) -> Path:
        return self._codex_home_path() / "sessions"

    def _load_codex_session_index(self) -> List[CodexSessionInfo]:
        path = self._codex_session_index_path()
        if not path.exists():
            return []

        latest_by_id: Dict[str, CodexSessionInfo] = {}
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                session_id = str(data.get("id", "") or "").strip()
                if not session_id:
                    continue
                thread_name = str(data.get("thread_name", "") or "").strip()
                updated_at = str(data.get("updated_at", "") or "").strip()
                current = latest_by_id.get(session_id)
                candidate = CodexSessionInfo(
                    session_id=session_id,
                    thread_name=thread_name,
                    updated_at=updated_at,
                )
                if current is None or (
                    candidate.updated_at,
                    candidate.session_id,
                ) >= (
                    current.updated_at,
                    current.session_id,
                ):
                    latest_by_id[session_id] = candidate
        except Exception as exc:
            LOG.warning("load codex session index failed path=%s err=%s", path, exc)
            return []

        sessions = list(latest_by_id.values())
        sessions.sort(key=lambda item: (item.updated_at, item.session_id), reverse=True)
        return sessions

    def _find_codex_session_file(self, session_id: str) -> Optional[Path]:
        normalized = session_id.strip()
        if not normalized:
            return None

        root = self._codex_sessions_root()
        if not root.exists():
            return None

        pattern = f"*{normalized}.jsonl"
        try:
            for path in root.rglob(pattern):
                if path.is_file():
                    return path
        except Exception as exc:
            LOG.warning(
                "find codex session file failed session=%s root=%s err=%s",
                normalized,
                root,
                exc,
            )
        return None

    def _load_codex_session_info(self, session_id: str) -> Optional[CodexSessionInfo]:
        normalized = session_id.strip()
        if not normalized:
            return None

        base: Optional[CodexSessionInfo] = None
        for candidate in self._load_codex_session_index():
            if candidate.session_id == normalized:
                base = candidate
                break

        cwd = ""
        session_file = self._find_codex_session_file(normalized)
        if session_file is not None:
            try:
                with session_file.open("r", encoding="utf-8") as fh:
                    for idx, raw_line in enumerate(fh):
                        if idx >= 32:
                            break
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if str(data.get("type", "") or "").strip() != "session_meta":
                            continue
                        payload = data.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        payload_id = str(payload.get("id", "") or "").strip()
                        if payload_id and payload_id != normalized:
                            continue
                        cwd = str(payload.get("cwd", "") or "").strip()
                        if base is None:
                            base = CodexSessionInfo(
                                session_id=normalized,
                                thread_name=str(payload.get("thread_name", "") or "").strip(),
                                updated_at=str(payload.get("timestamp", "") or "").strip(),
                            )
                        break
            except Exception as exc:
                LOG.warning(
                    "load codex session meta failed session=%s path=%s err=%s",
                    normalized,
                    session_file,
                    exc,
                )

        if base is None and not cwd:
            return None
        if base is None:
            base = CodexSessionInfo(session_id=normalized)
        return CodexSessionInfo(
            session_id=base.session_id,
            thread_name=base.thread_name,
            updated_at=base.updated_at,
            cwd=cwd,
        )

    @staticmethod
    def _compact_session_title(title: str, limit: int = 48) -> str:
        cleaned = re.sub(r"\s+", " ", title).strip()
        if not cleaned:
            return "(untitled)"
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3].rstrip() + "..."

    def _remember_session_candidates(
        self,
        session_key: str,
        sessions: List[CodexSessionInfo],
    ) -> None:
        state = self.get_or_create_chat_state(session_key)
        with state.lock:
            state.session_candidates = list(sessions)

    @staticmethod
    def _looks_like_command(text: str) -> bool:
        return text.startswith("/")

    def _dispatch_user_text_async(self, target: FeishuTarget, text: str) -> None:
        threading.Thread(
            target=self._handle_user_text,
            args=(target, text),
            daemon=True,
        ).start()

    def _consume_merge_buffer(
        self,
        session_key: str,
        *,
        cancel_timer: bool,
    ) -> Optional[Tuple[FeishuTarget, str, int]]:
        state = self.get_or_create_chat_state(session_key)
        with state.lock:
            timer = state.merge_timer
            if cancel_timer and timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
            state.merge_timer = None

            target = state.merge_target
            parts = list(state.merge_text_parts)
            state.merge_target = None
            state.merge_text_parts.clear()

        if target is None or not parts:
            return None

        merged = "\n".join(p for p in parts if p).strip()
        if not merged:
            return None
        return target, merged, len(parts)

    def _on_merge_timer(self, session_key: str) -> None:
        if self._stop_event.is_set():
            return
        payload = self._consume_merge_buffer(session_key, cancel_timer=False)
        if payload is None:
            return
        target, merged_text, parts_count = payload
        LOG.info(
            "merge flush session=%s parts=%s chars=%s",
            session_key,
            parts_count,
            len(merged_text),
        )
        self._dispatch_user_text_async(target, merged_text)

    def _route_user_text(self, target: FeishuTarget, text: str) -> None:
        merge_window = max(0.0, self.settings.merge_window_sec)
        if merge_window <= 0 or self._looks_like_command(text):
            self._dispatch_user_text_async(target, text)
            return

        state = self.get_or_create_chat_state(target.session_key)
        with state.lock:
            state.merge_target = target
            state.merge_text_parts.append(text)
            buffered_parts = len(state.merge_text_parts)

            if state.merge_timer is not None:
                try:
                    state.merge_timer.cancel()
                except Exception:
                    pass

            timer = threading.Timer(
                merge_window,
                self._on_merge_timer,
                args=(target.session_key,),
            )
            timer.daemon = True
            state.merge_timer = timer

        LOG.info(
            "merge buffer session=%s parts=%s window=%.2fs",
            target.session_key,
            buffered_parts,
            merge_window,
        )
        timer.start()

    @staticmethod
    def _safe_json_loads(raw: str) -> dict:
        try:
            return json.loads(raw)
        except Exception:
            return {}

    @staticmethod
    def _strip_mentions(text: str, mentions: object) -> str:
        out = text
        if isinstance(mentions, list):
            for mention in mentions:
                name = getattr(mention, "name", None)
                if isinstance(name, str) and name:
                    out = out.replace(f"@{name}", "")
        out = re.sub(r"@_user_\\d+", "", out)
        return out.strip()

    @staticmethod
    def _extract_text_from_content(content: object) -> str:
        text_keys = {"text", "title", "summary", "topic", "user_name", "name", "value"}
        chunks: List[str] = []

        def add(piece: object) -> None:
            if not isinstance(piece, str):
                return
            cleaned = piece.strip()
            if cleaned:
                chunks.append(cleaned)

        def walk(node: object) -> None:
            if isinstance(node, dict):
                tag = str(node.get("tag", "") or "").strip().lower()
                if tag == "at":
                    mention_name = (
                        node.get("user_name")
                        or node.get("name")
                        or node.get("text")
                    )
                    if isinstance(mention_name, str) and mention_name.strip():
                        add("@" + mention_name.strip())

                for key, value in node.items():
                    if isinstance(value, str):
                        key_lower = key.lower()
                        if key_lower in text_keys:
                            if tag == "at" and key_lower in {"user_name", "name", "text"}:
                                continue
                            add(value)
                    elif isinstance(value, (dict, list)):
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(content)
        if not chunks:
            return ""

        # Keep order but remove duplicates to reduce repeated lines in post payloads.
        deduped: List[str] = []
        seen: set = set()
        for chunk in chunks:
            if chunk in seen:
                continue
            seen.add(chunk)
            deduped.append(chunk)
        return "\n".join(deduped).strip()

    def _consume_rate_limit(self, sender_key: str) -> bool:
        limit = max(0, self.settings.rate_limit_per_minute)
        if limit <= 0:
            return True

        now = time.time()
        cutoff = now - 60.0
        with self._rate_lock:
            hits = self._rate_hits.get(sender_key, [])
            hits = [ts for ts in hits if ts >= cutoff]
            if len(hits) >= limit:
                self._rate_hits[sender_key] = hits
                return False
            hits.append(now)
            self._rate_hits[sender_key] = hits
            if len(self._rate_hits) > 2000:
                # Keep memory bounded.
                self._rate_hits = {
                    k: v
                    for k, v in self._rate_hits.items()
                    if v and v[-1] >= cutoff
                }
            return True

    def _authorize_and_normalize_text(
        self,
        target: FeishuTarget,
        text: str,
    ) -> Tuple[bool, str, str]:
        if self.settings.require_p2p and target.chat_type != "p2p":
            return False, "", "require_p2p"

        if (
            self.settings.allowed_chat_ids
            and target.chat_id not in self.settings.allowed_chat_ids
        ):
            return False, "", "chat_not_allowed"

        if (
            self.settings.allowed_open_ids
            and target.sender_open_id not in self.settings.allowed_open_ids
        ):
            return False, "", "sender_not_allowed"

        normalized = text.strip()
        token = self.settings.command_token.strip()
        if token:
            prefix = token + " "
            if normalized == token:
                return False, "", "token_only"
            if not normalized.startswith(prefix):
                return False, "", "token_missing"
            normalized = normalized[len(prefix) :].strip()
            if not normalized:
                return False, "", "empty_after_token"

        max_chars = max(0, self.settings.max_user_text_chars)
        if max_chars and len(normalized) > max_chars:
            return False, "", "text_too_long"

        return True, normalized, ""

    def _build_target_from_event(self, data: P2ImMessageReceiveV1) -> Optional[Tuple[FeishuTarget, str, str, str, object]]:
        if not data or not getattr(data, "event", None):
            return None

        event = data.event
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if message is None or sender is None:
            return None

        message_id = str(getattr(message, "message_id", "") or "").strip()
        if not message_id:
            return None

        sender_type = str(getattr(sender, "sender_type", "") or "").strip()
        if sender_type in {"app", "bot"}:
            return None

        msg_type = str(getattr(message, "message_type", "") or "").strip()
        supported_types = {"text", "post", "merge_forward"}
        if msg_type not in supported_types:
            LOG.info(
                "skip unsupported message_type=%s message_id=%s",
                msg_type or "(empty)",
                message_id[:24],
            )
            return None

        chat_id = str(getattr(message, "chat_id", "") or "").strip()
        chat_type = str(getattr(message, "chat_type", "p2p") or "p2p").strip()

        sender_id_obj = getattr(sender, "sender_id", None)
        open_id = ""
        if sender_id_obj is not None:
            open_id = str(getattr(sender_id_obj, "open_id", "") or "").strip()
        sender_open_id = open_id or f"unknown_{message_id[:8]}"

        content_raw = str(getattr(message, "content", "") or "")
        content = self._safe_json_loads(content_raw)
        if msg_type == "text":
            text = str(content.get("text", "") or "").strip()
        else:
            text = self._extract_text_from_content(content)
        text = self._strip_mentions(text, getattr(message, "mentions", None))
        if not text:
            LOG.info(
                "empty parsed text message_type=%s message_id=%s content_preview=%s",
                msg_type,
                message_id[:24],
                content_raw[:200],
            )
            return None

        if chat_type == "group" and chat_id:
            target = FeishuTarget(
                session_key=chat_id,
                receive_id=chat_id,
                receive_id_type="chat_id",
                chat_id=chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
            )
        elif open_id:
            target = FeishuTarget(
                session_key=chat_id or open_id,
                receive_id=open_id,
                receive_id_type="open_id",
                chat_id=chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
            )
        elif chat_id:
            target = FeishuTarget(
                session_key=chat_id,
                receive_id=chat_id,
                receive_id_type="chat_id",
                chat_id=chat_id,
                chat_type=chat_type,
                sender_open_id=sender_open_id,
            )
        else:
            return None

        return target, message_id, msg_type, text, message

    def _on_message_sync(self, data: P2ImMessageReceiveV1) -> None:
        parsed = self._build_target_from_event(data)
        if parsed is None:
            return

        target, message_id, _msg_type, text, _message = parsed

        if not self.mark_seen_or_skip(message_id):
            return

        ok, normalized_text, reason = self._authorize_and_normalize_text(
            target,
            text,
        )
        if not ok:
            LOG.warning(
                "blocked message reason=%s chat=%s sender=%s",
                reason,
                target.chat_id[:24],
                target.sender_open_id[:24],
            )
            if self.settings.send_unauthorized_notice:
                self._try_send(target, self.settings.unauthorized_notice_text)
            return

        rate_key = (
            target.sender_open_id
            if target.sender_open_id and not target.sender_open_id.startswith("unknown_")
            else target.chat_id
        )
        if not self._consume_rate_limit(rate_key):
            LOG.warning(
                "rate limited chat=%s sender=%s",
                target.chat_id[:24],
                target.sender_open_id[:24],
            )
            self._try_send(target, self.settings.rate_limit_notice_text)
            return

        LOG.info(
            "recv session=%s sender=%s text=%s",
            target.session_key,
            target.sender_open_id[:24],
            normalized_text,
        )
        self._route_user_text(target, normalized_text)

    def _handle_user_text(self, target: FeishuTarget, text: str) -> None:
        if text in {"/help", "help", "h", "?"}:
            self._try_send(target, self._help_text())
            return

        if text == "/whoami":
            self._try_send(
                target,
                (
                    f"chat_id={target.chat_id}\n"
                    f"chat_type={target.chat_type}\n"
                    f"sender_open_id={target.sender_open_id}"
                ),
            )
            return

        if text == "/security":
            self._try_send(
                target,
                (
                    f"require_p2p={self.settings.require_p2p}\n"
                    f"allowed_chat_ids={','.join(self.settings.allowed_chat_ids) or '(empty)'}\n"
                    f"allowed_open_ids={','.join(self.settings.allowed_open_ids) or '(empty)'}\n"
                    f"command_token={'set' if self.settings.command_token else 'empty'}\n"
                    f"enable_raw_cmd={self.settings.enable_raw_cmd}\n"
                    f"rate_limit_per_minute={self.settings.rate_limit_per_minute}"
                ),
            )
            return

        if text == "/status":
            state = self.get_or_create_chat_state(target.session_key)
            with state.lock:
                running = state.process is not None and state.process.poll() is None
                queue_len = len(state.pending_jobs)
                merge_parts = len(state.merge_text_parts)
                session_id = state.codex_session_id or "(none)"
            store_path = self._session_store_path()
            store_value = str(store_path) if store_path is not None else "(disabled)"
            msg = (
                f"workdir={state.workdir}\n"
                f"running={'yes' if running else 'no'}\n"
                f"queued_jobs={queue_len}\n"
                f"merge_buffer_parts={merge_parts}\n"
                f"codex_session_id={session_id}\n"
                f"session_state_file={store_value}\n"
                f"codex_session_index_file={self._codex_session_index_path()}"
            )
            self._try_send(target, msg)
            return

        if text == "/session":
            self._show_session_history(target, limit=8)
            return

        if text.startswith("/session "):
            self._handle_session_command(target, text)
            return

        if text in {"/new", "/reset"}:
            self._reset_session(target)
            return

        if text == "/stop":
            self._stop_chat_job(target)
            return

        if text.startswith("/setwd "):
            new_dir = text[len("/setwd ") :].strip()
            self._set_workdir(target, new_dir)
            return

        if text.startswith("/cmd "):
            if not self.settings.enable_raw_cmd:
                self._try_send(
                    target,
                    "raw /cmd is disabled by policy",
                )
                return
            args = text[len("/cmd ") :].strip()
            self._start_raw_codex_cmd(target, args)
            return

        if text.startswith("/codex "):
            prompt = text[len("/codex ") :].strip()
            self._start_prompt(target, prompt)
            return

        self._start_prompt(target, text)

    @staticmethod
    def _help_text() -> str:
        return (
            "Commands:\n"
            "/codex <prompt>  run prompt (auto-resume same chat context)\n"
            "/cmd <args>      run raw codex command (may be disabled)\n"
            "/setwd <path>    set chat workdir\n"
            "/status          show current status\n"
            "/session         show recent local codex sessions\n"
            "/session current show current bound codex session id\n"
            "/session list [n] show recent local codex sessions\n"
            "/session search <q> search local codex sessions\n"
            "/session use <n|id> bind this chat to a local codex session\n"
            "/new             start a new chat context\n"
            "/session set <id> bind this chat to a codex session id\n"
            "/session clear   clear bound codex session id\n"
            "/reset           clear remembered chat context\n"
            "/whoami          show sender/chat ids for ACL setup\n"
            "/security        show active security policy\n"
            "/stop            stop current running codex job\n"
            "/help            show this help\n"
            "\n"
            "If no prefix is provided, message is treated as /codex prompt."
        )

    def _set_workdir(self, target: FeishuTarget, new_dir: str) -> None:
        p = Path(new_dir).expanduser()
        if not p.is_absolute():
            p = (Path(self.settings.codex_default_cwd) / p).resolve()
        if not p.exists() or not p.is_dir():
            self._try_send(target, f"invalid directory: {p}")
            return

        state = self.get_or_create_chat_state(target.session_key)
        with state.lock:
            running = state.process is not None and state.process.poll() is None
            queued = len(state.pending_jobs)
            merge_pending = len(state.merge_text_parts)
        if running or queued > 0 or merge_pending > 0:
            self._try_send(
                target,
                "cannot change workdir while a job is running, queued, or waiting for merge",
            )
            return

        new_workdir = str(p)
        old_workdir = state.workdir
        state.workdir = new_workdir
        context_cleared = False
        if old_workdir != new_workdir and state.codex_session_id:
            state.codex_session_id = ""
            self._persist_session_binding(target.session_key, "")
            context_cleared = True

        if context_cleared:
            self._try_send(target, f"workdir updated: {p}\ncontext reset: yes")
        else:
            self._try_send(target, f"workdir updated: {p}")

    def _show_bound_session(self, target: FeishuTarget) -> None:
        state = self.get_or_create_chat_state(target.session_key)
        with state.lock:
            running = state.process is not None and state.process.poll() is None
            queued = len(state.pending_jobs)
            merged = len(state.merge_text_parts)
            sid = state.codex_session_id or "(none)"
        lines = [f"codex_session_id={sid}"]
        if sid != "(none)":
            info = self._load_codex_session_info(sid)
            if info is not None:
                if info.thread_name:
                    lines.append(f"title={info.thread_name}")
                if info.updated_at:
                    lines.append(f"updated_at={info.updated_at}")
                if info.cwd:
                    lines.append(f"session_cwd={info.cwd}")
        lines.extend(
            [
                f"running={'yes' if running else 'no'}",
                f"queued_jobs={queued}",
                f"merge_buffer_parts={merged}",
            ]
        )
        self._try_send(target, "\n".join(lines))

    def _set_bound_session(
        self,
        target: FeishuTarget,
        session_id: str,
        *,
        new_workdir: Optional[str] = None,
    ) -> bool:
        state = self.get_or_create_chat_state(target.session_key)
        normalized = session_id.strip()
        with state.lock:
            running = state.process is not None and state.process.poll() is None
            queued = len(state.pending_jobs)
            merged = len(state.merge_text_parts)
            if running or queued > 0 or merged > 0:
                return False
            if new_workdir is not None:
                state.workdir = new_workdir
            state.codex_session_id = normalized
        self._persist_session_binding(target.session_key, normalized)
        return True

    def _show_session_history(
        self,
        target: FeishuTarget,
        *,
        limit: int,
        query: str = "",
    ) -> None:
        state = self.get_or_create_chat_state(target.session_key)
        with state.lock:
            current_session_id = state.codex_session_id.strip()
        sessions = self._load_codex_session_index()
        if query:
            needle = query.casefold()
            sessions = [
                item
                for item in sessions
                if needle in item.thread_name.casefold()
                or needle in item.session_id.casefold()
            ]
        sessions = sessions[:limit]
        self._remember_session_candidates(target.session_key, sessions)

        if not sessions:
            label = (
                f"no local codex sessions matched query: {query}"
                if query
                else "no local codex sessions found"
            )
            self._try_send(
                target,
                f"{label}\nsession_index_file={self._codex_session_index_path()}",
            )
            return

        heading = (
            f"matching local codex sessions for: {query}"
            if query
            else "recent local codex sessions"
        )
        lines = [heading]
        for idx, item in enumerate(sessions, start=1):
            marker = " [current]" if current_session_id and item.session_id == current_session_id else ""
            lines.append(f"{idx}. {self._compact_session_title(item.thread_name)}{marker}")
            lines.append(f"id={item.session_id}")
            if item.updated_at:
                lines.append(f"updated_at={item.updated_at}")
            lines.append("")
        lines.append("use: /session use <index|id>")
        lines.append("current: /session current")
        self._try_send(target, "\n".join(lines).strip())

    def _resolve_session_reference(
        self,
        target: FeishuTarget,
        reference: str,
    ) -> Tuple[Optional[CodexSessionInfo], str]:
        normalized = reference.strip()
        if not normalized:
            return None, "usage: /session use <index|session_id>"

        if normalized.isdigit():
            index = int(normalized)
            state = self.get_or_create_chat_state(target.session_key)
            with state.lock:
                candidates = list(state.session_candidates)
            if not candidates:
                return None, "run /session list or /session search before using a numeric index"
            if index <= 0 or index > len(candidates):
                return None, f"session index out of range: 1-{len(candidates)}"
            chosen = candidates[index - 1]
            resolved = self._load_codex_session_info(chosen.session_id)
            return resolved or chosen, ""

        sessions = self._load_codex_session_index()
        exact = [item for item in sessions if item.session_id == normalized]
        if len(exact) == 1:
            resolved = self._load_codex_session_info(exact[0].session_id)
            return resolved or exact[0], ""

        prefix_matches = [item for item in sessions if item.session_id.startswith(normalized)]
        if len(prefix_matches) == 1:
            resolved = self._load_codex_session_info(prefix_matches[0].session_id)
            return resolved or prefix_matches[0], ""
        if len(prefix_matches) > 1:
            preview = ", ".join(item.session_id for item in prefix_matches[:3])
            return None, f"ambiguous session id prefix: {preview}"

        fallback = self._load_codex_session_info(normalized)
        if fallback is not None:
            return fallback, ""
        return None, f"session not found: {normalized}"

    def _use_history_session(self, target: FeishuTarget, reference: str) -> None:
        session_info, err = self._resolve_session_reference(target, reference)
        if session_info is None:
            self._try_send(target, err)
            return

        state = self.get_or_create_chat_state(target.session_key)
        with state.lock:
            current_workdir = state.workdir

        new_workdir: Optional[str] = None
        workdir_note = ""
        if session_info.cwd:
            session_cwd = Path(session_info.cwd).expanduser()
            if session_cwd.exists() and session_cwd.is_dir():
                new_workdir = str(session_cwd)
            else:
                workdir_note = f"session_cwd_missing={session_info.cwd}"
        else:
            workdir_note = "session_cwd=(unknown)"

        if not self._set_bound_session(
            target,
            session_info.session_id,
            new_workdir=new_workdir,
        ):
            self._try_send(
                target,
                "cannot change session while a job is running, queued, or waiting for merge",
            )
            return

        lines = [f"bound codex_session_id={session_info.session_id}"]
        if session_info.thread_name:
            lines.append(f"title={session_info.thread_name}")
        if session_info.updated_at:
            lines.append(f"updated_at={session_info.updated_at}")
        if new_workdir is not None:
            lines.append(f"workdir={new_workdir}")
            if new_workdir != current_workdir:
                lines.append("workdir_synced=yes")
        elif workdir_note:
            lines.append(workdir_note)
        self._try_send(target, "\n".join(lines))

    def _handle_session_command(self, target: FeishuTarget, text: str) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            self._try_send(
                target,
                "usage: /session [current|list [n]|search <query>|use <id|index>|set <id>|clear|new]",
            )
            return

        sub = parts[1].strip().lower()
        if sub in {"current", "show", "bound"}:
            self._show_bound_session(target)
            return

        if sub in {"new", "reset", "fresh"}:
            self._reset_session(target)
            return

        if sub == "list":
            limit = 8
            if len(parts) >= 3 and parts[2].strip():
                try:
                    limit = int(parts[2].strip())
                except ValueError:
                    self._try_send(target, "usage: /session list [1-20]")
                    return
            if limit < 1 or limit > 20:
                self._try_send(target, "usage: /session list [1-20]")
                return
            self._show_session_history(target, limit=limit)
            return

        if sub == "search":
            if len(parts) < 3 or not parts[2].strip():
                self._try_send(target, "usage: /session search <query>")
                return
            self._show_session_history(target, limit=8, query=parts[2].strip())
            return

        if sub == "use":
            if len(parts) < 3 or not parts[2].strip():
                self._try_send(target, "usage: /session use <index|session_id>")
                return
            self._use_history_session(target, parts[2].strip())
            return

        if sub == "set":
            if len(parts) < 3 or not parts[2].strip():
                self._try_send(target, "usage: /session set <session_id>")
                return
            new_id = parts[2].strip()
            if not self._set_bound_session(target, new_id):
                self._try_send(
                    target,
                    "cannot set session while a job is running, queued, or waiting for merge",
                )
                return
            self._try_send(target, f"bound codex_session_id={new_id}")
            return

        if sub in {"clear", "reset"}:
            if not self._set_bound_session(target, ""):
                self._try_send(
                    target,
                    "cannot clear session while a job is running, queued, or waiting for merge",
                )
                return
            self._try_send(target, "bound codex_session_id cleared")
            return

        self._try_send(
            target,
            "usage: /session [current|list [n]|search <query>|use <id|index>|set <id>|clear|new]",
        )

    def _reset_session(self, target: FeishuTarget) -> None:
        state = self.get_or_create_chat_state(target.session_key)
        should_clear_persisted = False
        with state.lock:
            running = state.process is not None and state.process.poll() is None
            if running:
                dropped = 0
                merged = 0
            else:
                dropped = len(state.pending_jobs)
                state.pending_jobs.clear()
                merged = len(state.merge_text_parts)
                state.merge_text_parts.clear()
                if state.merge_timer is not None:
                    try:
                        state.merge_timer.cancel()
                    except Exception:
                        pass
                    state.merge_timer = None
                state.merge_target = None
                state.codex_session_id = ""
                should_clear_persisted = True
        if should_clear_persisted:
            self._persist_session_binding(target.session_key, "")
        if running:
            self._try_send(target, "cannot reset context while a job is running")
            return

        if dropped > 0:
            self._try_send(
                target,
                f"context reset; cleared queued jobs={dropped}, merged_parts={merged}",
            )
        else:
            self._try_send(target, f"context reset; merged_parts={merged}")

    def _stop_chat_job(self, target: FeishuTarget) -> None:
        state = self.get_or_create_chat_state(target.session_key)
        with state.lock:
            proc = state.process
            dropped = len(state.pending_jobs)
            merged = len(state.merge_text_parts)
            if dropped > 0:
                state.pending_jobs.clear()
            if merged > 0:
                state.merge_text_parts.clear()
            if state.merge_timer is not None:
                try:
                    state.merge_timer.cancel()
                except Exception:
                    pass
                state.merge_timer = None
            state.merge_target = None

        if proc is None or proc.poll() is not None:
            if dropped > 0 or merged > 0:
                self._try_send(
                    target,
                    f"no running job; cleared queued jobs={dropped}, merged_parts={merged}",
                )
            else:
                self._try_send(target, "no running job")
            return

        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        if dropped > 0 or merged > 0:
            self._try_send(
                target,
                f"stopped; cleared queued jobs={dropped}, merged_parts={merged}",
            )
        else:
            self._try_send(target, "stopped")

    def _start_prompt(self, target: FeishuTarget, prompt: str) -> None:
        if not prompt:
            self._try_send(target, "empty prompt")
            return

        state = self.get_or_create_chat_state(target.session_key)
        # Build argv at execution time so queued prompts can inherit latest context.
        self._spawn_job(
            target,
            [],
            state.workdir,
            prompt_job=True,
            prompt_text=prompt,
        )

    def _start_raw_codex_cmd(self, target: FeishuTarget, raw_args: str) -> None:
        if not raw_args:
            self._try_send(target, "usage: /cmd <codex args>")
            return

        try:
            parts = shlex.split(raw_args)
        except ValueError as exc:
            self._try_send(target, f"invalid command args: {exc}")
            return

        if not parts:
            self._try_send(target, "usage: /cmd <codex args>")
            return

        if parts[0] == "exec" and len(parts) == 1:
            self._try_send(target, "usage: /cmd exec <prompt|subcommand>")
            return

        allowed_first = {"exec", "review", "apply", "resume", "fork", "features"}
        if parts[0] not in allowed_first:
            self._try_send(target, f"first subcommand not allowed: {parts[0]}")
            return

        blocked = {"--dangerously-bypass-approvals-and-sandbox"}
        if any(p in blocked for p in parts):
            self._try_send(target, "blocked flag detected")
            return

        if parts[0] in {"exec", "review", "resume"} and "--json" not in parts:
            parts.append("--json")

        state = self.get_or_create_chat_state(target.session_key)
        argv = [self.settings.codex_bin] + parts
        self._spawn_job(target, argv, state.workdir)

    def _build_prompt_argv(
        self,
        workdir: str,
        codex_session_id: str,
        prompt: str,
    ) -> List[str]:
        if codex_session_id:
            return [
                self.settings.codex_bin,
                "exec",
                "-C",
                workdir,
                "resume",
                "--json",
                "--skip-git-repo-check",
                codex_session_id,
                "--",
                prompt,
            ]
        return [
            self.settings.codex_bin,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-C",
            workdir,
            "--",
            prompt,
        ]

    def _spawn_job(
        self,
        target: FeishuTarget,
        argv: List[str],
        cwd: str,
        *,
        prompt_job: bool = False,
        prompt_text: str = "",
    ) -> None:
        state = self.get_or_create_chat_state(target.session_key)
        queue_pos = 0
        proc: Optional[subprocess.Popen] = None
        start_error: Optional[str] = None
        exec_argv: List[str] = list(argv)
        display_argv: List[str] = list(argv)

        with state.lock:
            if state.process is not None and state.process.poll() is None:
                state.pending_jobs.append(
                    PendingJob(
                        target=target,
                        cwd=cwd,
                        argv=list(argv),
                        prompt_job=prompt_job,
                        prompt_text=prompt_text,
                    )
                )
                queue_pos = len(state.pending_jobs)
            else:
                try:
                    if prompt_job:
                        exec_argv = self._build_prompt_argv(
                            cwd,
                            state.codex_session_id,
                            prompt_text,
                        )
                        display_argv = exec_argv
                    else:
                        exec_argv = list(argv)
                        display_argv = exec_argv
                    proc = subprocess.Popen(
                        exec_argv,
                        cwd=cwd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        universal_newlines=True,
                    )
                    state.process = proc
                except FileNotFoundError:
                    start_error = f"codex binary not found: {self.settings.codex_bin}"
                except Exception as exc:
                    start_error = f"failed to start command: {exc}"

        if queue_pos > 0:
            LOG.info(
                "job queued session=%s queue_pos=%s prompt_job=%s argv=%s",
                target.session_key,
                queue_pos,
                prompt_job,
                display_argv,
            )
            self._try_send(target, f"⏳ 已加入队列，前面还有 {queue_pos - 1} 个任务")
            return

        if start_error:
            self._try_send(target, start_error)
            return
        if proc is None:
            self._try_send(target, "failed to start command: unknown error")
            return

        try:
            stream_message_id: Optional[str] = None
            if self.settings.stream_edit_in_place:
                if self.settings.stream_use_markdown:
                    stream_message_id = self._try_send_markdown(
                        target,
                        self.settings.status_received_text,
                    )
                else:
                    stream_message_id = self._try_send(
                        target,
                        self.settings.status_received_text,
                    )

            worker = threading.Thread(
                target=self._stream_process_output,
                args=(target, proc, exec_argv, stream_message_id),
                daemon=True,
            )
            with state.lock:
                if state.process is proc:
                    state.worker = worker
            worker.start()
        except Exception:
            # Keep service alive even if stream setup failed unexpectedly.
            LOG.exception("failed to initialize worker for session=%s", target.session_key)
            with state.lock:
                if state.process is proc:
                    state.process = None
                    state.worker = None
            self._try_send(target, "failed to initialize worker")

    @staticmethod
    def _extract_item_text(item: object) -> str:
        if not isinstance(item, dict):
            return ""

        chunks: List[str] = []

        def read_content_node(node: object) -> None:
            if isinstance(node, dict):
                text = node.get("text")
                if isinstance(text, str):
                    chunks.append(text)
                value = node.get("value")
                if isinstance(value, str):
                    chunks.append(value)
                content = node.get("content")
                if isinstance(content, (list, dict)):
                    read_content_node(content)
            elif isinstance(node, list):
                for sub in node:
                    read_content_node(sub)

        read_content_node(item.get("content"))
        if not chunks:
            maybe = item.get("text")
            if isinstance(maybe, str):
                chunks.append(maybe)

        return "".join(chunks)

    def _parse_codex_line(self, line: str, saw_delta: bool) -> Tuple[str, bool, str]:
        stripped = line.strip()
        if not stripped:
            return "", saw_delta, ""

        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped + "\n", saw_delta, ""

        event_type = event.get("type")
        thread_id = ""
        if event_type == "thread.started":
            maybe = event.get("thread_id")
            if isinstance(maybe, str) and maybe:
                thread_id = maybe

        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            return delta, True, thread_id

        item = event.get("item")
        if isinstance(item, dict):
            item_delta = item.get("delta")
            if isinstance(item_delta, str) and item_delta:
                return item_delta, True, thread_id

        if event_type == "error":
            msg = event.get("message")
            if isinstance(msg, str) and msg:
                return f"\n[error] {msg}\n", saw_delta, thread_id

        if event_type == "turn.failed":
            err = event.get("error", {})
            if isinstance(err, dict):
                msg = err.get("message")
                if isinstance(msg, str) and msg:
                    return f"\n[turn.failed] {msg}\n", saw_delta, thread_id
            return "\n[turn.failed]\n", saw_delta, thread_id

        if event_type == "item.completed" and isinstance(item, dict):
            if saw_delta:
                return "", saw_delta, thread_id
            txt = self._extract_item_text(item)
            if txt:
                return txt + "\n", saw_delta, thread_id

        return "", saw_delta, thread_id

    def _stream_process_output(
        self,
        target: FeishuTarget,
        proc: subprocess.Popen,
        argv: List[str],
        stream_message_id: Optional[str] = None,
    ) -> None:
        sent_chunks = 0
        sent_chars = 0
        edited_updates = 0
        use_edit_in_place = bool(stream_message_id)
        current_message_id = stream_message_id
        updates_on_current_message = 0
        max_updates_per_message = max(
            1,
            self.settings.stream_max_updates_per_message,
        )

        def truncate_for_edit(text: str) -> str:
            limit = max(200, self.settings.stream_message_max_chars)
            if len(text) <= limit:
                return text
            suffix = "\n\n...[内容过长，已截断]"
            keep = max(1, limit - len(suffix))
            return text[:keep] + suffix

        def emit_chunked(
            buffer_text: str,
            *,
            chunk_size: int,
            force_all: bool = False,
        ) -> str:
            nonlocal sent_chunks, sent_chars
            chunk = max(1, chunk_size)
            delay = max(0.0, self.settings.stream_send_interval_sec)
            remaining = buffer_text

            if force_all:
                while remaining:
                    part = remaining[:chunk]
                    self._try_send(target, part)
                    sent_chunks += 1
                    sent_chars += len(part)
                    remaining = remaining[chunk:]
                    if remaining and delay > 0:
                        time.sleep(delay)
                return ""

            while len(remaining) >= chunk:
                part = remaining[:chunk]
                self._try_send(target, part)
                sent_chunks += 1
                sent_chars += len(part)
                remaining = remaining[chunk:]
                if remaining and delay > 0:
                    time.sleep(delay)
            return remaining

        def update_in_place(content: str) -> bool:
            nonlocal edited_updates
            nonlocal current_message_id
            nonlocal updates_on_current_message
            if not current_message_id:
                return False

            payload = truncate_for_edit(content)

            def rotate_message_anchor() -> bool:
                nonlocal current_message_id, updates_on_current_message
                if self.settings.stream_use_markdown:
                    new_id = self._try_send_markdown(target, payload)
                else:
                    new_id = self._try_send(target, payload)
                if not new_id:
                    return False
                current_message_id = new_id
                updates_on_current_message = 0
                return True

            if updates_on_current_message >= max_updates_per_message:
                LOG.info(
                    "rotate stream message by local cap session=%s old_message_id=%s updates=%s",
                    target.session_key,
                    current_message_id,
                    updates_on_current_message,
                )
                return rotate_message_anchor()

            try:
                if self.settings.stream_use_markdown:
                    self.feishu.update_markdown(
                        current_message_id,
                        payload,
                        title=self.settings.stream_markdown_title,
                    )
                else:
                    self.feishu.update_text(
                        current_message_id,
                        payload,
                    )
                edited_updates += 1
                updates_on_current_message += 1
                return True
            except Exception as exc:
                if "230072" in str(exc):
                    LOG.warning(
                        "rotate stream message by Feishu edit-limit session=%s message_id=%s",
                        target.session_key,
                        current_message_id,
                    )
                    if rotate_message_anchor():
                        return True
                LOG.error(
                    "update failed message_id=%s session=%s err=%s",
                    current_message_id,
                    target.session_key,
                    exc,
                )
                return False

        buf = ""
        edit_text = ""
        saw_delta = False
        last_flush = time.monotonic()
        last_update = 0.0
        state = self.get_or_create_chat_state(target.session_key)

        assert proc.stdout is not None
        for raw in proc.stdout:
            text, saw_delta, thread_id = self._parse_codex_line(raw, saw_delta)
            if thread_id:
                updated = False
                with state.lock:
                    if state.codex_session_id != thread_id:
                        state.codex_session_id = thread_id
                        updated = True
                if updated:
                    self._persist_session_binding(target.session_key, thread_id)
                    LOG.info(
                        "codex session updated session=%s codex_session_id=%s",
                        target.session_key,
                        thread_id,
                    )
            if not text:
                continue
            just_fallback = False

            if use_edit_in_place:
                if saw_delta:
                    edit_text += text
                    now = time.monotonic()
                    if (
                        now - last_update
                        >= self.settings.stream_update_interval_sec
                    ):
                        if not update_in_place(edit_text):
                            use_edit_in_place = False
                            buf = edit_text
                            just_fallback = True
                        else:
                            last_update = now
                else:
                    pending = text
                    pseudo_chunk = min(
                        self.settings.stream_chunk_chars,
                        self.settings.stream_pseudo_chunk_chars,
                    )
                    pseudo_chunk = max(1, pseudo_chunk)
                    while pending:
                        part = pending[:pseudo_chunk]
                        pending = pending[pseudo_chunk:]
                        edit_text += part
                        now = time.monotonic()
                        should_update = (
                            (now - last_update)
                            >= self.settings.stream_update_interval_sec
                        ) or (not pending)
                        if should_update:
                            if not update_in_place(edit_text):
                                use_edit_in_place = False
                                buf = edit_text + pending
                                just_fallback = True
                                break
                            last_update = now
                        if (
                            pending
                            and self.settings.stream_send_interval_sec > 0
                        ):
                            time.sleep(self.settings.stream_send_interval_sec)
            if not use_edit_in_place:
                if not just_fallback:
                    buf += text
                current_chunk_size = (
                    self.settings.stream_chunk_chars
                    if saw_delta
                    else min(
                        self.settings.stream_chunk_chars,
                        self.settings.stream_pseudo_chunk_chars,
                    )
                )
                buf = emit_chunked(
                    buf,
                    chunk_size=current_chunk_size,
                    force_all=False,
                )
                now = time.monotonic()
                if buf and (now - last_flush) >= self.settings.stream_flush_sec:
                    self._try_send(target, buf)
                    sent_chunks += 1
                    sent_chars += len(buf)
                    buf = ""
                    last_flush = now

        rc = proc.wait()

        if use_edit_in_place and current_message_id:
            final_text = edit_text.strip() or "[empty]"
            if rc != 0:
                final_text += f"\n\n[codex exit={rc}]"
            if not update_in_place(final_text):
                use_edit_in_place = False
                buf = final_text

        if not use_edit_in_place:
            if buf:
                final_chunk_size = (
                    self.settings.stream_chunk_chars
                    if saw_delta
                    else min(
                        self.settings.stream_chunk_chars,
                        self.settings.stream_pseudo_chunk_chars,
                    )
                )
                emit_chunked(
                    buf,
                    chunk_size=final_chunk_size,
                    force_all=True,
                )
            if rc != 0:
                self._try_send(target, f"[codex exit={rc}]")
                sent_chunks += 1
                sent_chars += len(f"[codex exit={rc}]")

        LOG.info(
            "stream summary session=%s rc=%s saw_delta=%s use_edit=%s updates=%s updates_on_current_message=%s sent_chunks=%s sent_chars=%s",
            target.session_key,
            rc,
            saw_delta,
            use_edit_in_place,
            edited_updates,
            updates_on_current_message,
            sent_chunks,
            sent_chars,
        )

        next_job: Optional[PendingJob] = None
        remaining_queue = 0
        with state.lock:
            if state.process is proc:
                state.process = None
                state.worker = None
                if state.pending_jobs:
                    next_job = state.pending_jobs.popleft()
                    remaining_queue = len(state.pending_jobs)

        if next_job is not None:
            LOG.info(
                "dequeue job session=%s remaining_queue=%s",
                target.session_key,
                remaining_queue,
            )
            self._spawn_job(
                next_job.target,
                next_job.argv,
                next_job.cwd,
                prompt_job=next_job.prompt_job,
                prompt_text=next_job.prompt_text,
            )

    def _try_send(self, target: FeishuTarget, text: str) -> Optional[str]:
        if not target.receive_id or not text:
            return None
        try:
            return self.feishu.send_text(
                target.receive_id,
                target.receive_id_type,
                text,
            )
        except Exception as exc:
            LOG.error(
                "send failed receive_id_type=%s receive_id=%s err=%s",
                target.receive_id_type,
                target.receive_id,
                exc,
            )
            return None

    def _try_send_markdown(
        self,
        target: FeishuTarget,
        markdown_text: str,
    ) -> Optional[str]:
        if not target.receive_id or not markdown_text:
            return None
        try:
            return self.feishu.send_markdown(
                target.receive_id,
                target.receive_id_type,
                markdown_text,
                title=self.settings.stream_markdown_title,
            )
        except Exception as exc:
            LOG.error(
                "send markdown failed receive_id_type=%s receive_id=%s err=%s",
                target.receive_id_type,
                target.receive_id,
                exc,
            )
            return None


def load_settings() -> Settings:
    load_dotenv(Path(".env"))

    missing: List[str] = []

    def parse_csv(name: str) -> Tuple[str, ...]:
        raw = os.getenv(name, "")
        if not raw.strip():
            return ()
        values = [x.strip() for x in raw.split(",")]
        return tuple(x for x in values if x)

    def parse_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            missing.append(name)
        return value

    app_id = required("FEISHU_APP_ID")
    app_secret = required("FEISHU_APP_SECRET")

    if missing:
        raise SystemExit(f"missing env vars: {', '.join(missing)}")

    return Settings(
        feishu_app_id=app_id,
        feishu_app_secret=app_secret,
        codex_bin=os.getenv("CODEX_BIN", "codex"),
        codex_default_cwd=os.getenv("CODEX_DEFAULT_CWD", str(Path.home())),
        codex_home=os.getenv("CODEX_HOME", str(Path.home() / ".codex")),
        session_state_file=os.getenv(
            "SESSION_STATE_FILE",
            ".feishu_session_map.json",
        ),
        stream_chunk_chars=int(os.getenv("STREAM_CHUNK_CHARS", "800")),
        stream_flush_sec=float(os.getenv("STREAM_FLUSH_SEC", "2.0")),
        stream_send_interval_sec=float(
            os.getenv("STREAM_SEND_INTERVAL_SEC", "0.12")
        ),
        stream_pseudo_chunk_chars=int(
            os.getenv("STREAM_PSEUDO_CHUNK_CHARS", "32")
        ),
        stream_edit_in_place=parse_bool("STREAM_EDIT_IN_PLACE", True),
        stream_use_markdown=parse_bool("STREAM_USE_MARKDOWN", True),
        stream_update_interval_sec=float(
            os.getenv("STREAM_UPDATE_INTERVAL_SEC", "0.25")
        ),
        stream_max_updates_per_message=int(
            os.getenv("STREAM_MAX_UPDATES_PER_MESSAGE", "18")
        ),
        stream_message_max_chars=int(
            os.getenv("STREAM_MESSAGE_MAX_CHARS", "4000")
        ),
        merge_window_sec=float(
            os.getenv("MERGE_WINDOW_SEC", "0.3")
        ),
        status_received_text=os.getenv(
            "STATUS_RECEIVED_TEXT",
            "⏳ 已收到，正在思考...",
        ),
        stream_markdown_title=os.getenv(
            "STREAM_MARKDOWN_TITLE",
            "",
        ),
        require_p2p=parse_bool("REQUIRE_P2P", True),
        allowed_open_ids=parse_csv("ALLOWED_OPEN_IDS"),
        allowed_chat_ids=parse_csv("ALLOWED_CHAT_IDS"),
        command_token=os.getenv("COMMAND_TOKEN", "").strip(),
        enable_raw_cmd=parse_bool("ENABLE_RAW_CMD", False),
        rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "20")),
        max_user_text_chars=int(os.getenv("MAX_USER_TEXT_CHARS", "8000")),
        send_unauthorized_notice=parse_bool(
            "SEND_UNAUTHORIZED_NOTICE",
            False,
        ),
        unauthorized_notice_text=os.getenv(
            "UNAUTHORIZED_NOTICE_TEXT",
            "⛔️ 未授权访问",
        ),
        rate_limit_notice_text=os.getenv(
            "RATE_LIMIT_NOTICE_TEXT",
            "⏱ 请求过于频繁，请稍后再试",
        ),
    )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    settings = load_settings()

    # Fast fail checks.
    try:
        subprocess.run(
            [settings.codex_bin, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        raise SystemExit(f"codex not usable ({settings.codex_bin}): {exc}")

    bridge = CodexBridge(settings)
    bridge.start()

    try:
        while True:
            time.sleep(1)
            if bridge._stop_event.is_set():
                break
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
