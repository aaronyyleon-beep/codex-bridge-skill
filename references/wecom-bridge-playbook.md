# Codex WeCom Bridge Playbook

## Scope

Use this reference when building or modifying a WeCom-to-Codex bridge.

Bundled reference implementation inside this repository:

- `assets/wecom-codex-bridge/bridge.py`
- `assets/wecom-codex-bridge/README.md`
- `assets/wecom-codex-bridge/.env.example`

## Preferred Architecture

- Use WeCom callback mode for inbound messages
- Return `success` quickly and send the real result later through主动消息 API
- Keep one logical Codex conversation per WeCom user by persisting `userid -> codex_session_id`
- Run one Codex job per user at a time and queue later requests
- Support `/session` browsing from local `CODEX_HOME`

## Minimal Config

```dotenv
WECOM_CORP_ID=ww_xxx
WECOM_AGENT_ID=1000002
WECOM_CORP_SECRET=xxx
WECOM_TOKEN=token
WECOM_ENCODING_AES_KEY=43_chars

WECOM_BIND_HOST=0.0.0.0
WECOM_BIND_PORT=8080
WECOM_CALLBACK_PATH=/wecom/callback

CODEX_BIN=codex
CODEX_DEFAULT_CWD=/Users/linxiaoyi
CODEX_HOME=/Users/linxiaoyi/.codex
SESSION_STATE_FILE=.wecom_session_map.json

ALLOWED_USER_IDS=zhangsan,lisi
COMMAND_TOKEN=
RATE_LIMIT_PER_MINUTE=20
LOG_LEVEL=INFO
```

## Operator Commands

- `/codex <prompt>`: run a normal prompt and continue the current user session
- `/status`: inspect workdir, queue state, and bound `codex_session_id`
- `/session`: show recent local Codex sessions from `CODEX_HOME`
- `/session current`: show the currently bound `codex_session_id`
- `/session list [n]`: show recent local Codex sessions
- `/session use <index|id>`: bind the user to a local Codex session and sync workdir if possible
- `/new`: clear the current binding, queue, and start fresh
- `/setwd <path>`: change the user workdir
- `/stop`: stop the active job and clear queued work
- `/help`: show command help

## Build Or Fix Checklist

1. Confirm callback URL verification works with GET.
2. Confirm POST signature verification and AES decryption work.
3. Confirm the bridge returns `success` within WeCom's timeout window.
4. Confirm proactive message sending works using `message/send`.
5. Confirm `userid -> codex_session_id` persistence survives restarts.
6. Confirm `/session use <n|id>` syncs `workdir` from local session metadata when available.
7. Confirm queue behavior is per user, not global.
8. Confirm allowlist, token gate, and rate limit work as intended.

## Common Failure Modes

### URL verification fails

Check `WECOM_TOKEN`, `WECOM_ENCODING_AES_KEY`, and `WECOM_CORP_ID` first. Any one of them being wrong breaks the handshake.

### POST decrypt fails

Most failures come from:

- wrong `EncodingAESKey`
- wrong `msg_signature` calculation
- wrong `CorpID` validation
- callback body not being raw XML

### User receives no result

Check both sides:

- callback handler returned `success` but background task never started
- `gettoken` or `message/send` failed
- user was not in app visible range

### Output is too long

Split large Codex output into multiple主动消息 blocks instead of sending one very large text.

### Session continuity breaks

Check `SESSION_STATE_FILE` and confirm the bridge updates it after `thread.started`.

## Verification Routine

1. Start `python3 bridge.py`.
2. Confirm logs show `wecom callback server started`.
3. In WeCom, send `/status`.
4. Send `/session` and confirm local history is readable from `CODEX_HOME`.
5. Send `/session use <index|id>` and verify both `codex_session_id` and `workdir` update as expected.
6. Send `/new` and confirm a fresh context starts.
7. Send two prompts quickly and verify queue behavior.
