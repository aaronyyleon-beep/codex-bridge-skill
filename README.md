# Codex Bridge Skills

Codex bridge repository for building, debugging, and operating chat bots that control a local Codex CLI.

## What This Repository Covers

- Feishu long connection setup and event flow
- WeCom callback setup and encrypted message handling
- Plain text, post, and forwarded message parsing
- Streamed replies through in-place Feishu message edits
- Per-chat session continuity with `session_key -> codex_session_id`
- Switching a Feishu chat back to saved local Codex sessions
- Browsing saved sessions with `/session`, checking the active binding with `/session current`, and starting fresh with `/new`
- Merge windows, per-chat queues, and workdir synchronization
- ACLs, rate limits, and raw-command hardening

## Repository Layout

- `SKILL.md`: main skill instructions and trigger description
- `agents/openai.yaml`: UI metadata for skill pickers
- `references/bridge-playbook.md`: operational details, commands, config, and troubleshooting
- `references/wecom-bridge-playbook.md`: WeCom callback bridge playbook
- `references/openclaw-im-deployment.md`: IM-driven OpenClaw install/binding guidance
- `assets/feishu-codex-bridge/`: bundled Feishu runnable reference implementation
- `assets/wecom-codex-bridge/`: bundled WeCom runnable reference implementation
- `assets/openclaw-installer/`: bundled Python OpenClaw installer and local channel setup wizard

## Install Manually

This repository currently keeps the original installable skill entrypoint as `feishu-codex-bridge`, and also includes a bundled `codex-wecom bridge` reference implementation.

```bash
git clone https://github.com/xllinbupt/feishu-codex-skill.git
mkdir -p ~/.codex/skills/feishu-codex-bridge
cp -R feishu-codex-skill/SKILL.md feishu-codex-skill/agents feishu-codex-skill/references feishu-codex-skill/assets ~/.codex/skills/feishu-codex-bridge/
```

After that, invoke it in Codex with prompts such as:

```text
$feishu-codex-bridge Help me debug why Feishu replies are not streaming.
$feishu-codex-bridge Add support for switching a Feishu chat to a saved Codex session.
```

## Bundled Code

This repository includes runnable reference implementations under:

- `assets/feishu-codex-bridge/`: Feishu long-connection bridge
- `assets/wecom-codex-bridge/`: WeCom callback bridge
- `assets/openclaw-installer/`: Python OpenClaw installer for macOS, including Codex fallback repair and Feishu/WeCom local config pages

Sensitive runtime files are intentionally not included, such as `.env`, `.feishu_session_map.json`, or generated tokens.

## Notes

- The skill content currently points at the author's local bridge project path as the default implementation example.
- If your bridge project lives somewhere else, update those paths in `SKILL.md` and `references/bridge-playbook.md`.
