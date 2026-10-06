# Hermes Console

[中文说明](#中文) · English

A lightweight web console for managing [Hermes Agent](https://github.com/NousResearch/hermes-agent) instances — like a cloud-provider control panel, but self-hosted.

Four jobs, four pages: **Model settings · Chat channels · Skills · Backup & restore**.

## Features

- 🔐 Password login (PBKDF2 + salt, 5-attempt lockout for 5 min, forced password change on first login)
- 🤖 **Model settings** — pick a provider from a dropdown, auto-fills the official base URL, read the provider's live model list, one-click connectivity test (real API call), manage saved models (set current / edit / delete). Covers China-region providers with **endpoints verified separately for subscription vs. pay-as-you-go plans** (Zhipu GLM, Alibaba Qwen, Tencent, Volcengine Doubao, Baidu Qianfan, Kimi, MiniMax, DeepSeek, Xiaomi MiMo, StepFun…)
- 💬 **Chat channels** — start/stop/restart the Gateway, per-platform configuration cards, live logs
- 🧩 **Skills** — list / enable / disable / view SKILL.md / upload zip (path-traversal protected), with Chinese description support
- 💾 **Backup & restore** — normal (excludes caches) or full backup, download, restore (auto-snapshots current state before overwriting)
- 🩺 **Dashboard** — version check & update (tracks official release tags), one-click repair (rebuild deps + restart + health check), current model, disk/memory usage
- 🌐 Responsive: works on mobile browsers
- 🔧 Generic by design: driven by the `HERMES_HOME` env var — manage any Hermes instance

## Quick start

```bash
# Requirements: Python 3.10+, Hermes Agent installed on the target machine
git clone https://github.com/qq5871480/hermes-console.git
cd hermes-console
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# Point it at a Hermes instance
export HERMES_HOME=~/.hermes

# Run (dev)
./venv/bin/python app.py            # 127.0.0.1:8787
# or production
./venv/bin/gunicorn -w 2 --timeout 300 -b 0.0.0.0:8787 app:app
```

### First launch — set your own password

**There is no default password.** The first time you open the console in a browser you are taken to a setup page where you create the admin password (min 8 chars). That page closes itself once the admin account exists.

Open `http://server-ip:8787` → create your password → you're in.

> ⚠️ If the console is reachable from the public internet, set your password **immediately** after starting it — whoever reaches the setup page first becomes the admin. Prefer firewall restrictions or TLS reverse proxy (see Security notes).

*(Legacy: setting the `CONSOLE_INIT_PASSWORD` env var still pre-creates the admin user with that password and forces a change on first login.)*

## Configuration (env vars, all optional)

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Hermes instance directory to manage |
| `CONSOLE_INIT_PASSWORD` | none (first-launch setup page) | Optional: pre-create admin with this password instead |
| `CONSOLE_DB` | `./console.db` | Console's own SQLite database |
| `CONSOLE_BACKUP_DIR` | `~/hermes-console-backups` | Where backups are stored |
| `CONSOLE_GATEWAY_SERVICE` | `hermes-gateway` | `systemctl --user` service name |
| `CONSOLE_SECRET_KEY` | random per boot | Set it to keep sessions across restarts |
| `CONSOLE_HTTP_PROXY` | — | Proxy for git/update operations (e.g. `http://127.0.0.1:8118`) |

## Usage guide (four pages)

**Dashboard** — shows the current Hermes version, Gateway status, active model, skill count and disk/memory usage.
- *Check for updates* compares against the latest **official release tag** (not every repo commit); the *Update* button upgrades to that release automatically (pull code → rebuild dependencies → restart Gateway), with a progress dialog. The button is disabled when already up to date.
- *🔧 One-click repair* rebuilds the dependency environment (`hermes pm repair`), restarts the Gateway and verifies health — use it whenever the Gateway shows an abnormal state.

**Model settings** — configure which LLM the agent talks to:
1. Pick a provider from the dropdown (grouped: subscription plans / pay-as-you-go / local / custom). The official base URL is filled in automatically.
2. Paste your API key.
3. Click *📥 Read available models* — the console calls the provider's real `/models` endpoint and turns the model field into a dropdown of models **your key can actually use**. Pick one.
4. *Connectivity test* makes one real API call; *Save* writes `config.yaml` + `.env` (the old config is backed up as `config.yaml.console-bak`). Restart the Gateway on the Channels page to apply.
- Note: subscription (Coding Plan) and pay-as-you-go endpoints are **different** for most China-region providers — using the wrong one can bypass plan quota or trigger pay-as-you-go billing. The built-in addresses were verified against official docs.
- The *Configured models* table lets you switch the active model, edit or delete saved entries. Keys are never displayed.

**Chat channels** — Gateway control (start / stop / restart), per-platform config cards (WeChat, QQ, DingTalk, Feishu, Telegram, Discord…) and live logs. Fill in the platform's credentials and restart the Gateway to bring a channel online.

**Skills** — every skill under `$HERMES_HOME/skills`: enable/disable, view its SKILL.md, edit the Chinese description (stored in `skill_zh.json`), or upload a new skill as a zip (must contain SKILL.md; path traversal is rejected).

**Backup & restore** —
- *Normal backup*: config + memory + sessions + skills + credentials, caches excluded — seconds, small file, for daily use.
- *Full backup*: the entire `HERMES_HOME` with nothing excluded — for migration or before big changes. (The framework itself and systemd units are not included; on a fresh server: install Hermes Agent → restore the full backup.)
- *Restore* requires typing `RESTORE`; the current state is auto-snapshotted first, and the Gateway is stopped/restarted around the operation.

## systemd deployment

`install.sh` asks "设置开机自启?" at the end — answer Y (default) and it registers a **user-level systemd service** (`systemctl --user enable --now hermes-console`), auto-starting on boot and on crash (Restart=on-failure). It also enables linger when possible.

For manual/system-wide setup, see [`hermes-console.service`](hermes-console.service). Recommended: restrict the firewall to trusted sources, or put it behind nginx/caddy with TLS.

## Security notes (read this)

- This console can edit Hermes config, restart the Gateway and restore backups = **instance-level admin power**. Change the initial password immediately.
- Plain HTTP by default. **Always put TLS in front** (caddy/nginx) for public deployments.
- API keys are shown masked in the UI and stored in `.env` (mode 600); never rendered back.
- Skill zip uploads are validated against path traversal; restore requires typing `RESTORE`.
- No credentials or instance data are committed to this repo (see `.gitignore`).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome.

## License

MIT — see [LICENSE](LICENSE).

---

<a name="中文"></a>
## 中文说明

[Hermes Agent](https://github.com/NousResearch/hermes-agent) 的轻量级 Web 管理控制台，像云厂商控制台一样简单，但完全自托管。

四件事，四个页面：**模型设置 · 会话通道 · 技能管理 · 备份还原**。

特性亮点：
- 模型设置内置**国内服务商全家桶**，且区分「订阅版 / 按量版」不同端点（均对官方文档核实、实测存活）：智谱 GLM、阿里千问、腾讯、火山方舟豆包、百度千帆、Kimi、MiniMax、DeepSeek、小米 MiMo、阶跃星辰等
- 一键读取服务商真实可用模型列表、连通性测试（真实调 API）
- 仪表盘：版本检查/更新（跟踪官方正式 tag）、一键修复（重建依赖+重启+健康校验）、当前模型、磁盘内存占用
- 备份还原：普通备份（排除缓存）/ 全量备份，下载、还原前自动留档
- 手机端自适应
- `HERMES_HOME` 环境变量驱动，可管理任意 Hermes 实例

### 使用说明（四个页面）

**仪表盘**：显示当前Hermes版本、Gateway状态、生效模型、技能数、磁盘内存。
- 「检查更新」只对比**官方正式发布版**（tag），不追日常小提交；「更新」自动升级（拉代码→重建依赖→重启Gateway），弹窗显示进度；已是最新时按钮置灰。
- 「🔧一键修复」：重建依赖环境+重启Gateway+健康校验，Gateway异常时点它。

**模型设置**：①下拉选服务商（订阅版/按量版/本地/自定义分组，官方地址自动带出）→ ②填API Key → ③点「读取可用模型」（真实调用服务商接口，模型框变成**你的key真实可用**的下拉列表）→ ④连通测试/保存。保存后到「会话通道」页重启Gateway生效。
- 注意：国内服务商的**订阅版和按量版端点不同**，用错会不扣套餐额度或转按量计费；内置地址均对官方文档核实过。
- 「已配置模型」表可切换当前模型、修改、删除；密钥任何情况下不回显。

**会话通道**：Gateway启动/停止/重启；各平台（微信、QQ、钉钉、飞书、Telegram、Discord等）配置卡片，填好凭据重启Gateway即上线；实时日志。

**技能管理**：启用/禁用、查看SKILL.md、编辑中文描述、上传zip新技能（须含SKILL.md，拒绝路径穿越）。

**备份还原**：
- 普通备份=配置+记忆+会话+技能+凭据（排除缓存），秒级、体积小，日常用；
- 全量备份=整个HERMES_HOME完整打包，迁移或大改前用（不含框架程序本身；新服务器复原=装Hermes→还原全量备份）；
- 还原须输入RESTORE确认，还原前自动给当前状态留档。

快速开始与安全说明见上方英文部分。开源协议 MIT，欢迎提 Issue / PR。
