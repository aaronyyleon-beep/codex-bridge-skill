#!/usr/bin/env python3
"""OpenClaw bootstrap installer for macOS.

Primary goals:
- Install Node.js / npm when missing
- Install and configure OpenClaw
- Let Codex attempt repair/deployment when an install step fails
- Open local Feishu / WeCom configuration pages after install
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple


GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
RESET = "\033[0m"
DIM = "\033[2m"


def info(msg: str) -> None:
    print(f"{CYAN}[INFO]{RESET}  {msg}")


def ok(msg: str) -> None:
    print(f"{GREEN}[ OK ]{RESET}  {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}[WARN]{RESET}  {msg}")


def err(msg: str) -> None:
    print(f"{RED}[ERR ]{RESET}  {msg}")


def step(msg: str) -> None:
    print(f"\n{BOLD}━━━  {msg}  ━━━{RESET}")


class InstallError(RuntimeError):
    pass


@dataclass
class RuntimeConfig:
    api_key: str
    non_interactive: bool
    skip_skills: bool
    no_browser: bool
    codex_rescue: bool
    channel: str


class Installer:
    def __init__(self, cfg: RuntimeConfig) -> None:
        self.cfg = cfg
        self.current_step = ""
        self.home = Path.home()
        self.oc_home = self.home / ".openclaw"
        self.oc_config = self.oc_home / "openclaw.json"
        self.oc_env = self.oc_home / ".env"
        self.oc_workspace = self.oc_home / "workspace"
        self.oc_skills_dir = self.oc_workspace / "skills"
        self.oc_bootstrap = self.oc_workspace / "bootstrap.md"
        self.oc_plist = self.home / "Library/LaunchAgents/ai.openclaw.gateway.plist"
        self.pinned_openclaw = "2026.3.28"
        self.node_min_major = 22
        self.node_min_minor = 14
        self._codex_rescue_in_progress = False

    def run(self) -> None:
        self.print_banner()
        self.ensure_clt_git_python()
        self.ensure_node_npm()
        self.install_openclaw()
        self.initialize_openclaw_config()
        self.onboard_openclaw()
        self.cleanup_bootstrap()
        self.channel_wizard()
        self.install_skills()
        self.print_report()

    def print_banner(self) -> None:
        print(f"{BOLD}{CYAN}")
        print("  ╔══════════════════════════════════════════════╗")
        print("  ║   OpenClaw 一键安装器 (macOS · Python)      ║")
        print("  ║   Codex-assisted bootstrap installer        ║")
        print("  ╚══════════════════════════════════════════════╝")
        print(f"{RESET}")
        print("  本安装器将依次完成：\n")
        print(f"  {DIM}Step 1{RESET}  检测环境与命令行工具")
        print(f"  {DIM}Step 2{RESET}  安装 Node.js / npm")
        print(f"  {DIM}Step 3{RESET}  安装 OpenClaw CLI")
        print(f"  {DIM}Step 4{RESET}  初始化 OpenClaw 配置")
        print(f"  {DIM}Step 5{RESET}  Onboard + 清理 bootstrap")
        print(f"  {DIM}Step 6{RESET}  打开飞书 / 企业微信 bot 配置页")
        print(f"  {DIM}Step 7{RESET}  安装预置 Skills")
        print(f"  {DIM}Step 8{RESET}  输出验证报告\n")
        if not self.cfg.non_interactive:
            input("  按 Enter 开始，Ctrl+C 取消 ... ")

    def run_cmd(
        self,
        cmd: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        rescue_hint: str = "",
    ) -> subprocess.CompletedProcess[str]:
        cmd_display = " ".join(cmd)
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            capture_output=capture,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            if self.try_codex_rescue(cmd, proc, rescue_hint):
                proc = subprocess.run(
                    list(cmd),
                    cwd=str(cwd) if cwd else None,
                    env=merged_env,
                    capture_output=capture,
                    text=True,
                    check=False,
                )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise InstallError(f"{cmd_display} failed: {detail or f'exit={proc.returncode}'}")
        return proc

    def try_codex_rescue(
        self,
        cmd: Sequence[str],
        proc: subprocess.CompletedProcess[str],
        rescue_hint: str,
    ) -> bool:
        if not self.cfg.codex_rescue or self._codex_rescue_in_progress:
            return False
        codex_bin = shutil.which("codex")
        if not codex_bin:
            return False

        self._codex_rescue_in_progress = True
        try:
            warn("当前步骤失败，尝试调用 Codex 进行本机修复和部署兜底 ...")
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            prompt = textwrap.dedent(
                f"""
                You are repairing a macOS OpenClaw installation on the user's local machine.

                Current step: {self.current_step}
                Failed command: {" ".join(cmd)}
                Rescue hint: {rescue_hint or "(none)"}

                stderr:
                {stderr or "(empty)"}

                stdout:
                {stdout or "(empty)"}

                Please inspect the local environment, install or fix the missing dependency,
                and leave the machine in a state where rerunning the failed command should work.
                Prefer minimal, direct changes. Do not ask for confirmation unless absolutely necessary.
                """
            ).strip()
            rescue = subprocess.run(
                [
                    codex_bin,
                    "exec",
                    "-s",
                    "workspace-write",
                    "--skip-git-repo-check",
                    "--",
                    prompt,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if rescue.returncode == 0:
                ok("Codex 兜底执行完成，重试当前步骤 ...")
                return True
            warn("Codex 兜底未成功，本步骤将按原错误退出")
            detail = (rescue.stderr or rescue.stdout or "").strip()
            if detail:
                warn(detail[:600])
            return False
        finally:
            self._codex_rescue_in_progress = False

    def set_step(self, title: str) -> None:
        self.current_step = title
        step(title)

    def command_exists(self, name: str) -> bool:
        return shutil.which(name) is not None

    def print_macos_security_note(self) -> None:
        warn("如果 macOS 阻止执行安装项，请按下面操作解除安全限制：")
        print("  1. 打开“系统设置” -> “隐私与安全性”")
        print("  2. 在页面底部找到“仍要打开”或“允许”")
        print("  3. 返回终端重新执行安装")

    def open_security_settings(self) -> None:
        if self.cfg.no_browser:
            return
        open_bin = shutil.which("open")
        if open_bin:
            subprocess.run(
                [open_bin, "x-apple.systempreferences:com.apple.preference.security?Privacy"],
                capture_output=True,
                text=True,
                check=False,
            )

    def ensure_clt_git_python(self) -> None:
        self.set_step("Step 1 · 检测环境与命令行工具")
        if not self.command_exists("python3"):
            raise InstallError("当前 Python 安装器需要 python3 执行。请先让 Codex 或 bootstrap 安装 python3。")
        ok(self.python_version())

        if self.command_exists("git"):
            ok(self.git_version())
            return

        warn("未检测到 git，尝试安装 Xcode Command Line Tools ...")
        if self.cfg.non_interactive:
            placeholder = Path("/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress")
            placeholder.touch()
            try:
                proc = self.run_cmd(["softwareupdate", "-l"], rescue_hint="Install Xcode Command Line Tools on macOS.")
                label = ""
                for line in proc.stdout.splitlines():
                    if "Command Line Tools" in line and "Label:" in line:
                        label = line.split("Label:", 1)[1].strip()
                        break
                if label:
                    self.run_cmd(
                        ["softwareupdate", "-i", label, "--verbose"],
                        rescue_hint="Install Xcode Command Line Tools so git becomes available.",
                    )
            finally:
                placeholder.unlink(missing_ok=True)
        else:
            subprocess.run(["xcode-select", "--install"], capture_output=True, text=True, check=False)
            self.open_security_settings()
            self.print_macos_security_note()
            input("请在弹出窗口中完成安装，然后回到终端按 Enter 继续 ... ")

        if self.command_exists("git"):
            ok(self.git_version())
        else:
            warn("git 仍不可用，后续 Skills 安装将跳过")

    def ensure_node_npm(self) -> None:
        self.set_step("Step 2 · 安装 Node.js / npm")
        if self.command_exists("node") and self.command_exists("npm"):
            version = self.node_version_tuple()
            if version and version >= (self.node_min_major, self.node_min_minor):
                ok(f"Node.js v{version[0]}.{version[1]}")
                ok(self.npm_version())
                return
            warn("Node.js 版本过低，尝试升级 ...")

        if self.has_nvm():
            self.run_cmd(
                ["bash", "-lc", "source ~/.nvm/nvm.sh && nvm install 24 && nvm use 24 && nvm alias default 24"],
                rescue_hint="Install Node.js 24 using nvm.",
            )
        elif self.command_exists("brew"):
            self.run_cmd(["brew", "install", "node@24"], rescue_hint="Install node@24 with Homebrew.")
        else:
            self.install_node_pkg_or_nvm()

        if not self.command_exists("node") or not self.command_exists("npm"):
            raise InstallError("Node.js / npm 安装后仍不可用")
        ok(self.node_version_string())
        ok(self.npm_version())

    def install_node_pkg_or_nvm(self) -> None:
        node_pkg = Path("/tmp/node-latest.pkg")
        pkg_name = self.resolve_latest_node_pkg()
        node_url = f"https://nodejs.org/dist/latest-v22.x/{pkg_name}"
        info(f"下载 {node_url} ...")
        urllib.request.urlretrieve(node_url, node_pkg)
        if self.has_passwordless_sudo():
            self.run_cmd(["sudo", "installer", "-pkg", str(node_pkg), "-target", "/"], rescue_hint="Install Node.js from the downloaded pkg.")
            node_pkg.unlink(missing_ok=True)
            os.environ["PATH"] = f"/usr/local/bin:{os.environ.get('PATH', '')}"
            return

        if self.cfg.non_interactive:
            warn("非交互模式且无免密 sudo，改用 nvm 安装到用户空间")
            node_pkg.unlink(missing_ok=True)
            self.install_nvm_to_user_space()
            return

        self.open_security_settings()
        self.print_macos_security_note()
        info("可能需要输入管理员密码以安装 Node.js ...")
        self.run_cmd(["sudo", "installer", "-pkg", str(node_pkg), "-target", "/"], rescue_hint="Install the Node.js pkg on macOS.")
        node_pkg.unlink(missing_ok=True)
        os.environ["PATH"] = f"/usr/local/bin:{os.environ.get('PATH', '')}"

    def install_nvm_to_user_space(self) -> None:
        nvm_dir = self.home / ".nvm"
        nvm_dir.mkdir(parents=True, exist_ok=True)
        installer = Path(tempfile.mktemp(prefix="nvm-", suffix=".sh"))
        urllib.request.urlretrieve("https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh", installer)
        self.run_cmd(["bash", str(installer)], rescue_hint="Install nvm in the user's home directory.")
        installer.unlink(missing_ok=True)
        self.run_cmd(
            ["bash", "-lc", "source ~/.nvm/nvm.sh && nvm install 22 && nvm use 22 && nvm alias default 22"],
            rescue_hint="Install Node.js 22 using nvm after nvm bootstrap.",
        )

    def install_openclaw(self) -> None:
        self.set_step("Step 3 · 安装 OpenClaw CLI")
        self.ensure_npm_prefix_ready()
        current = self.openclaw_version()
        if current == self.pinned_openclaw:
            ok(f"openclaw {current} 已安装，跳过")
            return
        if current:
            info(f"当前版本 {current}，切换到指定版本 {self.pinned_openclaw} ...")
        else:
            info(f"安装 openclaw@{self.pinned_openclaw} ...")
        self.npm_global_install(f"openclaw@{self.pinned_openclaw}")
        if not self.command_exists("openclaw"):
            npm_bin = Path(self.npm_prefix()) / "bin"
            os.environ["PATH"] = f"{npm_bin}:{os.environ.get('PATH', '')}"
        if not self.command_exists("openclaw"):
            raise InstallError("openclaw 命令未找到，请检查 npm 全局路径是否在 PATH 中")
        ok(f"openclaw {self.openclaw_version() or self.pinned_openclaw}")

    def initialize_openclaw_config(self) -> None:
        self.set_step("Step 4 · 初始化 OpenClaw 配置")
        if not self.cfg.api_key.strip():
            raise InstallError("缺少 ZAI_API_KEY，请通过环境变量或 --api-key 传入")

        self.oc_home.mkdir(parents=True, exist_ok=True)
        self.oc_workspace.mkdir(parents=True, exist_ok=True)
        self.oc_skills_dir.mkdir(parents=True, exist_ok=True)

        if self.oc_config.exists() and self.validate_openclaw_config():
            ok("配置文件已存在且校验通过，跳过重新生成")
            os.environ["OC_CONFIG"] = str(self.oc_config)
            return

        if self.oc_env.exists() and not os.access(self.oc_env, os.W_OK):
            try:
                self.oc_env.chmod(0o600)
            except PermissionError:
                self.oc_env.unlink(missing_ok=True)

        self.oc_env.write_text(
            f"# OpenClaw 环境变量\n# 由 Python 安装器生成于 {time.ctime()}\n"
            "# ZAI API Key 写入 openclaw.json（models.providers.zai.apiKey）\n",
            encoding="utf-8",
        )
        self.oc_env.chmod(0o600)
        ok(f".env 已生成: {self.oc_env}")

        if self.oc_config.exists():
            backup = self.oc_config.with_suffix(f".bak.{time.strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(self.oc_config, backup)
            warn(f"已备份旧配置到 {backup}")

        cfg = {
            "agents": {
                "defaults": {
                    "workspace": "~/.openclaw/workspace",
                    "model": {"primary": "zai/glm-5", "fallbacks": ["zai/glm-4.7"]},
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
                        "apiKey": self.cfg.api_key.strip(),
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
        self.oc_config.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.environ["OC_CONFIG"] = str(self.oc_config)
        ok(f"配置文件已生成: {self.oc_config}")
        if not self.validate_openclaw_config():
            raise InstallError("openclaw config validate 未通过")

    def onboard_openclaw(self) -> None:
        self.set_step("Step 5 · Onboard + 注册 Gateway daemon")
        if self.oc_plist.exists():
            ok("launchd daemon 已注册，跳过 onboard")
            return
        gw_token = self.python_hex_token()
        base_cmd = [
            "openclaw",
            "onboard",
            "--non-interactive",
            "--accept-risk",
            "--gateway-auth",
            "token",
            "--gateway-token",
            gw_token,
        ]
        full = base_cmd + [
            "--mode",
            "local",
            "--gateway-port",
            "18789",
            "--gateway-bind",
            "loopback",
            "--install-daemon",
            "--daemon-runtime",
            "node",
            "--skip-skills",
            "--skip-channels",
        ]
        proc = self.run_cmd(full, check=False, rescue_hint="Complete OpenClaw onboard and install the local daemon.")
        if proc.returncode != 0:
            warn("完整 onboard 失败，尝试仅安装 daemon ...")
            self.run_cmd(
                base_cmd + ["--install-daemon"],
                rescue_hint="Install only the OpenClaw daemon after onboard fallback.",
            )
            ok("daemon 注册成功（降级模式）")
            return
        ok("OpenClaw 初始化完成，launchd daemon 已注册")

    def cleanup_bootstrap(self) -> None:
        self.set_step("Step 5 · 清理 bootstrap.md")
        if self.oc_bootstrap.exists():
            self.oc_bootstrap.unlink()
            ok("bootstrap.md 已删除")
        else:
            ok("bootstrap.md 不存在，跳过")

    def channel_wizard(self) -> None:
        self.set_step("Step 6 · 打开 bot 配置页")
        if self.cfg.non_interactive:
            info("非交互模式：跳过渠道配置页，稍后可运行 openclaw channels add")
            return

        channel = self.cfg.channel.strip().lower()
        if channel not in {"", "feishu", "wecom"}:
            channel = ""

        if not channel:
            print("\n  请选择当前要配置的渠道：")
            print(f"  {BOLD}1{RESET}  飞书 (Feishu)")
            print(f"  {BOLD}2{RESET}  企业微信 (WeCom)")
            print(f"  {BOLD}3{RESET}  跳过")
            choice = input("\n  输入数字 (1/2/3): ").strip()
            channel = {"1": "feishu", "2": "wecom"}.get(choice, "")

        if channel == "feishu":
            self.install_channel_plugin(["@openclaw/feishu", "@larksuite/openclaw-lark"], "飞书")
            self.launch_feishu_wizard()
        elif channel == "wecom":
            self.install_channel_plugin(["@wecom/wecom-openclaw-plugin"], "企业微信")
            self.launch_wecom_wizard()
        else:
            info("跳过渠道配置页")

    def install_channel_plugin(self, packages: List[str], label: str) -> None:
        for pkg in packages:
            info(f"安装 {label} 插件 {pkg} ...")
            proc = self.run_cmd(
                ["openclaw", "plugins", "install", pkg],
                check=False,
                rescue_hint=f"Install the OpenClaw plugin {pkg} for {label}.",
            )
            if proc.returncode == 0:
                ok(f"{label} 插件已安装（{pkg}）")
                return
        raise InstallError(f"{label} 插件安装失败")

    def launch_feishu_wizard(self) -> None:
        html = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenClaw · 飞书配置</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f5f5f7;padding:24px;">
<div style="max-width:580px;margin:0 auto;background:#fff;border-radius:16px;padding:24px;box-shadow:0 2px 20px rgba(0,0,0,.08);">
<h2 style="margin:0 0 12px;">飞书 bot 配置</h2>
<p style="line-height:1.6;color:#555;">请填写飞书企业自建应用的 App ID 与 App Secret。保存后会写入 <code>~/.openclaw/openclaw.json</code>。</p>
<p style="line-height:1.6;"><a href="https://open.feishu.cn/app" target="_blank">飞书开发者后台</a> · <a href="https://docs.openclaw.ai/channels/feishu" target="_blank">配置教程</a></p>
<div style="display:flex;flex-direction:column;gap:12px;margin-top:20px;">
<input id="app-id" placeholder="App ID" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
<input id="app-secret" placeholder="App Secret" type="password" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
<input id="bot-name" placeholder="Bot 名称（可选）" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
<div id="msg" style="font-size:12px;color:#b42318;"></div>
<button onclick="save()" style="padding:12px;border:none;border-radius:10px;background:#1664FF;color:#fff;">保存并继续</button>
<button onclick="skip()" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;background:#fff;">跳过，稍后配置</button>
</div></div>
<script>
async function save(){const appId=document.getElementById('app-id').value.trim();const appSecret=document.getElementById('app-secret').value.trim();const botName=document.getElementById('bot-name').value.trim()||'My AI Assistant';if(!appId||!appSecret){document.getElementById('msg').textContent='请填写 App ID 和 App Secret';return;}const res=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({appId,appSecret,botName})});if(res.ok){document.body.innerHTML='<div style="font-family:sans-serif;padding:24px;max-width:580px;margin:0 auto;"><h2>飞书配置已保存</h2><p>接下来请在飞书里先给 Bot 发一条消息，拿到 Pairing code，然后在本机执行 <code>openclaw pairing approve feishu &lt;CODE&gt;</code>。</p><button onclick="done()">完成</button></div>';}else{document.getElementById('msg').textContent=await res.text()||'保存失败';}}
async function skip(){await fetch('/skip',{method:'POST'});window.close();}
async function done(){await fetch('/done',{method:'POST'});window.close();}
</script></body></html>"""

        def save(data: Dict[str, str]) -> Tuple[int, str]:
            app_id = data.get("appId", "").strip()
            app_secret = data.get("appSecret", "").strip()
            bot_name = data.get("botName", "My AI Assistant").strip() or "My AI Assistant"
            if not app_id or not app_secret:
                return 400, "请填写 App ID 和 App Secret"
            self.merge_channel_config(
                """
cfg.channels = cfg.channels || {};
cfg.channels.feishu = {
  enabled: true,
  dmPolicy: "pairing",
  accounts: { main: { appId, appSecret, botName } },
};
""",
                [app_id, app_secret, bot_name],
            )
            return 200, "ok"

        action = self.run_local_wizard(port=18899, html=html, save_handler=save)
        if action == "done":
            ok("飞书 bot 配置已保存")
            warn("接下来请确认：")
            print("  1. 飞书开发者后台已开启 Bot 能力")
            print("  2. 已添加事件订阅：im.message.receive_v1")
            print("  3. 已发布应用版本")
            print("  4. 在飞书里先给 Bot 发一条消息，拿到 Pairing code")
            print("  5. 在本机执行: openclaw pairing approve feishu <CODE>")
        elif action == "skip":
            info("已跳过飞书配置，稍后可运行 openclaw channels add")
        else:
            warn("飞书配置页面超时或未响应，稍后可运行 openclaw channels add")

    def launch_wecom_wizard(self) -> None:
        html = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenClaw · 企业微信配置</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f0f4f2;padding:24px;">
<div style="max-width:580px;margin:0 auto;background:#fff;border-radius:16px;padding:24px;box-shadow:0 2px 20px rgba(0,0,0,.08);">
<h2 style="margin:0 0 12px;">企业微信 bot 配置</h2>
<p style="line-height:1.6;color:#555;">请填写企业微信 AI 机器人的 Bot ID 与 Secret。保存后会写入 <code>~/.openclaw/openclaw.json</code>。</p>
<p style="line-height:1.6;"><a href="https://open.work.weixin.qq.com/help?doc_id=21657" target="_blank">企业微信 AI 机器人文档</a> · <a href="https://www.npmjs.com/package/@wecom/wecom-openclaw-plugin" target="_blank">插件说明</a></p>
<div style="display:flex;flex-direction:column;gap:12px;margin-top:20px;">
<input id="bot-id" placeholder="Bot ID" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
<input id="bot-secret" placeholder="Secret" type="password" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;">
<select id="dm-policy" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;"><option value="pairing" selected>pairing（推荐）</option><option value="open">open</option><option value="allowlist">allowlist</option><option value="disabled">disabled</option></select>
<div id="msg" style="font-size:12px;color:#b42318;"></div>
<button onclick="save()" style="padding:12px;border:none;border-radius:10px;background:#07C160;color:#fff;">保存并继续</button>
<button onclick="skip()" style="padding:12px;border:1px solid #d2d2d7;border-radius:10px;background:#fff;">跳过，稍后配置</button>
</div></div>
<script>
async function save(){const botId=document.getElementById('bot-id').value.trim();const secret=document.getElementById('bot-secret').value.trim();const dmPolicy=document.getElementById('dm-policy').value;if(!botId||!secret){document.getElementById('msg').textContent='请填写 Bot ID 和 Secret';return;}const res=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({botId,secret,dmPolicy})});if(res.ok){document.body.innerHTML='<div style="font-family:sans-serif;padding:24px;max-width:580px;margin:0 auto;"><h2>企业微信配置已保存</h2><p>如果使用 pairing 模式，请先在企微里与机器人对话拿到 Pairing code，再在本机执行 <code>openclaw pairing approve wecom &lt;CODE&gt;</code>。</p><button onclick="done()">完成</button></div>';}else{document.getElementById('msg').textContent=await res.text()||'保存失败';}}
async function skip(){await fetch('/skip',{method:'POST'});window.close();}
async function done(){await fetch('/done',{method:'POST'});window.close();}
</script></body></html>"""

        def save(data: Dict[str, str]) -> Tuple[int, str]:
            bot_id = data.get("botId", "").strip()
            secret = data.get("secret", "").strip()
            dm_policy = data.get("dmPolicy", "pairing").strip() or "pairing"
            if dm_policy not in {"pairing", "open", "allowlist", "disabled"}:
                dm_policy = "pairing"
            if not bot_id or not secret:
                return 400, "请填写 Bot ID 和 Secret"
            self.merge_channel_config(
                """
cfg.channels = cfg.channels || {};
cfg.channels.wecom = {
  enabled: true,
  botId,
  secret,
  dmPolicy,
  groupPolicy: "open",
  sendThinkingMessage: true,
};
""",
                [bot_id, secret, dm_policy],
            )
            return 200, "ok"

        action = self.run_local_wizard(port=18900, html=html, save_handler=save)
        if action == "done":
            ok("企业微信 bot 配置已保存")
            warn("接下来请确认：")
            print("  1. 企业微信管理后台中机器人已启用")
            print("  2. 网络可访问企业微信所需连接")
            print("  3. 若 dmPolicy=allowlist，请补 channels.wecom.allowFrom")
            print("  4. 若 dmPolicy=pairing，在企微里先与机器人对话拿到 Pairing code")
            print("  5. 在本机执行: openclaw pairing approve wecom <CODE>")
        elif action == "skip":
            info("已跳过企业微信配置，稍后可运行 openclaw channels add")
        else:
            warn("企业微信配置页面超时或未响应，稍后可运行 openclaw channels add")

    def merge_channel_config(self, assignment_js: str, values: List[str]) -> None:
        script = textwrap.dedent(
            f"""
            const fs = require("fs");
            const JSON5 = require("json5");
            const cfgPath = process.argv[1];
            const raw = fs.readFileSync(cfgPath, "utf8");
            const cfg = JSON5.parse(raw);
            const args = process.argv.slice(2);
            const [v1, v2, v3] = args;
            const appId = v1;
            const appSecret = v2;
            const botName = v3;
            const botId = v1;
            const secret = v2;
            const dmPolicy = v3;
            {assignment_js}
            fs.writeFileSync(cfgPath, JSON5.stringify(cfg, null, 2) + "\\n");
            """
        ).strip()
        env = os.environ.copy()
        env["NODE_PATH"] = self.node_module_root()
        self.run_cmd(
            ["node", "-e", script, str(self.oc_config), *values],
            env=env,
            rescue_hint="Update openclaw.json for the selected channel using the installed Node runtime.",
        )

    def run_local_wizard(
        self,
        *,
        port: int,
        html: str,
        save_handler: Callable[[Dict[str, str]], Tuple[int, str]],
    ) -> str:
        action_box = {"value": ""}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/":
                    self.send_error(404)
                    return
                payload = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length)
                if self.path == "/save":
                    try:
                        data = json.loads(body.decode("utf-8"))
                        status, message = save_handler(data)
                    except Exception as exc:  # noqa: BLE001
                        status, message = 500, str(exc)
                    payload = message.encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path in {"/done", "/skip"}:
                    action_box["value"] = "done" if self.path == "/done" else "skip"
                    payload = b"ok"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_error(404)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.timeout = 1

        def open_browser() -> None:
            time.sleep(0.4)
            if not self.cfg.no_browser:
                webbrowser.open(f"http://127.0.0.1:{port}/")

        browser_thread = threading.Thread(target=open_browser, daemon=True)
        browser_thread.start()
        info(f"本地配置页面已打开 → http://127.0.0.1:{port}/")
        info("如果浏览器没有自动弹出，可手动打开上面的本地地址。")

        deadline = time.time() + 300
        try:
            while time.time() < deadline and not action_box["value"]:
                server.handle_request()
        finally:
            server.server_close()
        return action_box["value"]

    def install_skills(self) -> None:
        self.set_step("Step 7 · 安装预置 Skills")
        if self.cfg.skip_skills:
            warn("已跳过 Skills 安装（--skip-skills）")
            return
        if not self.command_exists("git"):
            warn("git 不可用，跳过 Skills 安装")
            return

        tmp_dir = Path(tempfile.mkdtemp(prefix="openclaw-skills-"))
        try:
            self.install_skill_repo(tmp_dir, "goskill", "https://github.com/AIPMAndy/goskill")
            self.install_skill_repo(tmp_dir, "dna-memory", "https://github.com/AIPMAndy/dna-memory")
            self.install_skill_repo(tmp_dir, "soskill", "https://github.com/AIPMAndy/soskill", "skills/public/soskill")
            self.install_larksuite_skills(tmp_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def install_skill_repo(self, tmp_dir: Path, name: str, url: str, subpath: str = "") -> None:
        dest = self.oc_skills_dir / name
        if dest.exists():
            warn(f"Skill '{name}' 已存在，跳过")
            return
        work = tmp_dir / name
        proc = self.run_cmd(
            ["git", "clone", "--depth=1", "--quiet", url, str(work)],
            check=False,
            rescue_hint=f"Clone the skill repository {url}.",
        )
        if proc.returncode != 0:
            warn(f"无法克隆 {url}（Skill: {name}）")
            return
        source = work / subpath if subpath else work
        if not source.exists():
            warn(f"Skill '{name}' 缺少路径 {subpath}")
            return
        shutil.copytree(source, dest)
        ok(f"Skill '{name}' 已安装")

    def install_larksuite_skills(self, tmp_dir: Path) -> None:
        work = tmp_dir / "lark-cli-repo"
        proc = self.run_cmd(
            ["git", "clone", "--depth=1", "--quiet", "https://github.com/larksuite/cli", str(work)],
            check=False,
            rescue_hint="Clone the larksuite/cli repository and install skills from its skills directory.",
        )
        if proc.returncode != 0:
            warn("无法克隆 https://github.com/larksuite/cli（飞书官方 lark-cli Skills）")
            return
        skills_dir = work / "skills"
        if not skills_dir.exists():
            warn("larksuite/cli 仓库中未找到 skills/ 目录")
            return
        for item in skills_dir.iterdir():
            if item.is_dir() and (item / "SKILL.md").exists():
                dest = self.oc_skills_dir / item.name
                if dest.exists():
                    warn(f"Skill '{item.name}' 已存在，跳过")
                    continue
                shutil.copytree(item, dest)
                ok(f"Skill '{item.name}' 已安装")

    def print_report(self) -> None:
        self.set_step("Step 8 · 验证与安装报告")
        self.run_cmd(["openclaw", "gateway", "start", "--port", "18789"], check=False, capture=True)
        time.sleep(3)
        checks = [
            ("Node.js", self.node_version_string() if self.command_exists("node") else "NOT FOUND"),
            ("OpenClaw", self.openclaw_version() or "NOT FOUND"),
            ("配置文件", "存在" if self.oc_config.exists() else "缺失"),
            ("bootstrap.md", "已清除" if not self.oc_bootstrap.exists() else "仍存在"),
            ("API Key (.env)", "已生成" if self.oc_env.exists() else "未配置"),
            ("Gateway 状态", "运行中" if self.gateway_alive() else "未响应"),
            ("已安装 Skills", str(self.skill_count())),
        ]

        print("")
        print(f"{BOLD}  安装报告{RESET}")
        print("")
        for label, value in checks:
            mark = "✓"
            color = GREEN
            if value in {"NOT FOUND", "缺失", "未响应", "仍存在"}:
                mark = "✗"
                color = RED
            print(f"  {color}{mark}{RESET}  {label}: {DIM}{value}{RESET}")
        print("")
        print(f"  {BOLD}常用命令：{RESET}")
        print(f"  {DIM}openclaw gateway status{RESET}   — 查看运行状态")
        print(f"  {DIM}openclaw doctor{RESET}            — 健康检查")
        print(f"  {DIM}openclaw dashboard{RESET}         — 打开控制台")
        print(f"  {DIM}openclaw configure{RESET}         — 配置向导（渠道、模型等）")
        print("")
        print(f"{GREEN}{BOLD}  OpenClaw 安装完成{RESET}")

    def python_version(self) -> str:
        proc = subprocess.run(["python3", "--version"], capture_output=True, text=True, check=False)
        return proc.stdout.strip() or proc.stderr.strip() or "python3"

    def git_version(self) -> str:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
        return proc.stdout.strip() or "git"

    def node_version_tuple(self) -> Optional[Tuple[int, int]]:
        if not self.command_exists("node"):
            return None
        proc = subprocess.run(["node", "--version"], capture_output=True, text=True, check=False)
        raw = (proc.stdout or "").strip().lstrip("v")
        parts = raw.split(".")
        if len(parts) < 2:
            return None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None

    def node_version_string(self) -> str:
        proc = subprocess.run(["node", "--version"], capture_output=True, text=True, check=False)
        return (proc.stdout or "").strip() or "node"

    def npm_version(self) -> str:
        proc = subprocess.run(["npm", "--version"], capture_output=True, text=True, check=False)
        return f"npm {(proc.stdout or '').strip() or 'unknown'}"

    def resolve_latest_node_pkg(self) -> str:
        try:
            with urllib.request.urlopen("https://nodejs.org/dist/latest-v22.x/", timeout=20) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return "node-v22.15.0.pkg"
        for token in html.split('"'):
            if token.startswith("node-v") and token.endswith(".pkg"):
                return token
        return "node-v22.15.0.pkg"

    def has_passwordless_sudo(self) -> bool:
        proc = subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True, check=False)
        return proc.returncode == 0

    def has_nvm(self) -> bool:
        proc = subprocess.run(["bash", "-lc", "command -v nvm >/dev/null 2>&1"], capture_output=True, text=True, check=False)
        return proc.returncode == 0

    def ensure_npm_prefix_ready(self) -> None:
        prefix = Path(self.npm_prefix())
        lib_dir = prefix / "lib"
        if lib_dir.exists() and os.access(lib_dir, os.W_OK):
            return
        if self.has_passwordless_sudo() or not self.cfg.non_interactive:
            return
        warn("npm 全局目录不可写，切换到用户空间目录 ~/.npm-global")
        user_prefix = self.home / ".npm-global"
        user_prefix.mkdir(parents=True, exist_ok=True)
        self.run_cmd(["npm", "config", "set", "prefix", str(user_prefix)], rescue_hint="Set npm global prefix to ~/.npm-global so OpenClaw can be installed without sudo.")
        os.environ["PATH"] = f"{user_prefix / 'bin'}:{os.environ.get('PATH', '')}"
        rc_file = self.home / (".bash_profile" if os.environ.get("SHELL", "").endswith("bash") else ".zshrc")
        block = '\n# Added by OpenClaw installer — npm global bin\nexport PATH="$HOME/.npm-global/bin:$PATH"\n'
        existing = rc_file.read_text(encoding="utf-8") if rc_file.exists() else ""
        if "npm-global/bin" not in existing:
            rc_file.write_text(existing + block, encoding="utf-8")

    def npm_prefix(self) -> str:
        proc = subprocess.run(["npm", "config", "get", "prefix"], capture_output=True, text=True, check=False)
        return (proc.stdout or "").strip() or "/usr/local"

    def npm_global_install(self, package: str) -> None:
        cmd = ["npm", "i", "-g", package]
        if self.has_passwordless_sudo() and not os.access(Path(self.npm_prefix()) / "lib", os.W_OK):
            cmd = ["sudo"] + cmd
        elif not os.access(Path(self.npm_prefix()) / "lib", os.W_OK) and not self.cfg.non_interactive:
            cmd = ["sudo"] + cmd
        self.run_cmd(cmd, rescue_hint=f"Install {package} globally with npm so the openclaw command is available.")

    def openclaw_version(self) -> str:
        if not self.command_exists("openclaw"):
            return ""
        proc = subprocess.run(["openclaw", "--version"], capture_output=True, text=True, check=False)
        text = (proc.stdout or proc.stderr or "").strip()
        for part in text.split():
            if part[:4].isdigit() and "." in part:
                return part
        return text

    def validate_openclaw_config(self) -> bool:
        proc = subprocess.run(["openclaw", "config", "validate"], capture_output=True, text=True, check=False)
        return proc.returncode == 0

    def python_hex_token(self) -> str:
        proc = subprocess.run(
            ["python3", "-c", "import secrets; print(secrets.token_hex(32))"],
            capture_output=True,
            text=True,
            check=False,
        )
        token = (proc.stdout or "").strip()
        if token:
            return token
        return "bootstrap-token-placeholder"

    def node_module_root(self) -> str:
        npm_root = self.run_cmd(["npm", "root", "-g"], rescue_hint="Find the global npm root for the installed openclaw package.").stdout.strip()
        oc_nm = Path(npm_root) / "openclaw" / "node_modules"
        return str(oc_nm)

    def gateway_alive(self) -> bool:
        try:
            with urllib.request.urlopen("http://localhost:18789/health", timeout=3) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def skill_count(self) -> int:
        if not self.oc_skills_dir.exists():
            return 0
        return sum(1 for item in self.oc_skills_dir.iterdir() if item.is_dir())


def parse_args(argv: Sequence[str]) -> RuntimeConfig:
    parser = argparse.ArgumentParser(description="Install and bootstrap OpenClaw on macOS.")
    parser.add_argument("--api-key", default="", help="ZAI API key used to initialize openclaw.json")
    parser.add_argument("--non-interactive", "-y", action="store_true", help="Run without interactive prompts")
    parser.add_argument("--skip-skills", action="store_true", help="Skip bundled skills installation")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open local configuration pages")
    parser.add_argument("--no-codex-rescue", action="store_true", help="Disable Codex fallback repair when a step fails")
    parser.add_argument("--channel", choices=["feishu", "wecom"], default="", help="Directly open a specific bot configuration page")
    ns = parser.parse_args(argv)
    api_key = ns.api_key.strip() or os.environ.get("ZAI_API_KEY", "").strip()
    return RuntimeConfig(
        api_key=api_key,
        non_interactive=ns.non_interactive,
        skip_skills=ns.skip_skills,
        no_browser=ns.no_browser,
        codex_rescue=not ns.no_codex_rescue,
        channel=ns.channel,
    )


def main(argv: Sequence[str]) -> int:
    cfg = parse_args(argv)
    installer = Installer(cfg)
    try:
        installer.run()
        return 0
    except KeyboardInterrupt:
        err("安装已被用户取消")
        return 130
    except InstallError as exc:
        err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
