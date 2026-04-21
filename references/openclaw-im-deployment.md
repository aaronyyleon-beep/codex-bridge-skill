# OpenClaw IM Deployment Notes

这份说明用于桥接机器人在飞书或企业微信中响应用户的 OpenClaw 安装与绑定请求。

## 目标体验

用户在 IM 中用自然语言说：

- “帮我安装 openclaw”
- “帮我配置飞书 bot”
- “帮我配置企业微信 bot”

Codex 应该能把流程拆成 3 段：

1. 安装 OpenClaw
2. 打开本地绑定配置页
3. 根据用户选择继续解释飞书或企业微信后台配置

## 推荐执行方式

### 安装 OpenClaw

优先使用仓库内脚本：

- `assets/openclaw-installer/install_openclaw.py`

运行前提醒用户：

- 这是 `macOS-only`
- 需要 `ZAI_API_KEY`
- 可能触发 macOS 安全校验
- 如果机器上已经有本机 `codex`，安装器可以在失败时让 Codex 继续兜底修复和部署

如果 macOS 阻止执行，优先这样引导：

1. 打开“系统设置” -> “隐私与安全性”
2. 找到底部的“仍要打开”或“允许”
3. 回到终端重新运行安装命令

## 绑定配置页

安装脚本内置两个本地向导页：

- 飞书: `http://127.0.0.1:18899/`
- 企业微信: `http://127.0.0.1:18900/`

脚本会自动尝试打开浏览器；如果用户说没有弹出，直接告诉他手动打开本地地址即可。

## Codex 兜底策略

当用户已经能在 IM 中和本机 Codex 对话时，推荐执行顺序是：

1. 先让 Codex 检查 `python3` / `node` / `npm` / `openclaw`
2. 若缺失，则由 Codex 先安装这些依赖
3. 再运行 `install_openclaw.py`
4. 如果安装器中的某一步失败，安装器会再次尝试调用 `codex` 做本机修复
5. 成功后自动打开本地 bot 配置页

这意味着在“机器上有 Codex，但还没有 Node.js / OpenClaw”的崭新环境里，仍然可以走 Codex 兜底安装链路。

## 飞书配置话术

用户配置飞书时，Codex 应优先说明：

1. 去飞书开发者后台创建企业自建应用
2. 获取 `App ID` 和 `App Secret`
3. 在本地配置页填入
4. 在飞书后台开启 Bot 能力
5. 添加事件订阅 `im.message.receive_v1`
6. 发布应用版本
7. 在飞书里先给 Bot 发一条消息，拿到 Pairing code
8. 在本机执行 `openclaw pairing approve feishu <CODE>`

## 企业微信配置话术

用户配置企业微信时，Codex 应优先说明：

1. 在企业微信后台创建 AI 机器人
2. 获取 `Bot ID` 和 `Secret`
3. 在本地配置页填入
4. 选择 `dmPolicy`
5. 若选 `allowlist`，后续需要在 `openclaw.json` 中补 `channels.wecom.allowFrom`
6. 若选 `pairing`，先在企微里和机器人对话拿到 Pairing code
7. 在本机执行 `openclaw pairing approve wecom <CODE>`

## 建议回答风格

- 先告诉用户当前在哪一步
- 明确接下来会弹出本地页面，而不是远程网页
- 如果浏览器没弹出，给出明确本地地址
- 如果用户问“为什么 bot 还不能说话”，优先排查：
  - 应用是否发布
  - 权限是否开启
  - pairing 是否完成
  - Gateway 是否运行
