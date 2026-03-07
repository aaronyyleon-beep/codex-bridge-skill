---
name: feishu-codex-bridge
description: Build, debug, and operate a Feishu robot that controls local Codex through Feishu events, especially long connection mode. Use when the user asks to 打通飞书、接入飞书机器人、通过飞书控制本地 codex、调试 Feishu bridge、修复飞书里的流式输出/上下文/session/队列/安全策略、从飞书切换到本机历史 Codex 会话、解释 webhook vs 长连接，或整理相关文档、配置和运维流程。
---

# Feishu Codex Bridge

## Overview

Build or maintain a bridge that receives Feishu robot messages, runs local `codex`, and sends results back to Feishu. Prefer the existing local implementation at `/Users/linxiaoyi/feishu-codex-bridge` when it exists; inspect `README.md`, `.env.example`, and `bridge.py` before changing behavior.

## Workflow

1. Classify the request as one of: new bridge, behavior fix, security hardening, session/context fix, streaming/output fix, or documentation.
2. Inspect the current implementation before editing. Search for supported message types, commands, environment variables, and session state handling.
3. Prefer Feishu long connection unless the user explicitly needs HTTP callbacks. Do not require a public URL for long connection mode.
4. Keep one Codex conversation per Feishu chat by binding `session_key -> codex_session_id` and resuming that session on later prompts.
5. If the user wants to jump back to an older Codex context, read local history from `CODEX_HOME/session_index.jsonl`, expose browse/search/use commands, and sync the chat workdir from session metadata when available.
6. Stream replies by sending an immediate status message, then editing the same Feishu message in place. Fall back to rotated anchor messages only when edit limits are reached.
7. Serialize work per chat. Keep at most one running Codex process per chat, queue later jobs, and optionally merge nearby text messages into one prompt.
8. Harden access before enabling broad usage. Prefer private chat only, chat/user allowlists, rate limits, optional command tokens, and disabled raw command passthrough.
9. After behavior changes, update docs and `.env.example` so runtime behavior and operator docs stay aligned.

## Implementation Defaults

- Treat plain text as `/codex <prompt>`.
- Use `codex exec --json` for normal prompts.
- When a prompt may begin with `-`, insert `--` before the prompt so Codex does not parse it as CLI flags.
- Support `text`, `post`, and forwarded/merged messages when the bridge needs to read forwarded content.
- Persist session bindings to disk so bridge restarts do not drop context.
- Read local Codex history from `CODEX_HOME` when implementing session browsing or switching.
- Expose operator commands such as `/status`, `/session`, `/session list`, `/session search`, `/session use`, `/reset`, `/stop`, `/whoami`, and `/security`.

## Verification

- Start the bridge locally and confirm the Feishu long connection is established.
- Send `/whoami` and `/security` to confirm ACLs.
- Send `/status` and `/session` to confirm queue state and bound `codex_session_id`.
- Send `/session list` or `/session search <query>` and verify local history is readable from `CODEX_HOME`.
- Send `/session use <index|id>` and verify both the bound session id and chat workdir update as expected.
- Send two quick consecutive messages and verify merge-window and queue behavior match the intended UX.
- If streaming changed, confirm the user sees one continuously edited message rather than many separate chunks.

## Read References As Needed

- Read `references/bridge-playbook.md` for config keys, operator commands, recommended defaults, and troubleshooting patterns seen in real usage.
