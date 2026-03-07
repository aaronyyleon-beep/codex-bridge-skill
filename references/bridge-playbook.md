# Feishu Codex Bridge Playbook

## Scope

Use this reference when building a new Feishu-to-Codex bridge or modifying the existing implementation on this machine.

Default local project path:

- `/Users/linxiaoyi/feishu-codex-bridge`

Key files to inspect first:

- `README.md`
- `.env.example`
- `bridge.py`

## Preferred Architecture

- Prefer Feishu long connection over webhook callbacks unless the user explicitly needs HTTP ingress.
- Treat ordinary user text as a Codex prompt.
- Run one Codex job per chat at a time and queue the rest.
- Keep one logical Codex conversation per Feishu chat by persisting `session_key -> codex_session_id`.
- Reply with a fast status update, then stream by editing the same Feishu message in place.

## Existing Implementation Notes

Current bridge behavior in `/Users/linxiaoyi/feishu-codex-bridge/bridge.py` includes:

- Supported inbound message types: `text`, `post`, `merge_forward`
- Local Codex history source: `CODEX_HOME/session_index.jsonl` plus session files under `CODEX_HOME/sessions/`
- Default merge window: `MERGE_WINDOW_SEC=0.3`
- Session persistence file: `SESSION_STATE_FILE=.feishu_session_map.json`
- In-place streaming enabled by default: `STREAM_EDIT_IN_PLACE=1`
- Feishu Markdown output enabled by default: `STREAM_USE_MARKDOWN=1`
- Max edits before rotating anchor message: `STREAM_MAX_UPDATES_PER_MESSAGE=18`
- Immediate acknowledgement text: `STATUS_RECEIVED_TEXT=⏳ 已收到，正在思考...`
- Private chat restriction enabled by default: `REQUIRE_P2P=1`
- Raw `/cmd` passthrough disabled by default: `ENABLE_RAW_CMD=0`
- Rate limiting enabled by default: `RATE_LIMIT_PER_MINUTE=20`

## Minimal Config

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

CODEX_BIN=codex
CODEX_DEFAULT_CWD=/Users/linxiaoyi
CODEX_HOME=/Users/linxiaoyi/.codex
SESSION_STATE_FILE=.feishu_session_map.json

MERGE_WINDOW_SEC=0.3
STREAM_EDIT_IN_PLACE=1
STREAM_USE_MARKDOWN=1
STREAM_UPDATE_INTERVAL_SEC=0.25
STREAM_MAX_UPDATES_PER_MESSAGE=18

REQUIRE_P2P=1
ALLOWED_CHAT_IDS=oc_xxx
ENABLE_RAW_CMD=0
RATE_LIMIT_PER_MINUTE=20
LOG_LEVEL=INFO
```

## Operator Commands

- `/codex <prompt>`: run a normal prompt and auto-resume the current chat session
- `/cmd <args>`: run raw Codex arguments if policy allows it
- `/setwd <path>`: set the current chat workdir
- `/status`: inspect running state, queue depth, merge buffer, workdir, and `codex_session_id`
- `/session`: show the bound `codex_session_id`
- `/session list [n]`: show recent local Codex sessions from `CODEX_HOME`
- `/session search <query>`: search local Codex sessions by title or id
- `/session use <index|id>`: bind the chat to a local Codex session and sync workdir when possible
- `/session set <id>`: manually bind the chat to a Codex session
- `/session clear`: clear the bound session
- `/reset`: clear remembered context and queued work when idle
- `/stop`: stop the active job and clear queued or merged work
- `/whoami`: show `chat_id` and sender IDs for ACL setup
- `/security`: show active security settings
- `/help`: show command help

## Build Or Fix Checklist

1. Confirm whether the user wants long connection or webhook mode.
2. Inspect message parsing first. Forwarded content, post content, and mention stripping are common failure points.
3. Inspect prompt construction next. Normal prompt execution should use `codex exec --json` and insert `--` before the prompt payload.
4. Inspect session continuity. Resume the bound `codex_session_id` when possible and persist updates after `thread.started`.
5. If the request involves older Codex conversations, inspect `CODEX_HOME/session_index.jsonl` and session metadata under `CODEX_HOME/sessions/`.
6. When switching to a historical session, sync the Feishu chat workdir from the saved session `cwd` if that directory still exists.
7. Inspect output streaming. Prefer editing the same message in place; only rotate when Feishu edit caps force it.
8. Inspect queue and merge behavior per chat. Do not run multiple Codex processes concurrently for the same chat.
9. Inspect ACL and abuse controls before enabling usage outside a trusted private chat.
10. Update docs and `.env.example` whenever defaults or commands change.

## Common Failure Modes

### Webhook confusion

If the bridge uses long connection, it does not need a public callback URL. Do not send the user toward webhook setup unless the design explicitly requires HTTP callbacks.

### Message starts with `-`

If the first character of the prompt is `-`, Codex CLI can misread it as a flag unless the bridge inserts `--` before the prompt. Fix prompt argv construction before debugging anything else.

### User sees `started` or `finished`

Do not echo raw Codex lifecycle events to Feishu. Parse JSON event lines and prefer visible content deltas or completed text instead.

### Output is not truly streaming

Check both sides:

- Codex may emit a single final block instead of many deltas.
- The bridge may be buffering too aggressively or sending separate chunks instead of editing in place.

If true token streaming is unavailable, pseudo-stream the final text in small edits so the UX still feels continuous.

### Output jumps in chunks

This usually means one of these:

- In-place edits are disabled
- Update intervals are too conservative
- Feishu edit count caps forced anchor-message rotation

Inspect `STREAM_EDIT_IN_PLACE`, `STREAM_UPDATE_INTERVAL_SEC`, and `STREAM_MAX_UPDATES_PER_MESSAGE`.

### Forwarded messages only partly parsed

Forwarded Feishu messages may arrive as `merge_forward`. Parse recursively and extract text from nested content nodes instead of assuming a flat `text` field.

### Consecutive messages become separate contexts

Fix this in two layers:

- Merge nearby messages within a short window if the UX expects them to be one thought
- Persist and resume the same `codex_session_id` for later messages in the same Feishu chat

### Session continuity breaks after restart

Persist `session_key -> codex_session_id` to disk and load it on startup. Provide `/session` and `/session set <id>` for inspection and manual repair.

### Historical session switch does not show old conversations

Check these first:

- `CODEX_HOME` points at the correct Codex home directory
- `session_index.jsonl` exists and is readable
- The requested session id still exists in the index or in `CODEX_HOME/sessions/`

### Historical session switch keeps the wrong workdir

When binding a chat to a saved Codex session, read the session's `cwd` from session metadata and sync the chat workdir if the directory still exists. If the saved path no longer exists, keep the current workdir and surface that mismatch to the operator.

### Numeric `/session use` fails

Numeric selection depends on the last `/session list` or `/session search` result cached for that chat. If there is no cached result, ask the operator to list or search first.

### Unauthorized control risk

Lock the bridge down before wider rollout:

- `REQUIRE_P2P=1`
- `ALLOWED_CHAT_IDS` and/or `ALLOWED_OPEN_IDS`
- `COMMAND_TOKEN` if an extra shared secret is needed
- `ENABLE_RAW_CMD=0`
- `RATE_LIMIT_PER_MINUTE` tuned to the expected operator load

## Verification Routine

1. Start `python3 bridge.py` in `/Users/linxiaoyi/feishu-codex-bridge`.
2. Confirm logs show the long connection started successfully.
3. In Feishu, send `/whoami`, then update ACL settings if required.
4. Send `/status` and verify the session map file, queue state, workdir, and local session index path.
5. Send `/session list` or `/session search <query>` and confirm history entries are returned.
6. Send `/session use <index|id>` and verify both `codex_session_id` and workdir update as expected.
7. Send a normal prompt and watch whether the same message gets edited over time.
8. Send two messages in quick succession and confirm they merge or queue according to the configured UX.
9. Restart the bridge and verify `/session` still reports the prior binding when persistence is enabled.
