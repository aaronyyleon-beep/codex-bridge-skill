#!/usr/bin/env bash
# ==============================================================
#  OpenClaw 一键安装脚本 (macOS)
#  安全版：移除 LLM 自动修复与硬编码密钥
#
#  用法:
#    bash install-openclaw.sh
#    bash install-openclaw.sh --non-interactive
#    bash install-openclaw.sh -y --skip-skills
#    ZAI_API_KEY=xxx bash install-openclaw.sh
#    bash install-openclaw.sh --api-key xxx
# ==============================================================

set -euo pipefail
IFS=$'\n\t'

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[ OK ]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
err()     { echo -e "${RED}[ERR ]${RESET}  $*"; }
step()    { echo -e "\n${BOLD}━━━  $*  ━━━${RESET}"; }
die()     { err "$*"; exit 1; }

_oc_lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

SKIP_SKILLS=false
NON_INTERACTIVE=false
NO_BROWSER=false
API_KEY_CLI=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-skills) SKIP_SKILLS=true; shift ;;
    --non-interactive|-y) NON_INTERACTIVE=true; shift ;;
    --no-browser) NO_BROWSER=true; shift ;;
    --api-key)
      [[ -n "${2:-}" ]] || die "--api-key 需要紧跟密钥参数"
      API_KEY_CLI="$2"
      shift 2
      ;;
    *) die "未知参数: $1" ;;
  esac
done

if [[ -n "${API_KEY_CLI:-}" ]]; then
  ZAI_API_KEY="$API_KEY_CLI"
fi

OC_HOME="$HOME/.openclaw"
OC_CONFIG="$OC_HOME/openclaw.json"
OC_ENV="$OC_HOME/.env"
OC_WORKSPACE="$OC_HOME/workspace"
OC_SKILLS_DIR="$OC_WORKSPACE/skills"
OC_BOOTSTRAP="$OC_WORKSPACE/bootstrap.md"
_CURRENT_STEP=""

on_error() {
  local exit_code=$?
  local failed_line=${BASH_LINENO[0]:-?}
  local failed_cmd="${BASH_COMMAND:-unknown}"
  echo ""
  err "$_CURRENT_STEP 第 $failed_line 行失败: $failed_cmd (退出码 $exit_code)"
  echo ""
  warn "脚本已停止。请根据上面的失败步骤修复后重新运行。"
  exit "$exit_code"
}

trap 'on_error' ERR

print_macos_security_note() {
  echo ""
  warn "如果 macOS 阻止脚本或 pkg 运行，请按下面操作解除安全限制："
  echo "  1. 打开“系统设置” -> “隐私与安全性”"
  echo "  2. 在底部找到“已阻止使用 install-openclaw.sh / Node.pkg”"
  echo "  3. 点击“仍要打开”或“允许”"
  echo "  4. 回到终端重新运行本脚本"
  echo ""
}

open_security_settings() {
  if [[ "$NO_BROWSER" == true ]]; then
    return
  fi
  if command -v open >/dev/null 2>&1; then
    open "x-apple.systempreferences:com.apple.preference.security?Privacy" >/dev/null 2>&1 || true
  fi
}

clear
echo -e "${BOLD}${CYAN}"
cat << 'EOF'
  ╔══════════════════════════════════════════════╗
  ║   OpenClaw 一键安装脚本  (macOS)             ║
  ║   Safe Installer · no LLM fallback          ║
  ╚══════════════════════════════════════════════╝
EOF
echo -e "${RESET}"
echo -e "  本脚本将依次完成：\n"
echo -e "  ${DIM}Step 1${RESET}  检测 macOS 和 Node.js 环境"
echo -e "  ${DIM}Step 2${RESET}  安装 OpenClaw CLI"
echo -e "  ${DIM}Step 3${RESET}  初始化配置文件"
echo -e "  ${DIM}Step 4${RESET}  非交互 onboard + 注册后台守护进程"
echo -e "  ${DIM}Step 5${RESET}  清理 bootstrap.md"
echo -e "  ${DIM}Step 6${RESET}  打开飞书 / 企业微信绑定配置页"
echo -e "  ${DIM}Step 7${RESET}  预装精选 Skills"
echo -e "  ${DIM}Step 8${RESET}  验证并输出安装报告\n"
if [[ "$NON_INTERACTIVE" != true ]]; then
  read -rp "  按 Enter 开始，Ctrl+C 取消 ... "
fi

_CURRENT_STEP="Step 1 · 环境检测"
step "$_CURRENT_STEP"

install_clt() {
  if [[ "$NON_INTERACTIVE" == true ]]; then
    local placeholder="/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress"
    touch "$placeholder"
    local clt_label
    clt_label=$(softwareupdate -l 2>/dev/null \
      | grep -B 1 -E 'Command Line Tools' \
      | awk -F'\\*' '/^\*/{print $2}' \
      | sed 's/^ Label: //' | head -1) || true
    if [[ -n "$clt_label" ]]; then
      info "正在安装: $clt_label（可能需要几分钟）..."
      softwareupdate -i "$clt_label" --verbose 2>&1 || warn "softwareupdate 安装未成功"
    fi
    rm -f "$placeholder"
  else
    xcode-select --install 2>/dev/null || true
    open_security_settings
    print_macos_security_note
    info "请在弹出窗口中完成安装，然后回到终端按 Enter 继续..."
    read -rp ""
  fi
}

if ! command -v python3 &>/dev/null; then
  warn "未检测到 python3，尝试安装 Xcode Command Line Tools..."
  install_clt
  command -v python3 &>/dev/null || die "python3 仍不可用。请先安装 Xcode Command Line Tools"
fi
ok "python3 $(python3 --version 2>&1 | awk '{print $2}')"

GIT_AVAILABLE=true
if ! command -v git &>/dev/null; then
  warn "未检测到 git，尝试安装 Xcode Command Line Tools..."
  install_clt
  if ! command -v git &>/dev/null; then
    warn "git 仍不可用，后续 Skills 安装将跳过"
    GIT_AVAILABLE=false
  fi
fi
if [[ "$GIT_AVAILABLE" == true ]]; then
  ok "git $(git --version | awk '{print $3}')"
fi

NODE_MIN_MAJOR=22
NODE_MIN_MINOR=14

install_node() {
  if command -v nvm &>/dev/null; then
    nvm install 24 && nvm use 24 && nvm alias default 24
  elif command -v brew &>/dev/null; then
    brew install node@24
  else
    local node_pkg="/tmp/node-latest.pkg"
    local pkg_name
    pkg_name=$(curl -sfL "https://nodejs.org/dist/latest-v22.x/" \
      | grep -oE 'node-v[0-9]+\.[0-9]+\.[0-9]+\.pkg' | head -1) || true
    [[ -n "$pkg_name" ]] || pkg_name="node-v22.15.0.pkg"
    local node_url="https://nodejs.org/dist/latest-v22.x/$pkg_name"
    info "下载 $node_url ..."
    curl -fL --connect-timeout 30 --max-time 600 --progress-bar -o "$node_pkg" "$node_url"
    info "安装 Node.js..."
    if sudo -n true 2>/dev/null; then
      sudo installer -pkg "$node_pkg" -target /
      rm -f "$node_pkg"
      export PATH="/usr/local/bin:$PATH"
    elif [[ "$NON_INTERACTIVE" == true ]]; then
      warn "非交互模式且无免密 sudo，改用 nvm 安装到用户空间"
      rm -f "$node_pkg"
      export NVM_DIR="$HOME/.nvm"
      local nvm_tmp
      nvm_tmp=$(mktemp)
      curl -fsSL --connect-timeout 30 --max-time 120 -o "$nvm_tmp" https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh
      bash "$nvm_tmp"
      rm -f "$nvm_tmp"
      # shellcheck source=/dev/null
      [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
      nvm install 22 && nvm use 22 && nvm alias default 22
    else
      open_security_settings
      print_macos_security_note
      info "可能需要输入管理员密码以安装 Node.js ..."
      sudo installer -pkg "$node_pkg" -target /
      rm -f "$node_pkg"
      export PATH="/usr/local/bin:$PATH"
    fi
  fi
  ok "Node.js $(node --version) 安装完成"
}

if ! command -v node &>/dev/null; then
  warn "未检测到 Node.js，开始安装..."
  install_node
else
  NODE_VER=$(node --version | tr -d 'v')
  NODE_MAJOR=$(echo "$NODE_VER" | cut -d. -f1)
  NODE_MINOR=$(echo "$NODE_VER" | cut -d. -f2)
  if [[ $NODE_MAJOR -lt $NODE_MIN_MAJOR ]] || [[ $NODE_MAJOR -eq $NODE_MIN_MAJOR && $NODE_MINOR -lt $NODE_MIN_MINOR ]]; then
    warn "Node.js $NODE_VER 版本过低（要求 22.14+），尝试升级..."
    install_node
  else
    ok "Node.js v$NODE_VER"
  fi
fi

command -v npm &>/dev/null || die "npm 不可用，请检查 Node.js 安装"
ok "npm $(npm --version)"
curl -sf --max-time 5 https://registry.npmjs.org/ >/dev/null || die "无法访问 npmjs.org，请检查网络"
ok "网络连通性正常"

_CURRENT_STEP="Step 2 · 安装 OpenClaw"
step "$_CURRENT_STEP"

OC_PINNED_VERSION="2026.3.28"
_npm_global_prefix=$(npm config get prefix 2>/dev/null | head -1 || echo "/usr/local")
_USE_SUDO=false
if [[ ! -w "$_npm_global_prefix/lib" ]] 2>/dev/null; then
  if sudo -n true 2>/dev/null; then
    _USE_SUDO=true
  elif [[ "$NON_INTERACTIVE" == true ]]; then
    warn "npm 全局目录不可写，切换到用户空间目录 $HOME/.npm-global"
    mkdir -p "$HOME/.npm-global"
    npm config set prefix "$HOME/.npm-global"
    export PATH="$HOME/.npm-global/bin:$PATH"
    case "${SHELL:-/bin/zsh}" in
      */bash) _rc_file="$HOME/.bash_profile" ;;
      *)      _rc_file="$HOME/.zshrc" ;;
    esac
    if ! grep -q 'npm-global/bin' "$_rc_file" 2>/dev/null; then
      echo '' >> "$_rc_file"
      echo '# Added by OpenClaw installer — npm global bin' >> "$_rc_file"
      echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> "$_rc_file"
    fi
  else
    _USE_SUDO=true
  fi
fi

_npm_run() {
  if [[ "$_USE_SUDO" == true ]]; then
    sudo npm "$@"
  else
    npm "$@"
  fi
}

if command -v openclaw &>/dev/null; then
  CURRENT_VER=$(openclaw --version | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+' || echo "")
  if [[ "$CURRENT_VER" != "$OC_PINNED_VERSION" ]]; then
    info "当前版本 $CURRENT_VER，切换到指定版本 $OC_PINNED_VERSION..."
    _npm_run i -g "openclaw@$OC_PINNED_VERSION"
  fi
else
  info "安装 openclaw@$OC_PINNED_VERSION ..."
  _npm_run i -g "openclaw@$OC_PINNED_VERSION"
fi

if ! command -v openclaw &>/dev/null; then
  _npm_bin="$(npm config get prefix 2>/dev/null | head -1)/bin"
  export PATH="$_npm_bin:$PATH"
fi
command -v openclaw &>/dev/null || die "openclaw 命令未找到，请检查 npm 全局路径是否在 PATH 中"
ok "openclaw $(openclaw --version || echo $OC_PINNED_VERSION)"

_CURRENT_STEP="Step 3 · 初始化配置文件"
step "$_CURRENT_STEP"

mkdir -p "$OC_HOME" "$OC_WORKSPACE" "$OC_SKILLS_DIR"

if [[ -z "${ZAI_API_KEY:-}" ]]; then
  die "未配置 ZAI API Key。请通过环境变量 ZAI_API_KEY 或 --api-key 提供"
fi
export ZAI_API_KEY

if [[ -f "$OC_CONFIG" ]] && openclaw config validate &>/dev/null; then
  ok "配置文件已存在且校验通过，跳过重新生成"
  export OC_CONFIG
else
  if [[ -f "$OC_ENV" ]] && [[ ! -w "$OC_ENV" ]]; then
    chmod u+w "$OC_ENV" 2>/dev/null || rm -f "$OC_ENV" 2>/dev/null || sudo rm -f "$OC_ENV"
  fi
  cat > "$OC_ENV" << EOF
# OpenClaw 环境变量
# 由安装脚本生成于 $(date)
# ZAI API Key 写入 openclaw.json（models.providers.zai.apiKey）
EOF
  chmod 600 "$OC_ENV"
  ok ".env 已生成: $OC_ENV"

  if [[ -f "$OC_CONFIG" ]]; then
    BACKUP="$OC_CONFIG.bak.$(date +%Y%m%d_%H%M%S)"
    cp "$OC_CONFIG" "$BACKUP"
    warn "已备份旧配置到 $BACKUP"
  fi

  export OC_CONFIG
  python3 << 'INSTALL_CONFIG_PY'
import json
import os
import pathlib

key = os.environ["ZAI_API_KEY"].strip()
if not key:
    raise SystemExit("ZAI_API_KEY 为空")

cfg = {
    "agents": {
        "defaults": {
            "workspace": "~/.openclaw/workspace",
            "model": {
                "primary": "zai/glm-5",
                "fallbacks": ["zai/glm-4.7"],
            },
            "verboseDefault": "off",
            "models": {
                "zai/glm-5": {"alias": "GLM-5（默认）"},
                "zai/glm-4.7": {"alias": "GLM-4.7（备用）"},
            },
        }
    },
    "models": {
        "mode": "merge",
        "providers": {
            "zai": {
                "baseUrl": "https://api.z.ai/api/paas/v4",
                "apiKey": key,
                "api": "openai-completions",
                "models": [
                    {
                        "id": "glm-5",
                        "name": "GLM-5",
                        "reasoning": True,
                        "input": ["text"],
                        "cost": {"input": 1, "output": 3.2, "cacheRead": 0.2, "cacheWrite": 0},
                        "contextWindow": 202800,
                        "maxTokens": 131100,
                    },
                    {
                        "id": "glm-5-turbo",
                        "name": "GLM-5 Turbo",
                        "reasoning": True,
                        "input": ["text"],
                        "cost": {"input": 1.2, "output": 4, "cacheRead": 0.24, "cacheWrite": 0},
                        "contextWindow": 202800,
                        "maxTokens": 131100,
                    },
                    {
                        "id": "glm-4.7",
                        "name": "GLM-4.7",
                        "reasoning": True,
                        "input": ["text"],
                        "cost": {"input": 0.6, "output": 2.2, "cacheRead": 0.11, "cacheWrite": 0},
                        "contextWindow": 204800,
                        "maxTokens": 131072,
                    },
                ],
            }
        },
    },
    "session": {"dmScope": "per-channel-peer"},
    "gateway": {"port": 18789, "reload": {"mode": "hybrid"}},
}

path = pathlib.Path(os.environ["OC_CONFIG"])
path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
INSTALL_CONFIG_PY

  ok "配置文件已生成: $OC_CONFIG"
  openclaw config validate || die "配置未通过 openclaw config validate"
fi

_CURRENT_STEP="Step 4 · 非交互 onboard"
step "$_CURRENT_STEP"

_oc_plist="$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist"
if [[ -f "$_oc_plist" ]]; then
  ok "launchd daemon 已注册，跳过 onboard"
else
  _GW_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || openssl rand -hex 32)
  if openclaw onboard \
    --non-interactive \
    --accept-risk \
    --mode local \
    --gateway-port 18789 \
    --gateway-bind loopback \
    --gateway-auth token \
    --gateway-token "$_GW_TOKEN" \
    --install-daemon \
    --daemon-runtime node \
    --skip-skills \
    --skip-channels; then
    ok "OpenClaw 初始化完成，launchd daemon 已注册"
  else
    warn "onboard 非交互模式失败，尝试仅安装 daemon..."
    openclaw onboard --non-interactive --accept-risk --install-daemon --gateway-auth token --gateway-token "$_GW_TOKEN" || \
      warn "daemon 注册失败，稍后可手动运行: openclaw onboard --non-interactive --accept-risk --install-daemon"
  fi
fi

_CURRENT_STEP="Step 5 · 清理 bootstrap.md"
step "$_CURRENT_STEP"

if [[ -f "$OC_BOOTSTRAP" ]]; then
  rm -f "$OC_BOOTSTRAP"
  ok "bootstrap.md 已删除"
else
  ok "bootstrap.md 不存在，跳过"
fi

_launch_local_wizard_server() {
  local port="$1"
  local html_file="$2"
  local result_file="$3"
  local done_file="$4"
  local py_code="$5"
  local py_no_browser="False"
  if [[ "$NO_BROWSER" == true ]]; then
    py_no_browser="True"
  fi

  python3 - <<PYEOF &
import http.server, json, os, subprocess, threading, webbrowser

HTML_PATH = "$html_file"
RESULT_PATH = "$result_file"
DONE_PATH = "$done_file"
CONFIG_PATH = os.path.expanduser("~/.openclaw/openclaw.json")
PORT = $port
NO_BROWSER = $py_no_browser
${py_code}
PYEOF
}

setup_feishu() {
  echo ""
  step "飞书渠道配置向导"
  info "安装飞书插件..."
  if openclaw plugins install @openclaw/feishu; then
    ok "飞书插件已安装（@openclaw/feishu）"
  elif openclaw plugins install @larksuite/openclaw-lark; then
    ok "飞书插件已安装（@larksuite/openclaw-lark）"
  else
    warn "飞书插件安装失败，可稍后手动运行:"
    echo "  openclaw plugins install @openclaw/feishu"
    echo "  或: openclaw plugins install @larksuite/openclaw-lark"
    return
  fi

  local port=18899
  local done_file result_file html_file
  done_file=$(mktemp)
  result_file=$(mktemp)
  html_file=$(mktemp /tmp/oc-feishu-XXXXXX.html)

  cat > "$html_file" << 'HTML_EOF'
<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenClaw · 飞书渠道配置</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f5f5f7;padding:24px;">
<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:16px;padding:24px;box-shadow:0 2px 20px rgba(0,0,0,.08);">
<h2 style="margin:0 0 12px;">飞书渠道配置</h2>
<p style="line-height:1.6;color:#555;">请填写飞书应用的 App ID 与 App Secret。保存后会自动写入 <code>~/.openclaw/openclaw.json</code>。</p>
<p style="line-height:1.6;"><a href="https://open.feishu.cn/app" target="_blank">飞书开发者后台</a> · <a href="https://docs.openclaw.ai/channels/feishu" target="_blank">配置教程</a></p>
<div style="display:flex;flex-direction:column;gap:12px;margin-top:20px;">
<input id="app-id" placeholder="App ID" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
<input id="app-secret" placeholder="App Secret" type="password" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
<input id="bot-name" placeholder="Bot 名称（可选）" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
<div id="msg" style="font-size:12px;color:#b42318;"></div>
<button onclick="save()" style="padding:12px;border:none;border-radius:10px;background:#1664FF;color:#fff;cursor:pointer;">保存并继续</button>
<button onclick="skip()" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;background:#fff;cursor:pointer;">跳过，稍后配置</button>
</div></div>
<script>
async function save(){
  const appId=document.getElementById('app-id').value.trim();
  const appSecret=document.getElementById('app-secret').value.trim();
  const botName=document.getElementById('bot-name').value.trim() || 'My AI Assistant';
  if(!appId || !appSecret){ document.getElementById('msg').textContent='请填写 App ID 和 App Secret'; return; }
  const res=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({appId,appSecret,botName})});
  if(res.ok){ document.body.innerHTML='<div style="font-family:sans-serif;padding:24px;max-width:560px;margin:0 auto;"><h2>飞书渠道配置成功</h2><p>接下来请在飞书里先给机器人发一条消息，再在本机执行 <code>openclaw pairing approve feishu &lt;CODE&gt;</code>。</p><button onclick="done()">完成</button></div>'; }
  else { document.getElementById('msg').textContent=await res.text() || '保存失败'; }
}
async function skip(){ await fetch('/skip',{method:'POST'}); window.close(); }
async function done(){ await fetch('/done',{method:'POST'}); window.close(); }
</script></body></html>
HTML_EOF

  _launch_local_wizard_server "$port" "$html_file" "$result_file" "$done_file" '
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/":
            with open(HTML_PATH, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/save":
            try:
                data = json.loads(body)
                app_id = data.get("appId", "").strip()
                app_secret = data.get("appSecret", "").strip()
                bot_name = data.get("botName", "My AI Assistant").strip()

                npm_root = subprocess.check_output(["npm", "root", "-g"], text=True, timeout=30).strip()
                oc_nm = os.path.join(npm_root, "openclaw", "node_modules")
                if not os.path.isdir(os.path.join(oc_nm, "json5")):
                    raise RuntimeError("未找到 openclaw 自带的 json5 模块，请确认已安装 openclaw")

                node_script = r"""
const fs = require("fs");
const JSON5 = require("json5");
const cfgPath = process.argv[1];
const appId = process.argv[2];
const appSecret = process.argv[3];
const botName = process.argv[4];
const raw = fs.readFileSync(cfgPath, "utf8");
const cfg = JSON5.parse(raw);
cfg.channels = cfg.channels || {};
cfg.channels.feishu = {
  enabled: true,
  dmPolicy: "pairing",
  accounts: { main: { appId, appSecret, botName } },
};
fs.writeFileSync(cfgPath, JSON5.stringify(cfg, null, 2) + "\n");
"""
                env = os.environ.copy()
                env["NODE_PATH"] = oc_nm
                r = subprocess.run(["node", "-e", node_script, CONFIG_PATH, app_id, app_secret, bot_name], env=env, capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or r.stdout or "").strip() or ("node 退出码 %s" % r.returncode))
                with open(RESULT_PATH, "w") as f:
                    json.dump({"status": "saved", "appId": app_id, "botName": bot_name}, f)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
        elif self.path in ("/done", "/skip"):
            with open(DONE_PATH, "w") as f:
                f.write("done" if self.path == "/done" else "skip")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            threading.Thread(target=lambda: (__import__("time").sleep(0.5), os._exit(0))).start()

server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
def open_browser():
    import time
    time.sleep(0.3)
    if not NO_BROWSER:
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
threading.Thread(target=open_browser, daemon=True).start()
server.serve_forever()
'

  SERVER_PID=$!
  info "飞书配置页面已打开 → http://127.0.0.1:$port"
  info "如果浏览器没有自动弹出，可手动打开上面的地址。"

  TIMEOUT=300
  ELAPSED=0
  while [[ $ELAPSED -lt $TIMEOUT ]]; do
    if [[ -s "$done_file" ]]; then
      ACTION=$(cat "$done_file")
      break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
  done

  kill "$SERVER_PID" || true
  rm -f "$html_file"

  if [[ "${ACTION:-}" == "done" ]] && [[ -s "$result_file" ]]; then
    ok "飞书渠道配置已保存"
    echo ""
    warn "接下来请确认："
    echo "  1. 飞书开发者后台已开启 Bot 能力"
    echo "  2. 已添加事件订阅：im.message.receive_v1"
    echo "  3. 已发布应用版本"
    echo "  4. 在飞书中先给 Bot 发一条消息，拿到 Pairing code"
    echo "  5. 在本机执行: openclaw pairing approve feishu <CODE>"
  elif [[ "${ACTION:-}" == "skip" ]]; then
    info "已跳过飞书配置，稍后可运行: openclaw channels add"
  else
    warn "飞书配置页面超时或未响应，稍后可运行: openclaw channels add"
  fi

  rm -f "$done_file" "$result_file"
}

setup_wecom() {
  echo ""
  step "企业微信渠道配置向导"
  info "安装企业微信插件（@wecom/wecom-openclaw-plugin）..."
  if openclaw plugins install @wecom/wecom-openclaw-plugin; then
    ok "企业微信插件已安装"
  else
    warn "企业微信插件安装失败，可稍后手动运行:"
    echo "  openclaw plugins install @wecom/wecom-openclaw-plugin"
    return
  fi

  local port=18900
  local done_file result_file html_file
  done_file=$(mktemp)
  result_file=$(mktemp)
  html_file=$(mktemp /tmp/oc-wecom-XXXXXX.html)

  cat > "$html_file" << 'WECOM_HTML_EOF'
<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenClaw · 企业微信渠道配置</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f0f4f2;padding:24px;">
<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:16px;padding:24px;box-shadow:0 2px 20px rgba(0,0,0,.08);">
<h2 style="margin:0 0 12px;">企业微信渠道配置</h2>
<p style="line-height:1.6;color:#555;">请填写企业微信机器人的 Bot ID 与 Secret。保存后会自动写入 <code>~/.openclaw/openclaw.json</code>。</p>
<p style="line-height:1.6;"><a href="https://open.work.weixin.qq.com/help?doc_id=21657" target="_blank">企业微信 AI 机器人文档</a> · <a href="https://www.npmjs.com/package/@wecom/wecom-openclaw-plugin" target="_blank">插件说明</a></p>
<div style="display:flex;flex-direction:column;gap:12px;margin-top:20px;">
<input id="bot-id" placeholder="Bot ID" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
<input id="bot-secret" placeholder="Secret" type="password" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
<select id="dm-policy" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
  <option value="pairing" selected>pairing（推荐）</option>
  <option value="open">open</option>
  <option value="allowlist">allowlist</option>
  <option value="disabled">disabled</option>
</select>
<div id="msg" style="font-size:12px;color:#b42318;"></div>
<button onclick="save()" style="padding:12px;border:none;border-radius:10px;background:#07C160;color:#fff;cursor:pointer;">保存并继续</button>
<button onclick="skip()" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;background:#fff;cursor:pointer;">跳过，稍后配置</button>
</div></div>
<script>
async function save(){
  const botId=document.getElementById('bot-id').value.trim();
  const secret=document.getElementById('bot-secret').value.trim();
  const dmPolicy=document.getElementById('dm-policy').value;
  if(!botId || !secret){ document.getElementById('msg').textContent='请填写 Bot ID 和 Secret'; return; }
  const res=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({botId,secret,dmPolicy})});
  if(res.ok){ document.body.innerHTML='<div style="font-family:sans-serif;padding:24px;max-width:560px;margin:0 auto;"><h2>企业微信渠道配置成功</h2><p>如果使用 pairing 模式，请先在企微里与机器人对话拿到 Pairing code，再在本机执行 <code>openclaw pairing approve wecom &lt;CODE&gt;</code>。</p><button onclick="done()">完成</button></div>'; }
  else { document.getElementById('msg').textContent=await res.text() || '保存失败'; }
}
async function skip(){ await fetch('/skip',{method:'POST'}); window.close(); }
async function done(){ await fetch('/done',{method:'POST'}); window.close(); }
</script></body></html>
WECOM_HTML_EOF

  _launch_local_wizard_server "$port" "$html_file" "$result_file" "$done_file" '
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/":
            with open(HTML_PATH, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/save":
            try:
                data = json.loads(body)
                bot_id = data.get("botId", "").strip()
                secret = data.get("secret", "").strip()
                dm_policy = data.get("dmPolicy", "pairing").strip() or "pairing"
                if dm_policy not in ("pairing", "open", "allowlist", "disabled"):
                    dm_policy = "pairing"

                npm_root = subprocess.check_output(["npm", "root", "-g"], text=True, timeout=30).strip()
                oc_nm = os.path.join(npm_root, "openclaw", "node_modules")
                if not os.path.isdir(os.path.join(oc_nm, "json5")):
                    raise RuntimeError("未找到 openclaw 自带的 json5 模块，请确认已安装 openclaw")

                node_script = r"""
const fs = require("fs");
const JSON5 = require("json5");
const cfgPath = process.argv[1];
const botId = process.argv[2];
const secret = process.argv[3];
const dmPolicy = process.argv[4];
const raw = fs.readFileSync(cfgPath, "utf8");
const cfg = JSON5.parse(raw);
cfg.channels = cfg.channels || {};
cfg.channels.wecom = {
  enabled: true,
  botId,
  secret,
  dmPolicy,
  groupPolicy: "open",
  sendThinkingMessage: true,
};
fs.writeFileSync(cfgPath, JSON5.stringify(cfg, null, 2) + "\n");
"""
                env = os.environ.copy()
                env["NODE_PATH"] = oc_nm
                r = subprocess.run(["node", "-e", node_script, CONFIG_PATH, bot_id, secret, dm_policy], env=env, capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or r.stdout or "").strip() or ("node 退出码 %s" % r.returncode))
                with open(RESULT_PATH, "w") as f:
                    json.dump({"status": "saved", "botId": bot_id, "dmPolicy": dm_policy}, f)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
        elif self.path in ("/done", "/skip"):
            with open(DONE_PATH, "w") as f:
                f.write("done" if self.path == "/done" else "skip")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            threading.Thread(target=lambda: (__import__("time").sleep(0.5), os._exit(0))).start()

server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
def open_browser():
    import time
    time.sleep(0.3)
    if not NO_BROWSER:
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
threading.Thread(target=open_browser, daemon=True).start()
server.serve_forever()
'

  SERVER_PID=$!
  info "企业微信配置页面已打开 → http://127.0.0.1:$port"
  info "如果浏览器没有自动弹出，可手动打开上面的地址。"

  TIMEOUT=300
  ELAPSED=0
  while [[ $ELAPSED -lt $TIMEOUT ]]; do
    if [[ -s "$done_file" ]]; then
      ACTION=$(cat "$done_file")
      break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
  done

  kill "$SERVER_PID" || true
  rm -f "$html_file"

  if [[ "${ACTION:-}" == "done" ]] && [[ -s "$result_file" ]]; then
    DM_POL=$(python3 -c "import json; d=json.load(open('$result_file')); print(d.get('dmPolicy','pairing'))" 2>/dev/null || echo "pairing")
    ok "企业微信渠道配置已保存（单聊策略: $DM_POL）"
    echo ""
    warn "接下来请确认："
    echo "  1. 企业微信管理后台中机器人已启用"
    echo "  2. 网络可访问企业微信要求的连接地址"
    echo "  3. 若 dmPolicy=allowlist，请在 openclaw.json 中补 channels.wecom.allowFrom"
    if [[ "$DM_POL" == "pairing" ]]; then
      echo "  4. 在企微里先和机器人对话拿到 Pairing code"
      echo "  5. 在本机执行: openclaw pairing approve wecom <CODE>"
    fi
  elif [[ "${ACTION:-}" == "skip" ]]; then
    info "已跳过企业微信配置，稍后可运行: openclaw channels add"
  else
    warn "企业微信配置页面超时或未响应，稍后可运行: openclaw channels add"
  fi

  rm -f "$done_file" "$result_file"
}

_CURRENT_STEP="Step 6 · 渠道配置向导"
step "$_CURRENT_STEP"

if [[ "$NON_INTERACTIVE" == true ]]; then
  info "非交互模式：跳过渠道配置向导（需要时请运行: openclaw channels add）"
else
  echo ""
  echo -e "  OpenClaw 支持通过飞书、企业微信等渠道收发消息。"
  echo -e "  现在可以配置渠道，也可以跳过，安装完成后随时运行："
  echo -e "  ${DIM}openclaw channels add${RESET}"
  echo ""
  read -rp "  是否现在配置渠道？(y/N) " setup_channel
  echo ""
  if [[ "$(_oc_lc "$setup_channel")" == "y" ]]; then
    echo -e "  ${BOLD}1${RESET}  飞书 (Feishu)"
    echo -e "  ${BOLD}2${RESET}  企业微信 (WeCom)"
    echo -e "  ${BOLD}3${RESET}  跳过"
    echo ""
    read -rp "  输入数字 (1/2/3): " channel_choice
    case "$channel_choice" in
      1) setup_feishu ;;
      2) setup_wecom ;;
      *) info "跳过渠道配置" ;;
    esac
  fi
fi

_CURRENT_STEP="Step 7 · 预装精选 Skills"
step "$_CURRENT_STEP"

if [[ "$SKIP_SKILLS" == true ]]; then
  warn "已跳过 Skills 安装（--skip-skills）"
else
  install_skill() {
    local name="$1"
    local source_dir="$2"
    local dest="$OC_SKILLS_DIR/$name"
    if [[ -d "$dest" ]]; then
      warn "Skill '$name' 已存在，跳过"
      return
    fi
    mkdir -p "$dest"
    cp -r "$source_dir/." "$dest/"
    ok "Skill '$name' 已安装"
  }

  SKILLS_TMP=$(mktemp -d)
  trap '[[ -n "${SKILLS_TMP:-}" ]] && rm -rf "$SKILLS_TMP"' EXIT

  install_skill_from_git() {
    if [[ "${GIT_AVAILABLE:-true}" != "true" ]]; then
      warn "git 不可用，跳过 Skill: $1"
      return
    fi
    local name="$1"
    local url="$2"
    local subpath="${3:-}"
    local work="$SKILLS_TMP/$name"
    if ! GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=30 git clone --depth=1 --quiet "$url" "$work" 2>/dev/null; then
      warn "无法克隆 $url（Skill: $name）"
      return
    fi
    if [[ -n "$subpath" ]]; then
      [[ -d "$work/$subpath" ]] && install_skill "$name" "$work/$subpath" || warn "Skill '$name' 缺少路径 $subpath"
    else
      install_skill "$name" "$work"
    fi
  }

  install_larksuite_cli_skill_dirs() {
    if [[ "${GIT_AVAILABLE:-true}" != "true" ]]; then
      warn "git 不可用，跳过飞书官方 lark-cli Skills"
      return
    fi
    local url="https://github.com/larksuite/cli"
    local work="$SKILLS_TMP/lark-cli-repo"
    if ! GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=30 git clone --depth=1 --quiet "$url" "$work" 2>/dev/null; then
      warn "无法克隆 $url（飞书官方 lark-cli Skills）"
      return
    fi
    local skdir="$work/skills"
    local d
    for d in "$skdir"/*/; do
      [[ -d "$d" ]] || continue
      [[ -f "${d}SKILL.md" ]] || continue
      install_skill "$(basename "$d")" "$d"
    done
  }

  install_skill_from_git "goskill" "https://github.com/AIPMAndy/goskill" ""
  install_skill_from_git "dna-memory" "https://github.com/AIPMAndy/dna-memory" ""
  install_skill_from_git "soskill" "https://github.com/AIPMAndy/soskill" "skills/public/soskill"
  install_larksuite_cli_skill_dirs
fi

_CURRENT_STEP="Step 8 · 验证与安装报告"
step "$_CURRENT_STEP"

echo ""
echo -e "${BOLD}  启动 Gateway 并验证...${RESET}"
echo ""

openclaw gateway start --port 18789 &>/dev/null &
sleep 3

_CK_NODE="$(node --version | tr -d 'v' || echo 'NOT FOUND')"
_CK_OC="$(openclaw --version || echo 'NOT FOUND')"
_CK_CFG="$([ -f "$OC_CONFIG" ] && echo '存在' || echo '缺失')"
_CK_BS="$([ ! -f "$OC_BOOTSTRAP" ] && echo '已清除' || echo '仍存在')"
_CK_ENV="$([ -f "$OC_ENV" ] && echo '已生成' || echo '未配置')"
_CK_GW="$(curl -sf --max-time 3 'http://localhost:18789/health' > /dev/null 2>&1 && echo '运行中' || echo '未响应')"
SKILLS_COUNT=$(find "$OC_SKILLS_DIR" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ') || true

echo -e "  ${GREEN}✓${RESET}  Node.js: ${DIM}${_CK_NODE}${RESET}"
echo -e "  ${GREEN}✓${RESET}  OpenClaw: ${DIM}${_CK_OC}${RESET}"
echo -e "  ${GREEN}✓${RESET}  配置文件: ${DIM}${_CK_CFG}${RESET}"
echo -e "  ${GREEN}✓${RESET}  bootstrap.md: ${DIM}${_CK_BS}${RESET}"
echo -e "  ${GREEN}✓${RESET}  API Key (.env): ${DIM}${_CK_ENV}${RESET}"
echo -e "  ${GREEN}✓${RESET}  Gateway 状态: ${DIM}${_CK_GW}${RESET}"
echo -e "  ${GREEN}✓${RESET}  已安装 Skills: ${DIM}${SKILLS_COUNT} 个${RESET}"
echo ""
echo -e "  ${BOLD}常用命令：${RESET}"
echo -e "  ${DIM}openclaw gateway status${RESET}   — 查看运行状态"
echo -e "  ${DIM}openclaw doctor${RESET}            — 健康检查"
echo -e "  ${DIM}openclaw dashboard${RESET}         — 打开控制台"
echo -e "  ${DIM}openclaw configure${RESET}         — 配置向导（渠道、模型等）"
echo ""
echo -e "${GREEN}${BOLD}  OpenClaw 安装完成${RESET}"
