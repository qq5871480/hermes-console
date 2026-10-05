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

On first start the console creates an `admin` user. If you did not set `CONSOLE_INIT_PASSWORD`, a random initial password is printed to the startup log / journal — grab it there. You are forced to change it on first login.

Open `http://server-ip:8787`, log in as `admin`.

## Configuration (env vars, all optional)

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Hermes instance directory to manage |
| `CONSOLE_INIT_PASSWORD` | random | Initial admin password (first init only) |
| `CONSOLE_DB` | `./console.db` | Console's own SQLite database |
| `CONSOLE_BACKUP_DIR` | `~/hermes-console-backups` | Where backups are stored |
| `CONSOLE_GATEWAY_SERVICE` | `hermes-gateway` | `systemctl --user` service name |
| `CONSOLE_SECRET_KEY` | random per boot | Set it to keep sessions across restarts |
| `CONSOLE_HTTP_PROXY` | — | Proxy for git/update operations (e.g. `http://127.0.0.1:8118`) |

## systemd deployment

See [`hermes-console.service`](hermes-console.service). Recommended: restrict the firewall to trusted sources, or put it behind nginx/caddy with TLS.

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

快速开始与安全说明见上方英文部分。开源协议 MIT，欢迎提 Issue / PR。
