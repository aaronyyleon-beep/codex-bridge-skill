# OpenClaw Installer

这个目录提供一个适合在 macOS 上使用的 OpenClaw 一键安装器。

## 目标

- 从零安装 OpenClaw CLI
- 初始化 `~/.openclaw/openclaw.json`
- 注册本地 Gateway daemon
- 在安装后自动打开飞书或企业微信的本地绑定配置页
- 给用户清晰解释后续 pairing / bot 配置步骤

## 安全边界

- 已移除 LLM 自动诊断和自动执行修复命令逻辑
- 已移除任何硬编码 API Key
- 安装时必须通过 `ZAI_API_KEY` 或 `--api-key` 提供密钥
- 当某一步安装失败时，会优先尝试调用本机 `codex` 做修复和继续部署
- 这是 `macOS-only` 脚本，会修改本机 Node/npm/OpenClaw 环境，并可能注册 `launchd` daemon

## 用法

```bash
ZAI_API_KEY=xxx python3 install_openclaw.py
```

或：

```bash
python3 install_openclaw.py --api-key xxx
```

可选参数：

- `--non-interactive` / `-y`
- `--skip-skills`
- `--no-browser`
- `--no-codex-rescue`
- `--channel feishu|wecom`

## 运行前提

- 如果客户机器上已经有本机 `codex`，那么即使还没有 Node.js / OpenClaw，安装器也可以在失败时让 Codex 兜底修复与继续部署
- 如果客户机器上连 `python3` 都没有，这个 `.py` 本身无法直接启动；这种情况下仍然需要：
  - 先让本机 Codex 通过终端命令装好 `python3`
  - 或后续把这个安装器再打包成 `.app` / 单文件可执行程序

## IM 场景建议

如果用户是在飞书或企业微信里通过 Codex 请求“安装 OpenClaw”，建议直接让 Codex：

1. 说明即将运行这个 Python 安装器
2. 提醒用户可能需要在 macOS “隐私与安全性”中点击“仍要打开”或“允许”
3. 运行 `python3 install_openclaw.py`
4. 告诉用户本地将自动弹出渠道配置页
5. 根据用户选择继续解释飞书或企业微信的 bot 配置与 pairing 步骤

## 文件说明

- [`install_openclaw.py`](/Users/aaron/codex-bridge/assets/openclaw-installer/install_openclaw.py): 当前主入口
- [`install-openclaw.sh`](/Users/aaron/codex-bridge/assets/openclaw-installer/install-openclaw.sh): 旧版 shell 入口，暂时保留作参考
