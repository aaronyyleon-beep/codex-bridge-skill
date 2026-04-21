# Codex WeCom Bridge

通过企业微信自建应用回调，把用户消息转给本地 `codex` CLI，并把结果主动发送回企业微信。

## 1. 功能

- 使用企业微信“接收消息”回调模式接收文本消息
- 默认把普通文本当作 `codex exec` prompt 执行
- 每个用户独立维护 `userid -> codex_session_id`
- 每个用户独立维护工作目录，可用 `/setwd`
- 单用户串行执行，后续请求自动排队
- 支持浏览和切换本地 Codex 历史会话
- 企业微信回调 5 秒内快速返回 `success`，结果异步主动推送

## 2. 依赖

- Python 3.9+
- 本机可用 `codex` CLI
- 安装 AES 依赖：

```bash
python3 -m pip install --user pycryptodome
```

## 3. 配置

```bash
cd /Users/linxiaoyi/codex-wecom-bridge
cp .env.example .env
# 编辑 .env，填入企业微信应用配置
```

必填项：

- `WECOM_CORP_ID`
- `WECOM_AGENT_ID`
- `WECOM_CORP_SECRET`
- `WECOM_TOKEN`
- `WECOM_ENCODING_AES_KEY`

## 4. 企业微信后台配置

在企业微信自建应用中：

- 开启“接收消息”
- URL 指向你的回调地址，例如 `https://your-domain.example/wecom/callback`
- Token 与 EncodingAESKey 填为 `.env` 中相同值
- 消息加解密方式选择安全模式

## 5. 启动

```bash
cd /Users/linxiaoyi/codex-wecom-bridge
python3 bridge.py
```

看到日志 `wecom callback server started` 即表示服务已启动。

## 6. 可用指令

- `/codex <prompt>`: 执行 `codex exec --json`
- `/status`: 查看当前状态
- `/session`: 查看最近的本地 Codex 历史会话
- `/session current`: 查看当前绑定的 `codex_session_id`
- `/session list [n]`: 列出最近 `n` 条本地 Codex 历史会话
- `/session use <n|id>`: 切到某个本地历史 `codex_session_id`
- `/new`: 新建一轮对话
- `/setwd <path>`: 设置当前工作目录
- `/stop`: 终止当前任务并清空队列
- `/help`: 查看帮助

不带前缀的文本默认按 `/codex <text>` 处理。

## 7. 说明

- 企业微信回调要求快速返回，所以桥接会先回 `success`，再异步主动发消息给用户。
- 当前只处理文本消息；图片、文件、语音等类型默认忽略。
- 默认不做被动回复加密返回，而是通过主动发消息 API 回写结果。
- 本地 Codex 数据目录通过 `CODEX_HOME` 指定，默认 `~/.codex`。
- 会话持久化文件通过 `SESSION_STATE_FILE` 指定，默认 `.wecom_session_map.json`。

## 8. 安全建议

- 填 `ALLOWED_USER_IDS`，只允许白名单用户使用
- 设置 `COMMAND_TOKEN`，要求用户每次命令携带口令
- 配置反向代理只暴露固定回调路径
- 为回调地址启用 HTTPS
