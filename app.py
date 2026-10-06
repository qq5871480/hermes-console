#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes Console — 轻量级 Hermes Agent 管理控制台
功能：模型设置 / 会话通道 / 技能管理 / 备份还原 / 仪表盘
设计目标：通用（HERMES_HOME环境变量驱动，可管理任何Hermes实例）、可开源。
"""
import os
import shlex
import requests
import re
import io
import sqlite3
import hashlib
import secrets
import subprocess
import tarfile
import threading
import time
import shutil
import zipfile
from pathlib import Path
from functools import wraps

from flask import (Flask, request, session, redirect, url_for,
                   render_template, flash, send_file, abort, jsonify)
from ruamel.yaml import YAML

# ---------- 配置（全部环境变量驱动，无硬编码个人信息） ----------
HERMES_HOME = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
BACKUP_DIR = Path(os.environ.get('CONSOLE_BACKUP_DIR', str(Path.home() / 'hermes-console-backups')))
APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('CONSOLE_DB', str(APP_DIR / 'console.db')))
# 首次访问设置模式：不设任何默认密码。
# 第一次打开网页即引导创建管理员密码（见 /setup）。
# 兼容保留：如显式设置 CONSOLE_INIT_PASSWORD 环境变量，仍按其初始化admin（须首登改密）。
INIT_PASSWORD = os.environ.get('CONSOLE_INIT_PASSWORD', '')

def _resolve_hermes_bin():
    env = os.environ.get('HERMES_BIN')
    if env and Path(env).exists():
        return env
    w = shutil.which('hermes')
    if w:
        return w
    for c in [Path.home() / '.local/bin/hermes', Path('/usr/local/bin/hermes'),
              Path('/opt/hermes/hermes-agent/.venv/bin/hermes')]:
        if c.exists():
            return str(c)
    return 'hermes'

HERMES_BIN = _resolve_hermes_bin()
HTTP_PROXY = os.environ.get('CONSOLE_HTTP_PROXY', '')  # git fetch/pull 用的代理（可空）

def framework_dir():
    """从hermes二进制反推框架git目录（含.git的目录）。
    兼容两种形态：venv入口脚本、shell包装器（exec 真实路径）。"""
    candidates = []
    try:
        candidates.append(Path(HERMES_BIN).resolve())
    except Exception:
        pass
    # shell包装器：读出 exec 的真实路径
    try:
        head = Path(HERMES_BIN).read_text(errors='replace')[:500]
        m = re.search(r"exec\s+[\"']?(/[^\s\"']+)", head)
        if m:
            candidates.append(Path(m.group(1)).resolve())
    except Exception:
        pass
    for c in candidates:
        for parent in c.parents:
            if (parent / '.git').exists():
                return parent
    # 兜底：常见安装位置
    for guess in [Path.home() / 'hermes/hermes-agent', Path('/opt/hermes/hermes-agent')]:
        if (guess / '.git').exists():
            return guess
    return None
GATEWAY_SERVICE = os.environ.get('CONSOLE_GATEWAY_SERVICE', 'hermes-gateway')
yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)
yaml.width = 4096

# 框架注册表（从Hermes源码导出的70个provider+平台字段定义）
REGISTRY_PATH = Path(os.environ.get('CONSOLE_REGISTRY', str(Path(__file__).resolve().parent / 'hermes_registry.json')))
# 官网核实后的端点修正（框架注册表滞后时以此为准）：provider_id -> base_url
ENDPOINT_FIXES = {
    # 小米MiMo订阅（Token Plan）官方端点，用户核实 2026.10.6
    'xiaomi': 'https://token-plan-cn.xiaomimimo.com/v1',
}

def registry():
    import json
    try:
        reg = json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
    except Exception:
        reg = {'providers': {}, 'platforms': {}}
    for pid, url in ENDPOINT_FIXES.items():
        if pid in reg.get('providers', {}):
            reg['providers'][pid]['base_url'] = url
    return reg

app = Flask(__name__)
_SKF = APP_DIR / '.secret_key'
if os.environ.get('CONSOLE_SECRET_KEY'):
    app.secret_key = os.environ['CONSOLE_SECRET_KEY']
elif _SKF.exists():
    app.secret_key = _SKF.read_text().strip()
else:
    app.secret_key = secrets.token_hex(32)
    try:
        _SKF.write_text(app.secret_key)
        os.chmod(_SKF, 0o600)
    except OSError:
        pass
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 技能zip上传上限100M
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

BACKUP_EXCLUDES = {'cache', 'audio_cache', 'tmp', 'backups', '__pycache__',
                   'venv', 'node_modules', 'browser_data', 'image_cache', 'tools', 'logs', 'uploads'}

# ---------- 数据库 ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS models(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT, model_name TEXT, base_url TEXT, key_env TEXT,
            is_current INTEGER DEFAULT 0, created REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS skill_zh(
            path TEXT PRIMARY KEY, desc_zh TEXT, src_hash TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS chats(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, session_id TEXT, created REAL, last_active REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS chat_msgs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER, role TEXT, content TEXT, created REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT UNIQUE, pw_hash TEXT,
            salt TEXT, must_change INTEGER DEFAULT 1,
            login_fails INTEGER DEFAULT 0, locked_until REAL DEFAULT 0)''')

        conn.execute('''CREATE TABLE IF NOT EXISTS cron_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT, job_name TEXT, ts TEXT,
            ok INTEGER, output TEXT, duration REAL)''')

        conn.execute('''CREATE TABLE IF NOT EXISTS metrics(
            ts INTEGER PRIMARY KEY, cpu REAL, mem REAL, load1 REAL)''')

        conn.execute('''CREATE TABLE IF NOT EXISTS chat_tasks(
            id TEXT PRIMARY KEY, chat_id INTEGER, status TEXT,
            started REAL, output TEXT, session_id TEXT, error TEXT)''')

        conn.execute('''CREATE TABLE IF NOT EXISTS instances(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, home TEXT UNIQUE, note TEXT)''')
        if INIT_PASSWORD and not conn.execute('SELECT id FROM users WHERE username="admin"').fetchone():
            salt = secrets.token_hex(16)
            conn.execute('INSERT OR IGNORE INTO users(username,pw_hash,salt,must_change) VALUES(?,?,?,1)',
                         ('admin', hashlib.pbkdf2_hmac('sha256', INIT_PASSWORD.encode(), salt.encode(), 200000).hex(), salt))

def admin_exists():
    with db() as conn:
        return conn.execute('SELECT id FROM users WHERE username="admin"').fetchone() is not None

def verify_pw(username, password):
    with db() as conn:
        u = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if not u:
            return None
        if u['locked_until'] and time.time() < u['locked_until']:
            return None
        h = hashlib.pbkdf2_hmac('sha256', password.encode(), u['salt'].encode(), 200000).hex()
        if secrets.compare_digest(h, u['pw_hash']):
            conn.execute('UPDATE users SET login_fails=0, locked_until=0 WHERE id=?', (u['id'],))
            return dict(u)
        fails = u['login_fails'] + 1
        lock = time.time() + 300 if fails >= 5 else 0   # 5次失败锁5分钟
        conn.execute('UPDATE users SET login_fails=?, locked_until=? WHERE id=?', (fails, lock, u['id']))
        return None

def set_pw(username, newpw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', newpw.encode(), salt.encode(), 200000).hex()
    with db() as conn:
        conn.execute('UPDATE users SET pw_hash=?, salt=?, must_change=0 WHERE username=?', (h, salt, username))

# ---------- 登录装饰器 ----------
def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if 'user' not in session:
            return redirect(url_for('login'))
        if session.get('must_change') and request.endpoint not in ('change_password', 'logout', 'static'):
            return redirect(url_for('change_password'))
        return f(*a, **kw)
    return wrapper

# ---------- Hermes 配置文件读写 ----------
def cfg_path():
    return HERMES_HOME / 'config.yaml'

def load_cfg():
    p = cfg_path()
    if not p.exists():
        return {}
    with open(p, encoding='utf-8') as fh:
        return yaml.load(fh) or {}

def save_cfg(data):
    p = cfg_path()
    if p.exists():
        shutil.copy2(p, str(p) + '.console-bak')
    buf = io.StringIO()
    yaml.dump(data, buf)
    p.write_text(buf.getvalue(), encoding='utf-8')

def env_path():
    return HERMES_HOME / '.env'

def load_env():
    d, order = {}, []
    p = env_path()
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line.strip())
            if m:
                d[m.group(1)] = m.group(2).strip().strip('"').strip("'")
                order.append(m.group(1))
    return d, order

def save_env(d, order):
    p = env_path()
    if p.exists():
        shutil.copy2(p, str(p) + '.console-bak')
    lines = ['# Managed by Hermes Console']
    seen = set()
    for k in order:
        if k in d:
            lines.append(f'{k}={d[k]}')
            seen.add(k)
    for k, v in d.items():
        if k not in seen:
            lines.append(f'{k}={v}')
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    os.chmod(p, 0o600)

def run(cmd, timeout=30, user_env=True, proxy=False):
    env = dict(os.environ)
    if user_env:
        uid = os.getuid()
        env['XDG_RUNTIME_DIR'] = f'/run/user/{uid}'
        env['HERMES_HOME'] = str(HERMES_HOME)
        env['PATH'] = str(Path.home() / '.local/bin') + ':' + env.get('PATH', '')
    if proxy and HTTP_PROXY:
        env['http_proxy'] = env['https_proxy'] = HTTP_PROXY
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except subprocess.TimeoutExpired:
        return 124, '命令超时'
    except Exception as e:
        return 1, str(e)

def hermes_version():
    """本地已安装的官方发布版本号（如 2026.9.24）。优先取HEAD上的tag。"""
    fd = framework_dir()
    if fd:
        rc, t = run(f"git -C {fd} describe --tags --abbrev=0 HEAD 2>/dev/null", timeout=15)
        if rc == 0 and t.strip().lstrip('v'):
            return t.strip().lstrip('v')
    rc, out = run(f'{HERMES_BIN} --version 2>&1 | head -1')
    if rc != 0 or not out.strip():
        return '未知'
    m = re.search(r'\(([^)]+)\)', out.strip())
    return m.group(1) if m else out.strip()

def gateway_status():
    rc, out = run(f'systemctl --user is-active {GATEWAY_SERVICE} 2>&1')
    return out.strip()

def gateway_ctl(action):
    if action not in ('start', 'stop', 'restart'):
        return 1, '非法操作'
    return run(f'systemctl --user {action} {GATEWAY_SERVICE}')

def mask(s, keep=4):
    if not s:
        return ''
    return s[:keep] + '•' * max(len(s) - keep, 4) if len(s) > keep else '•' * 8

# ---------- 路由：认证 ----------
@app.route('/setup', methods=['GET', 'POST'])
def setup_admin():
    """首次访问：创建管理员密码。仅在库里没有admin时开放。"""
    if admin_exists():
        return redirect(url_for('login'))
    if request.method == 'POST':
        p1 = request.form.get('new1', '')
        p2 = request.form.get('new2', '')
        if len(p1) < 8:
            flash('密码至少8位', 'err')
        elif p1 != p2:
            flash('两次输入的密码不一致', 'err')
        else:
            salt = secrets.token_hex(16)
            with db() as conn:
                if not admin_exists():
                    conn.execute('INSERT OR IGNORE INTO users(username,pw_hash,salt,must_change) VALUES(?,?,?,0)',
                                 ('admin', hashlib.pbkdf2_hmac('sha256', p1.encode(), salt.encode(), 200000).hex(), salt))
            session['user'] = 'admin'
            session['must_change'] = False
            flash('管理员创建成功，已自动登录', 'ok')
            return redirect(url_for('dashboard'))
    return render_template('setup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not admin_exists():
        return redirect(url_for('setup_admin'))
    if request.method == 'POST':
        u = verify_pw(request.form.get('username', 'admin'), request.form.get('password', ''))
        if u:
            session['user'] = u['username']
            session['must_change'] = bool(u['must_change'])
            return redirect(url_for('dashboard'))
        flash('用户名或密码错误（连续5次失败锁定5分钟）', 'err')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        old, new1, new2 = (request.form.get(k, '') for k in ('old', 'new1', 'new2'))
        if not verify_pw(session['user'], old):
            flash('原密码错误', 'err')
        elif len(new1) < 8:
            flash('新密码至少8位', 'err')
        elif new1 != new2:
            flash('两次新密码不一致', 'err')
        elif INIT_PASSWORD and new1 == INIT_PASSWORD:
            flash('新密码不能与初始密码相同', 'err')
        else:
            set_pw(session['user'], new1)
            session['must_change'] = False
            flash('密码已修改', 'ok')
            return redirect(url_for('dashboard'))
    return render_template('change_password.html', forced=session.get('must_change'))

# ---------- 仪表盘 ----------
@app.route('/')
@login_required
def dashboard():
    rc, disk = run("df -h " + str(HERMES_HOME) + " | tail -1 | awk '{print $3\" / \"$2\" (\"$5\")\"}'")
    rc2, mem = run("free -b | awk '/Mem/{printf \"%.0f MB / %.1f GB\", $3/1048576, $2/1073741824}'")
    # CPU负载+核数+运行时长
    _, loadavg = run("cat /proc/loadavg | awk '{print $1, $2, $3}'")
    _, ncpu_r = run("nproc")
    try:
        ncpu = int(ncpu_r.strip())
    except ValueError:
        ncpu = 1
    # 线程数（逻辑核）+整机CPU占用率（proc/stat两次采样）
    _, nthread_r = run("grep -c '^processor' /proc/cpuinfo")
    try:
        nthread = int(nthread_r.strip())
    except ValueError:
        nthread = ncpu
    _, cpu_sample = run("cat /proc/stat | grep '^cpu ' | awk '{print $2+$3+$4, $5}' && sleep 0.5 && cat /proc/stat | grep '^cpu ' | awk '{print $2+$3+$4, $5}'", timeout=15)
    cpu_pct = ''
    try:
        lines = [x for x in cpu_sample.strip().splitlines() if x.strip()]
        if len(lines) == 2:
            b1, i1 = (int(x) for x in lines[0].split())
            b2, i2 = (int(x) for x in lines[1].split())
            busy = b2 - b1
            total = (b2 + i2) - (b1 + i1)
            if total > 0:
                cpu_pct = f'{busy*100//total}%'
    except (ValueError, IndexError):
        pass
    _, uptime_s = run("uptime -p 2>/dev/null | sed 's/up //'")
    # 中文格式化uptime
    m2 = re.search(r'(\d+)\s*years?\s*', uptime_s)
    m3 = re.search(r'(\d+)\s*months?\s*', uptime_s)
    m4 = re.search(r'(\d+)\s*weeks?\s*', uptime_s)
    m5 = re.search(r'(\d+)\s*days?\s*', uptime_s)
    m6 = re.search(r'(\d+)\s*hours?\s*', uptime_s)
    m7 = re.search(r'(\d+)\s*minutes?\s*', uptime_s)
    uptime_cn = ''.join(filter(None, [
        (m2.group(1) + '年') if m2 else '',
        (m3.group(1) + '个月') if m3 else '',
        (m4.group(1) + '周') if m4 else '',
        (m5.group(1) + '天') if m5 else '',
        (m6.group(1) + '小时') if m6 else '',
        (m7.group(1) + '分钟') if m7 else '',
    ])) or (uptime_s.strip() if uptime_s.strip() else '刚刚')
    # 负载解析为占比
    loads = []
    try:
        parts = loadavg.split()
        loads = [float(x) for x in parts[:3]]
    except (ValueError, IndexError):
        pass
    # 磁盘细分：总量/已用/百分比
    _, disk_full = run("df -B1 " + str(HERMES_HOME) + " | tail -1 | awk '{print $2, $3, $5}'")
    disk_total = disk_used = disk_pct = ''
    try:
        t, u, pc = disk_full.split()
        disk_total = f'{int(t)/1073741824:.0f} GB'
        disk_used = f'{int(u)/1073741824:.1f} GB'
        disk_pct = pc
    except (ValueError, IndexError):
        pass
    # 内存百分比
    _, mem_full = run("free -b | awk '/Mem/{print $2, $3}'")
    mem_pct = ''
    try:
        mt, mu = mem_full.split()
        mem_pct = f'{int(mu)*100//int(mt)}%'
    except (ValueError, IndexError):
        pass
    # SOUL字符
    soul_chars = 0
    try:
        soul_chars = len((HERMES_HOME / 'SOUL.md').read_text(encoding='utf-8'))
    except OSError:
        pass
    # Hermes安装目录大小
    _, fw_size = run("du -sh " + str(Path.home() / 'hermes' / 'hermes-agent') + " 2>/dev/null | awk '{print $1}'")
    # 最近会话时间
    _, last_sess = run(f"{HERMES_BIN} sessions list 2>/dev/null | head -2 | tail -1", timeout=20)
    sdir = HERMES_HOME / 'skills'
    n_skills = len(list(sdir.rglob('SKILL.md'))) if sdir.exists() else 0
    cfg = load_cfg()
    m = cfg.get('model') or {}
    envd, _ = load_env()
    n_keys = sum(1 for k in envd if ('KEY' in k or 'TOKEN' in k) and envd.get(k))
    # 定时任务
    cron_jobs = _cron_jobs()
    n_cron = len(cron_jobs)
    cron_next = next((j for j in cron_jobs if j['next_run']), None)
    # 会话数（控制台聊天+Hermes sessions）
    with db() as conn:
        n_chats = conn.execute('SELECT COUNT(*) FROM chats').fetchone()[0]
    _, n_sessions = run(f'{HERMES_BIN} sessions list 2>/dev/null | grep -c "_" || echo 0', timeout=20)
    # 最近备份
    bks = sorted(BACKUP_DIR.glob('*.tar.gz'), key=lambda x: x.stat().st_mtime, reverse=True)
    last_backup = bks[0].stat().st_mtime if bks else 0
    last_backup_str = time.strftime('%m-%d %H:%M', time.localtime(last_backup)) if last_backup else '从未'
    # 记忆占用
    mem_mem = 0; mem_usr = 0
    try:
        mem_mem = (HERMES_HOME / 'memories' / 'MEMORY.md').stat().st_size
    except OSError: pass
    try:
        mem_usr = (HERMES_HOME / 'memories' / 'USER.md').stat().st_size
    except OSError: pass
    mem_cfg = cfg.get('memory') or {}
    # Gateway进程详情
    gw_pid, gw_since = '', ''
    _, gwinfo = run(f"systemctl show {shlex.quote(GATEWAY_SERVICE)} -p MainPID,ActiveEnterTimestamp --no-pager 2>/dev/null", timeout=15)
    for line in gwinfo.splitlines():
        if line.startswith('MainPID=') and line[8:] not in ('', '0'):
            gw_pid = line[8:]
        elif line.startswith('ActiveEnterTimestamp='):
            gw_since = line[len('ActiveEnterTimestamp='):].strip()
    return render_template('dashboard.html', ver=hermes_version(), gw=gateway_status(),
                           disk=disk.strip(), mem=mem.strip(), home=str(HERMES_HOME),
                           n_skills=n_skills, n_backups=len(list(BACKUP_DIR.glob('*.tar.gz'))),
                           model_default=m.get('default', '（未设置）'), model_provider=m.get('provider', '（未设置）'),
                           n_keys=n_keys, loads=loads, ncpu=ncpu, nthread=nthread, cpu_pct=cpu_pct, uptime_cn=uptime_cn,
                           disk_total=disk_total, disk_used=disk_used, disk_pct=disk_pct, mem_pct=mem_pct,
                           soul_chars=soul_chars, fw_size=fw_size.strip() or '—',
                           n_cron=n_cron, cron_next=(cron_next['name'] + ' · ' + cron_next['next_run']) if cron_next else '无排期',
                           n_chats=n_chats, n_sessions=(n_sessions or '0').strip(),
                           last_backup=last_backup_str, n_backups_all=len(bks),
                           mem_mem=mem_mem, mem_usr=mem_usr,
                           mem_limit=(mem_cfg.get('memory_char_limit', 4400)),
                           usr_limit=(mem_cfg.get('user_char_limit', 1375)),
                           gw_pid=gw_pid, gw_since=gw_since)

# ---------- 人格（SOUL.md） ----------
SOUL_FILE = HERMES_HOME / 'SOUL.md'

@app.route('/system/reboot', methods=['POST'])
@login_required
def system_reboot():
    if (request.get_json(force=True, silent=True) or {}).get('confirm') != 'REBOOT':
        return jsonify({'ok': False, 'msg': '确认词错误'})
    rc, out = run('sudo -n reboot 2>&1', timeout=10)
    if rc != 0:
        return jsonify({'ok': False, 'msg': '重启指令失败：%s（检查sudo免密配置）' % (out or rc)[:200]})
    return jsonify({'ok': True, 'msg': '重启指令已发出，机器约1-2分钟后恢复，页面会自动重连'})


def _log_filter_lines(raw_lines, q, level):
    """按关键词(q)与级别(level逗号分隔)过滤日志行，返回(级别标记, 行)列表。统一Python侧过滤。"""
    lvls = {x.strip().upper() for x in (level or '').split(',') if x.strip()}
    out = []
    for ln in raw_lines:
        low = ln.lower()
        if ' error ' in low or 'traceback' in low:
            lv = 'E'
        elif ' warn ' in low or ' warning ' in low:
            lv = 'W'
        else:
            lv = ''
        if q and q.lower() not in ln.lower():
            continue
        if lvls:
            want = ('ERROR' in lvls and lv == 'E') or ('WARN' in lvls and lv == 'W') or ('INFO' in lvls and lv == '')
            if not want:
                continue
        out.append((lv, ln))
    return out


@app.route('/logs')
@login_required


def logs():
    _, unit = run("systemctl --user list-units --type=service --no-legend 2>/dev/null | grep -E 'hermes-gateway|gateway' | awk '{print $1}' | head -1")
    unit = unit.strip() or 'hermes-gateway'
    try:
        n = int(request.args.get('n', 200))
    except ValueError:
        n = 200
    n = max(50, min(n, 2000))
    q = request.args.get('q', '')
    level = request.args.get('level', '')
    _, out = run(f"journalctl --user -u {shlex.quote(unit)} -n {n} --no-pager -o short-iso 2>&1 | tail -n {n}")
    if 'No journal files' in out or not out.strip() or 'No entries' in out:
        _, out2 = run(f"journalctl -u {shlex.quote(unit)} -n {n} --no-pager -o short-iso 2>&1 | tail -n {n}")
        out = out2 if out2.strip() and 'No entries' not in out2 and 'No journal' not in out2 else out
    if 'No journal files' in out or not out.strip() or 'No entries' in out:
        # 最终兜底：~/.hermes/logs/gateway.log
        _, out2 = run(f"tail -n {n} {shlex.quote(str(HERMES_HOME))}/logs/gateway.log 2>/dev/null")
        out = out2 if out2.strip() else out
    lines = _log_filter_lines(out.splitlines(), q, level)
    return render_template('logs.html', logs=lines, unit=unit, n=n, q=q, level=level)


_RATE_LIMIT_RE = re.compile(r'rate-?limited|429|Too Many Requests', re.I)


def _hermes_explain(prompt, timeout=170):
    """一次性调用 hermes CLI 做日志解读。限流时等25秒自动重试一次，再失败给人话提示。"""
    last_err = ''
    for attempt in (1, 2):
        try:
            out, _sid = _hermes_ask(prompt, timeout=timeout)
            out = (out or '').strip()
            if out and _RATE_LIMIT_RE.search(out):
                last_err = out
                if attempt == 1:
                    import time as _t
                    _t.sleep(25)
                    continue
                return ('（模型限流中：当前模型请求太频繁，等一两分钟再点「AI解读」，'
                        '或在「模型设置」切换到其他模型再试）')
            return out or '（无输出）'
        except subprocess.TimeoutExpired:
            return '（超时：Agent处理超过%d秒，可稍后重试）' % timeout
        except Exception as e:
            last_err = str(e)
            if attempt == 1 and _RATE_LIMIT_RE.search(last_err):
                import time as _t
                _t.sleep(25)
                continue
    return '（解读失败：%s……建议：等一两分钟再试，或在「模型设置」换一个模型）' % last_err[:200]


_tidy_explain_lock = threading.Lock()


@app.route('/logs/explain', methods=['POST'])
@login_required
def logs_explain():
    if not _tidy_explain_lock.acquire(blocking=False):
        return jsonify({'ok': False, 'msg': '已有一次解读在进行，请稍候'})
    try:
        data = request.get_json(force=True, silent=True) or {}
        logs = (data.get('logs') or '')[-8000:]
        if not logs.strip():
            return jsonify({'ok': False, 'msg': '日志为空'})
        prompt = ('你是一名运维助手。以下是Hermes Agent Gateway的最新日志。请用通俗中文向不懂技术'
                  '的用户解释：1.整体运行是否正常 2.有没有错误或警告，分别是什么、严重吗、需要处理吗'
                  ' 3.如果一切正常就一句话说明。直接给结论，别贴大段原文。\n\n日志内容：\n' + logs)
        msg = _hermes_explain(prompt)
        return jsonify({'ok': True, 'msg': msg})
    finally:
        _tidy_explain_lock.release()


@app.route('/logs/api')
@login_required
def logs_api():
    try:
        n = int(request.args.get('n', 100))
    except ValueError:
        n = 100
    n = max(20, min(n, 500))
    _, unit = run("systemctl --user list-units --type=service --no-legend 2>/dev/null | grep -E 'hermes-gateway|gateway' | awk '{print $1}' | head -1")
    unit = unit.strip() or 'hermes-gateway'
    _, out = run(f"journalctl --user -u {shlex.quote(unit)} -n {n} --no-pager -o short-iso 2>&1 | tail -n {n}")
    if 'No journal files' in out or not out.strip() or 'No entries' in out:
        _, out2 = run(f"tail -n {n} {shlex.quote(str(HERMES_HOME))}/logs/gateway.log 2>/dev/null")
        out = out2 if out2.strip() else out
    q = request.args.get('q', '')
    level = request.args.get('level', '')
    lines = [ln for _lv, ln in _log_filter_lines(out.splitlines(), q, level)]
    return jsonify({'ok': True, 'lines': lines})


@app.route('/soul', methods=['POST'])
@login_required
def soul():
    content = request.form.get('content', '')
    if len(content) > 100000:
        flash('内容过大（>100KB）', 'err')
    else:
        if SOUL_FILE.exists():
            bak = SOUL_FILE.with_suffix(f'.md.bak-{time.strftime("%Y%m%d%H%M%S")}')
            bak.write_bytes(SOUL_FILE.read_bytes())
        SOUL_FILE.write_text(content, encoding='utf-8')
        flash('SOUL.md 已保存（旧版已备份；下个新会话生效）', 'ok')
    return redirect(url_for('persona'))

@app.route('/persona', methods=['GET', 'POST'])
@login_required
def persona():
    if request.method == 'POST':
        act = request.form.get('act', '')
        if act == 'limits':
            try:
                ml = int(request.form.get('mem_limit', ''))
                ul = int(request.form.get('usr_limit', ''))
                if not (200 <= ml <= 50000) or not (100 <= ul <= 20000):
                    raise ValueError
            except ValueError:
                flash('限制值须为正整数（MEMORY 200~50000，USER 100~20000）', 'err')
                return redirect(url_for('persona'))
            cfg = load_cfg()
            cfg.setdefault('memory', {})
            cfg['memory']['memory_char_limit'] = ml
            cfg['memory']['user_char_limit'] = ul
            save_cfg(cfg)
            flash(f'记忆容量已更新：MEMORY {ml} / USER {ul} 字符（Gateway重启后生效，去「会话通道」页）', 'ok')
            return redirect(url_for('persona'))
    soul_content = SOUL_FILE.read_text(encoding='utf-8') if SOUL_FILE.exists() else ''
    baks = sorted(SOUL_FILE.parent.glob('SOUL.md.bak-*'), reverse=True)[:5]
    mem, mem_chars, _ = _mem_entries('MEMORY.md')
    usr, usr_chars, _ = _mem_entries('USER.md')
    cfg = load_cfg()
    mc = (cfg.get('memory') or {})
    return render_template('persona.html', content=soul_content, baks=[b.name for b in baks],
                           mem=mem, usr=usr, mem_chars=mem_chars, usr_chars=usr_chars,
                           mem_limit=mc.get('memory_char_limit', 4400),
                           usr_limit=mc.get('user_char_limit', 1375),
                           defaults={'mem': 2200, 'usr': 1375})

@app.route('/memory')
@login_required
def memory_page_redirect():
    return redirect(url_for('persona'))

# ---------- 记忆库管理 ----------
MEM_DIR = HERMES_HOME / 'memories'

def _mem_entries(fname):
    f = MEM_DIR / fname
    if not f.exists():
        return [], 0, 0
    text = f.read_text(encoding='utf-8')
    entries = [e.strip() for e in text.split('\n§\n')]
    entries = [e for e in entries if e]
    return entries, len(text), f.stat().st_size



_tidy_locks = {}
_tidy_locks_guard = threading.Lock()

def _tidy_lock(key):
    with _tidy_locks_guard:
        if key not in _tidy_locks:
            _tidy_locks[key] = threading.Lock()
        return _tidy_locks[key]

TIDY_PROMPT = """你是记忆库整理员。下面是 {fname} 的当前内容（各条用单独一行 § 分隔）。

请提炼整理：
1. 删除已过时、被取代、重复表达的条目
2. 合并同类项，精炼措辞，保留所有仍然有效的事实、规则、口径
3. 不确定是否过时的条目一律保留
4. 输出格式：只输出整理后的全文，各条之间用单独一行 § 分隔，不要任何解释、前言、代码块标记

原文：
{content}"""

@app.route('/memory/tidy', methods=['POST'])
@login_required
def memory_tidy():
    """调用本机Hermes Agent提炼整理指定文件，返回对比数据（不写盘，等用户确认）。"""
    import json as _j
    which = request.form.get('which', '')
    fname = {'mem': 'MEMORY.md', 'usr': 'USER.md'}.get(which)
    if not fname:
        return _j.dumps({'ok': False, 'error': '参数错误'}), 400
    f = MEM_DIR / fname
    if not f.exists():
        return _j.dumps({'ok': False, 'error': f'{fname} 不存在'}), 404
    key = 'tidy_' + which
    lock = _tidy_lock(key)
    if not lock.acquire(blocking=False):
        return _j.dumps({'ok': False, 'error': '该文件正在整理中，请稍候'}), 409
    try:
        text = f.read_text(encoding='utf-8')
        entries = [e for e in (x.strip() for x in text.split('\n§\n')) if e]
        prompt = TIDY_PROMPT.format(fname=fname, content=text[:20000])
        reply, _sid = _hermes_ask(prompt, timeout=300)
        new_text = reply.strip()
        # 去掉可能的代码块包裹
        if new_text.startswith('```'):
            new_text = re.sub(r'^```[a-z]*\n?', '', new_text)
            new_text = re.sub(r'\n?```$', '', new_text).strip()
        if not new_text or '§' not in new_text:
            return _j.dumps({'ok': False, 'error': 'Agent返回格式异常，已放弃（原文未动）。返回内容：' + reply[:200]}), 200
        new_entries = [e for e in (x.strip() for x in new_text.split('\n§\n')) if e]
        return _j.dumps({'ok': True, 'old_n': len(entries), 'new_n': len(new_entries),
                         'old_chars': len(text), 'new_chars': len(new_text),
                         'new_text': new_text})
    except subprocess.TimeoutExpired:
        return _j.dumps({'ok': False, 'error': '整理超时（5分钟），已放弃，原文未动'}), 200
    except Exception as e:
        return _j.dumps({'ok': False, 'error': f'整理失败：{str(e)[:200]}（原文未动）'}), 200
    finally:
        lock.release()

@app.route('/memory/tidy_apply', methods=['POST'])
@login_required
def memory_tidy_apply():
    """用户确认后把整理结果写盘（先备份）。"""
    import json as _j
    which = request.form.get('which', '')
    content = request.form.get('content', '')
    fname = {'mem': 'MEMORY.md', 'usr': 'USER.md'}.get(which)
    if not fname or not content or len(content) > 50000:
        return _j.dumps({'ok': False, 'error': '参数错误'}), 400
    f = MEM_DIR / fname
    if f.exists():
        bak = f.with_suffix(f'.md.bak-{time.strftime("%Y%m%d%H%M%S")}')
        bak.write_text(f.read_text(encoding='utf-8'), encoding='utf-8')
    f.write_text(content + '\n', encoding='utf-8')
    return _j.dumps({'ok': True})

@app.route('/memory/delete', methods=['POST'])
@login_required
def memory_delete():
    which = request.form.get('which', '')
    idx = request.form.get('idx', type=int)
    fname = {'mem': 'MEMORY.md', 'usr': 'USER.md'}.get(which)
    if not fname or idx is None:
        flash('参数错误', 'err')
        return redirect(url_for('persona'))
    f = MEM_DIR / fname
    if not f.exists():
        flash('文件不存在', 'err')
        return redirect(url_for('persona'))
    text = f.read_text(encoding='utf-8')
    entries = [e for e in (x.strip() for x in text.split('\n§\n')) if e]
    if idx < 0 or idx >= len(entries):
        flash('编号越界', 'err')
        return redirect(url_for('persona'))
    removed = entries.pop(idx)
    bak = f.with_suffix(f'.md.bak-{time.strftime("%Y%m%d%H%M%S")}')
    bak.write_text(text, encoding='utf-8')
    f.write_text('\n§\n'.join(entries) + ('\n' if entries else ''), encoding='utf-8')
    flash(f'已删除 {fname} 第{idx+1}条（原文备份 {bak.name}）：{removed[:40]}…', 'ok')
    return redirect(url_for('persona'))

@app.route('/memory/edit', methods=['POST'])
@login_required
def memory_edit():
    which = request.form.get('which', '')
    content = request.form.get('content', '')
    fname = {'mem': 'MEMORY.md', 'usr': 'USER.md'}.get(which)
    if not fname or len(content) > 50000:
        flash('参数错误或内容过大', 'err')
        return redirect(url_for('persona'))
    f = MEM_DIR / fname
    if f.exists():
        bak = f.with_suffix(f'.md.bak-{time.strftime("%Y%m%d%H%M%S")}')
        bak.write_text(f.read_text(encoding='utf-8'), encoding='utf-8')
    f.write_text(content, encoding='utf-8')
    flash(f'{fname} 已保存（旧版已备份；§ 分隔各条）', 'ok')
    return redirect(url_for('persona'))

# ---------- 模型分工 ----------
@app.route('/roles', methods=['GET', 'POST'])
@login_required
def roles():
    cfg = load_cfg()
    if request.method == 'POST':
        act = request.form.get('act', '')
        sel = request.form.get('model_sel', '').strip()
        if '::' in sel:
            prov, model = sel.split('::', 1)
        else:
            prov = model = ''
        if not model:
            flash('未选择模型（保持原样）', 'err')
            return redirect(url_for('roles'))
        if act == 'main':
            cfg.setdefault('model', {})
            if model:
                cfg['model']['default'] = model
            if prov:
                cfg['model']['provider'] = prov
            save_cfg(cfg)
            flash('主对话模型已更新（Gateway重启后生效，去「会话通道」页重启）', 'ok')
        elif act == 'delegation':
            cfg.setdefault('delegation', {})
            if prov:
                cfg['delegation']['provider'] = prov
            if model:
                cfg['delegation']['model'] = model
            save_cfg(cfg)
            flash('子agent执行模型已更新（新任务生效，无需重启）', 'ok')
        elif act == 'fast':
            cfg.setdefault('model', {})
            if model:
                cfg['model']['fast'] = model
            else:
                cfg['model'].pop('fast', None)
            save_cfg(cfg)
            flash('快速小活模型已更新' if model else '快速小活模型已清除（跟随主模型）', 'ok')
        return redirect(url_for('roles'))
    m = cfg.get('model') or {}
    d = cfg.get('delegation') or {}
    plist, reg = provider_list()
    pnames = {k: p.get('cn_name') or p.get('name') or k for k, p in reg.items()}
    # 候选模型=控制台登记表+外部扫描（显示为"模型名（服务商中文）"）
    envd, _ = load_env()
    with db() as conn:
        db_models = [dict(r) for r in conn.execute('SELECT * FROM models ORDER BY is_current DESC, created DESC')]
    cands = []
    seen = set()
    for mm in db_models:
        if mm['model_name'] and mm['model_name'] not in seen:
            seen.add(mm['model_name'])
            cands.append({'model': mm['model_name'], 'prov': mm['provider'],
                          'label': f"{mm['model_name']}（{pnames.get(mm['provider'], mm['provider'])}）"})
    for mm in _scan_external_models(db_models, envd, cfg, plist):
        pn = pnames.get(mm['provider'], mm['provider'])
        cur_m = m.get('default', '') if mm['provider'] == m.get('provider') else ''
        if cur_m and cur_m not in seen:
            seen.add(cur_m)
            cands.append({'model': cur_m, 'prov': mm['provider'], 'label': f'{cur_m}（{pn}）'})
    return render_template('roles.html', cur_main=m.get('default', ''), cur_main_prov=m.get('provider', ''),
                           cur_del_model=d.get('model', ''), cur_del_prov=d.get('provider', ''),
                           cur_fast=m.get('fast', ''), pnames=pnames, cands=cands)

# ---------- 定时任务（cron） ----------
CRON_FILE = HERMES_HOME / 'cron' / 'jobs.json'

def _cron_jobs():
    """读jobs.json（只读展示）；Gateway运行中，写操作一律走CLI避免抢锁。"""
    import json as _j
    try:
        data = _j.loads(CRON_FILE.read_text(encoding='utf-8'))
        jobs = data.get('jobs', data) if isinstance(data, dict) else data
    except Exception:
        return []
    out = []
    for jb in jobs:
        state = jb.get('state') or ('paused' if jb.get('paused_at') else 'active')
        out.append({
            'id': jb.get('id', ''),
            'name': jb.get('name') or (jb.get('prompt') or '')[:30],
            'schedule': jb.get('schedule_display') or '',
            'prompt': jb.get('prompt') or '',
            'deliver': jb.get('deliver') or '',
            'model': jb.get('model') or '',
            'provider': jb.get('provider') or '',
            'state': state,
            'next_run': (jb.get('next_run_at') or '')[:16].replace('T', ' '),
            'last_run': (jb.get('last_run_at') or '')[:16].replace('T', ' '),
            'last_status': jb.get('last_status') or '',
        })
    out.sort(key=lambda x: x['next_run'] or '9999')
    return out

def _cron_status():
    rc, out = run(f'{HERMES_BIN} cron status 2>/dev/null', timeout=30)
    running = 'running' in out.lower()
    hb = ''
    m = re.search(r'heartbeat[:\s]+([^\n]+)', out, re.I)
    if m:
        hb = m.group(1).strip()
    return {'running': running, 'detail': out.strip()[:300], 'heartbeat': hb}

@app.route('/cron')
@login_required
def cron():
    return render_template('cron.html', jobs=_cron_jobs(), status=_cron_status())

@app.route('/cron/create', methods=['POST'])
@login_required
def cron_create():
    name = (request.form.get('name') or '').strip()
    prompt = (request.form.get('prompt') or '').strip()
    deliver = (request.form.get('deliver') or '').strip()
    smode = request.form.get('smode', 'daily')   # daily/weekly/interval/once/custom
    if not prompt:
        flash('任务内容必填', 'err')
        return redirect(url_for('cron'))
    once = False
    try:
        if smode == 'daily':
            hh, mm = (request.form.get('at_time') or '07:00').split(':')
            schedule = f'{int(mm)} {int(hh)} * * *'
        elif smode == 'weekly':
            hh, mm = (request.form.get('at_time') or '07:00').split(':')
            days = request.form.getlist('weekdays') or ['1']
            days = ','.join(sorted({d for d in days if d.isdigit()})) or '1'
            schedule = f'{int(mm)} {int(hh)} * * {days}'
        elif smode == 'interval':
            n = (request.form.get('interval') or '').strip()
            unit = request.form.get('interval_unit', 'h')
            if not re.fullmatch(r'\d{1,4}', n) or int(n) < 1 or unit not in ('h', 'm'):
                raise ValueError('间隔须为正整数，单位选小时或分钟')
            schedule = f'every {n}{unit}'
        elif smode == 'once':
            dt = request.form.get('once_at', '')
            m2 = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})', dt)
            if not m2:
                raise ValueError('请选择执行时间')
            Y, Mo, D, hh, mm = m2.groups()
            schedule = f'{int(mm)} {int(hh)} {int(D)} {int(Mo)} *'
            once = True
        else:  # custom
            schedule = (request.form.get('schedule') or '').strip()
            if not schedule:
                raise ValueError('自定义周期不能为空')
            once = request.form.get('once_flag') == '1'
    except Exception as ve:
        flash(f'周期设置有误：{ve}', 'err')
        return redirect(url_for('cron'))
    from shlex import quote as _q
    cmd = f'{HERMES_BIN} cron create {_q(schedule)} {_q(prompt)}'
    if name:
        cmd += f' --name {_q(name)}'
    if deliver:
        cmd += f' --deliver {_q(deliver)}'
    if once:
        cmd += ' --repeat 1'
    rc, out = run(cmd + ' 2>&1', timeout=60)
    if rc == 0:
        flash(f'定时任务已创建：{name or schedule}', 'ok')
    else:
        flash(f'创建失败：{out[:200]}', 'err')
    return redirect(url_for('cron'))

def _cron_job_name(jid):
    """从jobs.json查任务名，查不到返回空（任务可能已删）。"""
    try:
        import json as _json
        data = _json.loads((HERMES_HOME / 'cron' / 'jobs.json').read_text())
        jobs = data.get('jobs', data) if isinstance(data, dict) else data
        for j in jobs:
            if str(j.get('id', '')) == jid:
                return j.get('name', '')
    except Exception:
        pass
    return ''


def _cron_hist_record(job_id, ok, output, duration):
    import datetime as _dt
    try:
        with db() as conn:
            conn.execute('INSERT INTO cron_runs(job_id, job_name, ts, ok, output, duration) VALUES(?,?,?,?,?,?)',
                         (job_id, _cron_job_name(job_id), _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                          1 if ok else 0, (output or '')[:500], round(duration, 1)))
    except Exception:
        pass


@app.route('/cron/history')
@login_required
def cron_history():
    jid = request.args.get('job_id', '')
    try:
        limit = int(request.args.get('limit', 20))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 100))
    with db() as conn:
        if jid:
            rows = [dict(x) for x in conn.execute(
                'SELECT job_id, job_name, ts, ok, output, duration FROM cron_runs WHERE job_id=? ORDER BY id DESC LIMIT ?',
                (jid, limit))]
        else:
            rows = [dict(x) for x in conn.execute(
                'SELECT job_id, job_name, ts, ok, output, duration FROM cron_runs ORDER BY id DESC LIMIT ?', (limit,))]
    # 标注已删除任务
    for r in rows:
        r['deleted'] = (r['job_name'] != '' and _cron_job_name(r['job_id']) == '')
    return jsonify({'ok': True, 'rows': rows})


@app.route('/cron/history/clear', methods=['POST'])
@login_required
def cron_history_clear():
    with db() as conn:
        conn.execute('DELETE FROM cron_runs')
    flash('执行历史已清空', 'ok')
    return redirect(url_for('cron'))


# ---------- 多实例管理（v1：本机多HERMES_HOME） ----------
DEFAULT_HERMES_HOME = str(HERMES_HOME)


def _rebind_hermes_paths(home: str):
    """按home重绑模块级Path常量（多实例切换的读取路径跟随）。"""
    global HERMES_HOME, SOUL_FILE, MEM_DIR, CRON_FILE
    HERMES_HOME = Path(home)
    SOUL_FILE = HERMES_HOME / 'SOUL.md'
    MEM_DIR = HERMES_HOME / 'memories'
    CRON_FILE = HERMES_HOME / 'cron' / 'jobs.json'


@app.before_request
def _inst_before():
    """请求前按session切换HERMES_HOME（v1：接受单worker内微小串扰窗口，文档已注明）。"""
    home = session.get('inst_home')
    target = home if (home and Path(home).exists()) else DEFAULT_HERMES_HOME
    if str(HERMES_HOME) != target:
        os.environ['HERMES_HOME'] = target
        _rebind_hermes_paths(target)


@app.after_request
def _inst_after(resp):
    # 请求后恢复默认，避免长连接/后台线程串环境
    os.environ['HERMES_HOME'] = DEFAULT_HERMES_HOME
    return resp


def _seed_default_instance():
    try:
        with db() as conn:
            n = conn.execute('SELECT COUNT(*) c FROM instances').fetchone()['c']
            if n == 0:
                conn.execute('INSERT INTO instances(name, home, note) VALUES(?,?,?)',
                             ('默认实例', DEFAULT_HERMES_HOME, '安装时自动登记'))
    except Exception:
        pass


def _current_instance():
    with db() as conn:
        home = session.get('inst_home')
        if home:
            row = conn.execute('SELECT * FROM instances WHERE home=?', (home,)).fetchone()
            if row:
                return dict(row)
        row = conn.execute('SELECT * FROM instances ORDER BY id LIMIT 1').fetchone()
        return dict(row) if row else {'id': 0, 'name': '默认实例', 'home': DEFAULT_HERMES_HOME}


@app.route('/instances')
@login_required
def instances_page():
    _seed_default_instance()
    with db() as conn:
        rows = [dict(x) for x in conn.execute('SELECT * FROM instances ORDER BY id')]
    cur = _current_instance()
    # 探测各实例gateway健康（进程是否指向该home粗略判断：config.yaml存在即可用）
    for r in rows:
        r['alive'] = Path(r['home'], 'config.yaml').exists()
    return render_template('instances.html', rows=rows, cur=cur)


@app.route('/instances/add', methods=['POST'])
@login_required
def instances_add():
    name = (request.form.get('name') or '').strip()[:30]
    home = (request.form.get('home') or '').strip()
    if not name or not home.startswith('/') or not Path(home, 'config.yaml').exists():
        flash('需要名称 + 绝对路径（且路径下有config.yaml）', 'err')
        return redirect(url_for('instances_page'))
    try:
        with db() as conn:
            conn.execute('INSERT INTO instances(name, home, note) VALUES(?,?,?)', (name, home, ''))
        flash(f'实例「{name}」已登记', 'ok')
    except Exception as e:
        flash(f'登记失败（路径可能重复）：{e}', 'err')
    return redirect(url_for('instances_page'))


@app.route('/instances/switch/<int:iid>', methods=['POST'])
@login_required
def instances_switch(iid):
    with db() as conn:
        row = conn.execute('SELECT * FROM instances WHERE id=?', (iid,)).fetchone()
    if not row or not Path(row['home']).exists():
        flash('实例不存在或路径无效', 'err')
        return redirect(url_for('instances_page'))
    session['inst_home'] = row['home']
    session['inst_name'] = row['name']
    flash(f'已切换到实例「{row["name"]}」', 'ok')
    return redirect(url_for('dashboard'))


@app.route('/instances/delete/<int:iid>', methods=['POST'])
@login_required
def instances_delete(iid):
    with db() as conn:
        row = conn.execute('SELECT * FROM instances WHERE id=?', (iid,)).fetchone()
        if row and row['home'] == DEFAULT_HERMES_HOME:
            flash('默认实例不可删除', 'err')
            return redirect(url_for('instances_page'))
        conn.execute('DELETE FROM instances WHERE id=?', (iid,))
    if session.get('inst_home') == (row['home'] if row else None):
        session.pop('inst_home', None)
    flash('已删除登记（实例文件不受影响）', 'ok')
    return redirect(url_for('instances_page'))


def _metrics_sample():
    """采一个CPU%/内存%点入库；距上次采样不足60秒则跳过。惰性触发，无后台线程。"""
    try:
        _, stat1 = run("cat /proc/stat | grep '^cpu ' | awk '{print $2+$3+$4, $5}'", timeout=10)
        import time as _t
        _t.sleep(0.4)
        _, stat2 = run("cat /proc/stat | grep '^cpu ' | awk '{print $2+$3+$4, $5}'", timeout=10)
        b1, i1 = (int(x) for x in stat1.split())
        b2, i2 = (int(x) for x in stat2.split())
        busy = b2 - b1
        total = (b2 + i2) - (b1 + i1)
        cpu = round(busy * 100 / total, 1) if total > 0 else 0.0
    except Exception:
        cpu = 0.0
    try:
        _, meminfo = run("grep -E 'MemTotal|MemAvailable' /proc/meminfo | awk '{print $2}'", timeout=10)
        vals = [int(x) for x in meminfo.split()]
        mem = round((vals[0] - vals[1]) * 100 / vals[0], 1) if len(vals) == 2 and vals[0] > 0 else 0.0
    except Exception:
        mem = 0.0
    try:
        _, load1 = run("cat /proc/loadavg | awk '{print $1}'", timeout=10)
        load1v = float(load1.strip() or 0)
    except (ValueError, OSError):
        load1v = 0.0
    now = int(time.time())
    try:
        with db() as conn:
            conn.execute('INSERT OR REPLACE INTO metrics(ts, cpu, mem, load1) VALUES(?,?,?,?)', (now, cpu, mem, load1v))
            conn.execute('DELETE FROM metrics WHERE ts < ?', (now - 48 * 3600,))
    except Exception:
        pass
    return now, cpu, mem, load1v


# ---------- HTTPS一键配（gunicorn直挂TLS + 自签证书） ----------
CERT_DIR = APP_DIR / 'certs'


HTTPS_UNIT_CANDIDATES = [
    Path('/etc/systemd/system/hermes-console.service'),
    Path(os.environ.get('CONSOLE_UNIT_PATH', '/home/ubuntu/.config/systemd/user/hermes-console.service')),
]


def _find_unit():
    for p in HTTPS_UNIT_CANDIDATES:
        if p.exists():
            return p
    return None


def _https_unit_info():
    """读当前systemd unit内容，判断TLS状态与端口。"""
    unit = _find_unit()
    if not unit:
        return {'managed': False}
    txt = unit.read_text()
    tls = '--certfile' in txt
    m = re.search(r'-b 0\.0\.0\.0:(\d+)', txt)
    port = m.group(1) if m else '8787'
    cert = CERT_DIR / 'console.crt'
    key = CERT_DIR / 'console.key'
    return {'managed': True, 'tls': tls, 'port': port,
            'cert_ready': cert.exists() and key.exists(),
            'cert_path': str(cert), 'key_path': str(key)}


@app.route('/https')
@login_required
def https_page():
    return render_template('https.html', info=_https_unit_info())


@app.route('/https/generate', methods=['POST'])
@login_required
def https_generate():
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert = CERT_DIR / 'console.crt'
    key = CERT_DIR / 'console.key'
    ip = run("hostname -I | awk '{print $1}'", timeout=10)[1].strip() or '127.0.0.1'
    rc, out = run(
        f"openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes "
        f"-keyout {shlex.quote(str(key))} -out {shlex.quote(str(cert))} "
        f"-subj '/CN={ip}' -addext 'subjectAltName=IP:{ip}' 2>&1", timeout=60)
    if rc != 0 or not cert.exists():
        return jsonify({'ok': False, 'msg': '证书生成失败：' + out[:200]})
    os.chmod(key, 0o600)
    return jsonify({'ok': True, 'msg': f'自签证书已生成（10年有效，CN={ip}）'})


@app.route('/https/enable', methods=['POST'])
@login_required
def https_enable():
    info = _https_unit_info()
    if not info.get('managed'):
        return jsonify({'ok': False, 'msg': '未找到systemd服务unit（可能前台运行），无法自动切换'})
    if not info.get('cert_ready'):
        return jsonify({'ok': False, 'msg': '请先生成证书'})
    unit = _find_unit()
    txt = unit.read_text()
    if not os.access(unit.parent, os.W_OK) and not run('sudo -n true 2>/dev/null', timeout=10)[0] == 0:
        return jsonify({'ok': False, 'msg': 'unit目录无写权限且sudo不可用，请手动修改'})
    enable = (request.get_json(force=True, silent=True) or {}).get('enable')
    port = info.get('port', '8787')
    # 切回HTTP时要用原始HTTP端口：从.bak或unit注释恢复
    raw_http = None
    bak = unit.with_suffix('.service.bak')
    src_txt = bak.read_text() if bak.exists() else ''
    m0 = re.search(r'-b 0\.0\.0\.0:(\d+)', src_txt)
    if m0:
        raw_http = m0.group(1)
    if raw_http and '--certfile' not in src_txt:
        port = raw_http  # bak是HTTP版，用它
    tls_port = str(int(port) + 1)
    if enable:
        new_exec = (f'{APP_DIR}/venv/bin/gunicorn -w 1 --threads 8 --timeout 700 '
                    f'--certfile {CERT_DIR}/console.crt --keyfile {CERT_DIR}/console.key '
                    f'-b 0.0.0.0:{tls_port} app:app')
        txt2 = re.sub(r'ExecStart=.*', 'ExecStart=' + new_exec, txt, count=1)
    else:
        txt2 = re.sub(r'ExecStart=.*',
                      f'ExecStart={APP_DIR}/venv/bin/gunicorn -w 1 --threads 8 --timeout 700 -b 0.0.0.0:{port} app:app',
                      txt, count=1)
    is_system = str(unit).startswith('/etc/systemd')
    pre = 'sudo -n ' if is_system else ''
    ctl = 'sudo systemctl' if is_system else 'systemctl --user'
    bak = unit.with_suffix('.service.bak')
    if not bak.exists():
        if is_system:
            run(f'{pre}cp {shlex.quote(str(unit))} {shlex.quote(str(bak))}', timeout=15)
        else:
            bak.write_text(txt)
    # 写回：system级走sudo tee
    if is_system:
        run(f"sudo -n tee {shlex.quote(str(unit))} > /dev/null <<'UNIT_EOF'\n{txt2}\nUNIT_EOF", timeout=15)
    else:
        unit.write_text(txt2)
    run(f'{ctl} daemon-reload', timeout=20)
    # restart放后台（把自己杀了也无所谓，前端轮询探活）
    import threading as _th
    def _do_restart():
        run(f'{ctl} restart hermes-console', timeout=30)
    _th.Thread(target=_do_restart, daemon=True).start()
    target = f'https://0.0.0.0:{tls_port}' if enable else f'http://0.0.0.0:{port}'
    return jsonify({'ok': True, 'msg': f'切换指令已发出，约10秒后生效：{target}'})


@app.route('/https/status')
@login_required
def https_status():
    return jsonify(_https_unit_info())


@app.route('/metrics/api')
@login_required
def metrics_api():
    try:
        hours = int(request.args.get('hours', 6))
    except ValueError:
        hours = 6
    hours = max(1, min(hours, 48))
    now = int(time.time())
    # 惰性补采：距上次点>=60秒才采
    with db() as conn:
        row = conn.execute('SELECT MAX(ts) FROM metrics').fetchone()
        last = row[0] if row and row[0] else 0
    if now - last >= 60:
        _metrics_sample()
    with db() as conn:
        rows = [dict(x) for x in conn.execute(
            'SELECT ts, cpu, mem, load1 FROM metrics WHERE ts >= ? ORDER BY ts', (now - hours * 3600,))]
    return jsonify({'ok': True, 'rows': rows, 'hours': hours})


@app.route('/cron/action/<jid>/<act>', methods=['POST'])
@login_required
def cron_action(jid, act):
    if act not in ('pause', 'resume', 'run', 'remove') or not re.fullmatch(r'[0-9a-zA-Z_-]{6,64}', jid):
        abort(400)
    import time as _time
    t0 = _time.time()
    rc, out = run(f'{HERMES_BIN} cron {act} {jid} 2>&1', timeout=60)
    duration = _time.time() - t0
    if act == 'run':
        _cron_hist_record(jid, rc == 0, out, duration)
    if rc == 0:
        flash({'pause': '已暂停', 'resume': '已恢复', 'run': '已触发立即执行（下个调度周期内跑）', 'remove': '已删除'}[act], 'ok')
    else:
        flash(f'操作失败：{out[:200]}', 'err')
    return redirect(url_for('cron'))

# ---------- 会话（桥接Hermes Agent本体：hermes -z + --resume） ----------
_chat_locks = {}
_chat_locks_guard = threading.Lock()

def _chat_lock(chat_id):
    with _chat_locks_guard:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]

def _hermes_ask(prompt, session_id=None, timeout=600):
    """调用 hermes CLI 单轮问答；session_id为空则新建会话。返回(回复, session_id)。"""
    cmd = [str(HERMES_BIN), '-z', prompt, '--pass-session-id']
    if session_id:
        cmd += ['--resume', session_id]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       cwd=str(Path.home()))
    out = (r.stdout or '').strip()
    err = (r.stderr or '').strip()
    if r.returncode != 0:
        raise RuntimeError((err or out or '未知错误')[:500])
    sid = session_id
    m = re.search(r'SESSION[_ ]ID[:\s]+([0-9a-zA-Z_-]+)', out)
    if m:
        sid = m.group(1)
        out = re.sub(r'^.*SESSION[_ ]ID[:\s]+[0-9a-zA-Z_-]+.*$', '', out, flags=re.M).strip()
    return out, sid

def _list_session_ids(limit=8):
    _, out = run(f'{HERMES_BIN} sessions list 2>/dev/null | head -{limit+2}', timeout=30)
    ids = []
    for line in out.strip().splitlines():
        m = re.search(r'(\d{8}_\d{6}_[0-9a-f]{6})', line)
        if m:
            ids.append(m.group(1))
    return ids

def _new_session_id(before_ids):
    """调用后与调用前的session列表对比，找出新建的那个（避免抓错别的会话）。"""
    for sid in _list_session_ids():
        if sid not in before_ids:
            return sid
    # 没有新增（--resume场景）：返回最近活跃的第一个
    ids = _list_session_ids(1)
    return ids[0] if ids else None

@app.route('/chat')
@login_required
def chat():
    with db() as conn:
        chats = [dict(x) for x in conn.execute('SELECT id,title,last_active FROM chats ORDER BY last_active DESC LIMIT 50')]
        cid = request.args.get('id', type=int)
        chat = None; msgs = []
        if cid:
            row = conn.execute('SELECT * FROM chats WHERE id=?', (cid,)).fetchone()
            if row:
                chat = dict(row)
                msgs = [dict(x) for x in conn.execute('SELECT role,content,created FROM chat_msgs WHERE chat_id=? ORDER BY id', (cid,))]
    return render_template('chat.html', chats=chats, chat=chat, msgs=msgs)

UPLOAD_DIR = HERMES_HOME / 'uploads' / 'console'
UPLOAD_MAX = 50 * 1024 * 1024
UPLOAD_EXT_OK = {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.pdf','.txt','.md',
                 '.doc','.docx','.xls','.xlsx','.ppt','.pptx','.csv','.json','.xml',
                 '.zip','.log','.py','.sh','.yaml','.yml','.dwg','.dxf'}

def _save_uploads(chat_id):
    """保存本次请求附带的文件，返回[(文件名,绝对路径,大小)]；超限/类型不符抛ValueError。"""
    saved = []
    for f in request.files.getlist('files'):
        if not f or not f.filename:
            continue
        raw = f.read()
        if len(raw) > UPLOAD_MAX:
            raise ValueError(f'{f.filename} 超过50M限制')
        ext = Path(f.filename).suffix.lower()
        if ext not in UPLOAD_EXT_OK:
            raise ValueError(f'不支持的文件类型 {ext}（可传图片/文档/表格/PDF/压缩包等）')
        d = UPLOAD_DIR / str(chat_id or 'new')
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[^\w.\u4e00-\u9fff-]', '_', Path(f.filename).name)[:80]
        target = d / f'{int(time.time())}_{safe}'
        target.write_bytes(raw)
        os.chmod(target, 0o600)
        saved.append((Path(f.filename).name, str(target), len(raw)))
    return saved

@app.route('/chat/send', methods=['POST'])
@login_required
def chat_send():
    import json as _j
    chat_id = request.form.get('chat_id', type=int)
    text = (request.form.get('text') or '').strip()
    has_files = any(f and f.filename for f in request.files.getlist('files'))
    if (not text and not has_files) or len(text) > 8000:
        return _j.dumps({'ok': False, 'error': '消息为空或超长（>8000字）'}), 400
    now = time.time()
    with db() as conn:
        if chat_id:
            row = conn.execute('SELECT session_id FROM chats WHERE id=?', (chat_id,)).fetchone()
            if not row:
                return _j.dumps({'ok': False, 'error': '会话不存在'}), 404
            sid = row['session_id']
        else:
            sid = None
            cur = conn.execute('INSERT INTO chats(title,session_id,created,last_active) VALUES(?,?,?,?)',
                               (text[:30], None, now, now))
            chat_id = cur.lastrowid
        # 保存附件并把路径注入消息（Agent用read_file/vision工具自行处理）
        att_lines = []
        saved_files = []
        try:
            saved_files = _save_uploads(chat_id)
            for fname, fpath, fsize in saved_files:
                att_lines.append(f'[附件] {fname}（{fsize/1024:.0f}KB）已存到服务器路径：{fpath}')
        except ValueError as ve:
            return _j.dumps({'ok': False, 'error': str(ve), 'chat_id': chat_id}), 400
        full_text = text
        if att_lines:
            has_img = any(Path(p_).suffix.lower() in ('.jpg','.jpeg','.png','.gif','.webp','.bmp')
                          for _, p_, _ in saved_files)
            hint = '（附件含图片，请用视觉工具查看图片内容）' if has_img else ''
            full_text = (text + '\n\n' if text else '') + '\n'.join(att_lines) + f'\n请处理以上附件。{hint}'
        # 界面显示：原文+附件名（不显示服务器路径）；发给Agent：full_text
        display = text
        if saved_files:
            display = (text + '\n' if text else '') + '\n'.join(f'📎 {fn}' for fn, _, _ in saved_files)
        conn.execute('INSERT INTO chat_msgs(chat_id,role,content,created) VALUES(?,?,?,?)',
                     (chat_id, 'user', display, now))
    lock = _chat_lock(chat_id)
    if not lock.acquire(blocking=False):
        return _j.dumps({'ok': False, 'error': '上一条还在处理中，请稍候', 'chat_id': chat_id}), 409
    lock.release()  # 线程内再持锁；此处只做占位检查

    if request.args.get('sync') == '1':
        # 同步旧路径（冒烟/e2e用）
        try:
            before_ids = set(_list_session_ids()) if not sid else set()
            reply, new_sid = _hermes_ask(full_text, sid)
            if not new_sid:
                new_sid = _new_session_id(before_ids)
            with db() as conn:
                conn.execute('INSERT INTO chat_msgs(chat_id,role,content,created) VALUES(?,?,?,?)',
                             (chat_id, 'assistant', reply, time.time()))
                conn.execute('UPDATE chats SET session_id=?, last_active=? WHERE id=?',
                             (new_sid or sid, time.time(), chat_id))
            return _j.dumps({'ok': True, 'chat_id': chat_id, 'reply': reply})
        except subprocess.TimeoutExpired:
            return _j.dumps({'ok': False, 'error': 'Agent响应超时（10分钟），可能任务太重', 'chat_id': chat_id}), 504
        except Exception as e:
            return _j.dumps({'ok': False, 'error': f'调用失败：{str(e)[:300]}', 'chat_id': chat_id})
    # 异步路径：起线程跑，立即返回task_id
    import uuid as _uuid
    task_id = _uuid.uuid4().hex[:16]
    with db() as conn:
        conn.execute('INSERT INTO chat_tasks(id, chat_id, status, started, output, session_id, error) VALUES(?,?,?,?,?,?,?)',
                     (task_id, chat_id, 'running', time.time(), '', sid or '', ''))
    before_ids = set(_list_session_ids()) if not sid else set()

    def _run_task():
        lk = _chat_lock(chat_id)
        if not lk.acquire(blocking=False):
            with db() as conn:
                conn.execute("UPDATE chat_tasks SET status='error', error='该会话已有任务在跑' WHERE id=?", (task_id,))
            return
        try:
            reply, new_sid = _hermes_ask(full_text, sid)
            if not new_sid:
                new_sid = _new_session_id(before_ids)
            with db() as conn:
                conn.execute('INSERT INTO chat_msgs(chat_id,role,content,created) VALUES(?,?,?,?)',
                             (chat_id, 'assistant', reply, time.time()))
                conn.execute('UPDATE chats SET session_id=?, last_active=? WHERE id=?',
                             (new_sid or sid, time.time(), chat_id))
                conn.execute("UPDATE chat_tasks SET status='done', output=?, session_id=? WHERE id=?",
                             (reply[:500], new_sid or sid or '', task_id))
        except subprocess.TimeoutExpired:
            with db() as conn:
                conn.execute("UPDATE chat_tasks SET status='error', error='Agent响应超时（10分钟）' WHERE id=?", (task_id,))
        except Exception as e:
            with db() as conn:
                conn.execute("UPDATE chat_tasks SET status='error', error=? WHERE id=?", (str(e)[:300], task_id))
        finally:
            lk.release()

    threading.Thread(target=_run_task, daemon=True).start()
    return _j.dumps({'ok': True, 'chat_id': chat_id, 'task_id': task_id, 'async': True})

@app.route('/chat/poll')
@login_required
def chat_poll():
    task_id = request.args.get('task_id', '')
    if not re.fullmatch(r'[0-9a-f]{16}', task_id):
        return jsonify({'ok': False, 'error': '参数错误'})
    with db() as conn:
        row = conn.execute('SELECT chat_id, status, output, error, started FROM chat_tasks WHERE id=?', (task_id,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': '任务不存在'})
    started = row['started'] or 0
    elapsed = round(time.time() - started)
    if row['status'] == 'running' and elapsed > 620:
        # 超时兜底：CLI没回但状态还running（worker重启丢线程等）
        with db() as conn:
            conn.execute("UPDATE chat_tasks SET status='error', error='任务超时中断' WHERE id=?", (task_id,))
        return jsonify({'ok': True, 'status': 'error', 'error': '任务超时中断', 'chat_id': row['chat_id'], 'elapsed': elapsed})
    return jsonify({'ok': True, 'status': row['status'], 'reply': row['output'] if row['status'] == 'done' else '',
                    'error': row['error'] or '', 'chat_id': row['chat_id'], 'elapsed': elapsed})


@app.route('/chat/search')
@login_required
def chat_search():
    q = (request.args.get('q') or '').strip()
    if not q or len(q) > 100:
        return jsonify({'ok': True, 'hits': []})
    like = f'%{q}%'
    with db() as conn:
        rows = [dict(x) for x in conn.execute(
            'SELECT m.chat_id, m.role, m.content, m.created, c.title '
            'FROM chat_msgs m LEFT JOIN chats c ON c.id = m.chat_id '
            'WHERE m.content LIKE ? ORDER BY m.created DESC LIMIT 20', (like,))]
    hits = [{'chat_id': r['chat_id'], 'title': r['title'] or '(无标题)', 'role': r['role'],
             'snippet': (r['content'] or '')[:120], 'time': time.strftime('%m-%d %H:%M', time.localtime(r['created']))}
            for r in rows]
    return jsonify({'ok': True, 'hits': hits})


@app.route('/chat/delete/<int:cid>', methods=['POST'])
@login_required
def chat_delete(cid):
    with db() as conn:
        conn.execute('DELETE FROM chat_msgs WHERE chat_id=?', (cid,))
        conn.execute('DELETE FROM chats WHERE id=?', (cid,))
    flash('会话已删除（Hermes侧原始session记录保留）', 'ok')
    return redirect(url_for('chat'))

# ---------- 版本检查/更新 ----------
UPDATE_STATE_FILE = Path(os.environ.get('CONSOLE_DB', str(APP_DIR / 'console.db'))).with_suffix('.update.json')

def _ustate_read():
    import json as _j, time as _time
    try:
        st = _j.loads(UPDATE_STATE_FILE.read_text())
        # 陈旧自愈：running超过45分钟视为进程崩溃残留，自动复位
        if st.get('running') and st.get('ts') and _time.time() - st['ts'] > 2700:
            st = {'running': False, 'stage': '', 'result': '上次操作超时中断，已自动复位', 'ok': False}
            try:
                UPDATE_STATE_FILE.write_text(_j.dumps(st))
            except OSError:
                pass
        return st
    except Exception:
        return {'running': False, 'stage': '', 'result': '', 'ok': None}

def _ustate_write(**kw):
    import json as _j, time as _time, tempfile as _tf
    s = _ustate_read()
    s.update(kw)
    s['ts'] = _time.time()
    try:
        fd, tmp = _tf.mkstemp(dir=str(UPDATE_STATE_FILE.parent), suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            f.write(_j.dumps(s, ensure_ascii=False))
        os.replace(tmp, UPDATE_STATE_FILE)
    except OSError:
        pass

# 状态读写一律走 _ustate_read()/_ustate_write()（多worker安全）

@app.route('/version/check', methods=['POST'])
@login_required
def version_check():
    """只对比官方正式发布版（tag），忽略仓库日常小提交。"""
    import json as _j
    fd = framework_dir()
    if not fd:
        return _j.dumps({'ok': False, 'msg': '未找到框架git目录'}), 400
    local_ver = hermes_version()
    pxy = f'-c http.proxy={HTTP_PROXY} -c https.proxy={HTTP_PROXY} ' if HTTP_PROXY else ''
    rc, out = run(f'git {pxy}-C {fd} ls-remote --tags origin', timeout=45)
    if rc != 0:
        return _j.dumps({'ok': False, 'msg': f'连接官方仓库失败：{out.strip()[:120]}'}), 502
    # 解析tag：v2026.9.24 → (2026,9,24)
    tags = []
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) == 2 and parts[1].startswith('refs/tags/v') and '^{}' not in parts[1]:
            name = parts[1].split('/')[-1].lstrip('v')
            try:
                tags.append((tuple(int(x) for x in name.split('.')), name, parts[0][:12]))
            except ValueError:
                pass
    if not tags:
        return _j.dumps({'ok': False, 'msg': '官方仓库没有发布tag'}), 502
    tags.sort()
    _, latest_ver, latest_sha = tags[-1]
    def vt(s):
        try:
            return tuple(int(x) for x in s.split('.'))
        except ValueError:
            return (0,)
    if vt(local_ver) >= vt(latest_ver):
        _ustate_write(latest=True, target_tag='v' + latest_ver)
        return _j.dumps({'ok': True, 'latest': True, 'local': local_ver, 'remote': latest_ver,
                         'msg': f'✓ 已是官方最新正式版\n当前版本：{local_ver}'}), 200
    _ustate_write(latest=False, target_tag='v' + latest_ver)
    return _j.dumps({'ok': True, 'latest': False, 'local': local_ver, 'remote': latest_ver,
                     'msg': f'发现新正式版！\n当前版本：{local_ver}\n官方最新：{latest_ver}\n可点「更新」升级'}), 200

@app.route('/version/update', methods=['POST'])
@login_required
def version_update():
    import json as _j, threading
    if _ustate_read()['running']:
        return _j.dumps({'started': False, 'msg': '更新已在进行中，请等待完成提示'}), 200
    fd = framework_dir()
    if not fd:
        return _j.dumps({'started': False, 'msg': '未找到框架git目录'}), 400
    if _ustate_read().get('latest') is True:
        return _j.dumps({'started': False, 'msg': '已是最新版本，无需更新'}), 200

    def job():
        try:
            target_tag = _ustate_read().get('target_tag') or ''
            _ustate_write(running=True, stage=f'① 拉取官方正式版（{target_tag or "最新tag"}）…', result='', ok=None)
            pxy = f'-c http.proxy={HTTP_PROXY} -c https.proxy={HTTP_PROXY} ' if HTTP_PROXY else ''
            # 有本地改动先stash保护
            dirty = run(f'git -C {fd} status --porcelain', timeout=15)[1].strip()
            if dirty:
                run(f"git -C {fd} stash push -u -m console-update-$(date +%s)", timeout=60)
            # 若没指定tag，现查最新tag
            if not target_tag:
                rc, out = run(f'git {pxy}-C {fd} ls-remote --tags origin', timeout=45)
                if rc == 0:
                    vers = []
                    for line in out.splitlines():
                        parts = line.split('\t')
                        if len(parts) == 2 and parts[1].startswith('refs/tags/v') and '^{}' not in parts[1]:
                            name = parts[1].split('/')[-1]
                            try:
                                vers.append((tuple(int(x) for x in name.lstrip('v').split('.')), name))
                            except ValueError:
                                pass
                    if vers:
                        target_tag = sorted(vers)[-1][1]
            if not target_tag:
                _ustate_write(running=False, ok=False, result='未找到官方发布tag，更新中止')
                return
            rc, out = run(f'git {pxy}-C {fd} fetch --depth=50 origin tag {target_tag}', timeout=300)
            if rc != 0:
                # 浅fetch失败 → 全量fetch tags
                rc, out = run(f'git {pxy}-C {fd} fetch origin --tags --force', timeout=600)
            if rc != 0:
                _ustate_write(running=False, ok=False, result=f'拉取 {target_tag} 失败：{out.strip()[:300]}')
                return
            rc, out = run(f'git -C {fd} checkout -q {target_tag} && git -C {fd} reset --hard {target_tag}', timeout=120)
            if rc != 0:
                _ustate_write(running=False, ok=False, result=f'切换到 {target_tag} 失败：{out.strip()[:300]}')
                return
            _ustate_write(stage='② 重建依赖环境（hermes pm repair，几分钟）…')
            rc2, out2 = run(f'{HERMES_BIN} pm repair', timeout=1200, proxy=True)
            if rc2 != 0:
                if 'invalid choice' in out2 or 'unrecognized' in out2:
                    # 老版本框架没有pm子命令→退回uv sync
                    uv = Path.home() / '.local/bin/uv'
                    uv = str(uv) if uv.exists() else 'uv'
                    rc2, out2 = run(f'cd {fd} && {uv} sync --extra voice --extra messaging', timeout=1200, proxy=True)
                if rc2 != 0:
                    _ustate_write(running=False, ok=False, result=f'依赖同步失败：{out2.strip()[-300:]}\n可在仪表盘点「🔧 一键修复」重试')
                    return
            _ustate_write(stage='③ 重启 Gateway 并校验…')
            gateway_ctl('restart')
            time.sleep(6)
            gw = gateway_status()
            if gw == 'active':
                _ustate_write(running=False, ok=True, latest=True,
                              result=f'✅ 更新完成！\n当前正式版：{hermes_version()}（{target_tag}）\nGateway：active（已校验运行正常）')
            else:
                rcj, jn = run(f'journalctl --user -u {GATEWAY_SERVICE} -n 5 --no-pager 2>&1 | tail -3', timeout=20)
                _ustate_write(running=False, ok=False,
                              result=f'⚠️ 代码已更新但 Gateway 异常（状态：{gw}）\n日志：{jn.strip()[:200]}\n请点仪表盘「🔧 一键修复」自动处理')
        except Exception as e:
            _ustate_write(running=False, ok=False, result=f'更新异常：{type(e).__name__} {str(e)[:200]}')

    threading.Thread(target=job, daemon=True).start()
    return _j.dumps({'started': True, 'msg': '更新已在后台开始'}), 200

@app.route('/version/repair', methods=['POST'])
@login_required
def version_repair():
    import json as _j, threading
    if _ustate_read()['running']:
        return _j.dumps({'started': False, 'msg': '已有任务在进行中，请等待'}), 200

    def job():
        try:
            _ustate_write(running=True, stage='🔧 ① 重建依赖环境（hermes pm repair）…', result='', ok=None, latest=None)
            rc, out = run(f'{HERMES_BIN} pm repair', timeout=1200, proxy=True)
            if rc != 0 and ('invalid choice' in out or 'unrecognized' in out):
                fd = framework_dir()
                uv = Path.home() / '.local/bin/uv'
                uv = str(uv) if uv.exists() else 'uv'
                rc, out = run(f'cd {fd} && {uv} sync --extra voice --extra messaging', timeout=1200, proxy=True)
            if rc != 0:
                _ustate_write(running=False, ok=False, result=f'修复失败（依赖重建）：{out.strip()[-300:]}')
                return
            _ustate_write(stage='🔧 ② 重启 Gateway 并校验…')
            gateway_ctl('restart')
            time.sleep(6)
            gw = gateway_status()
            if gw == 'active':
                rcv, ver = run(f'{HERMES_BIN} --version 2>&1 | head -1', timeout=30)
                _ustate_write(running=False, ok=True, result=f'✅ 修复完成！\n{ver.strip()}\nGateway：active（已校验运行正常）')
            else:
                rcj, jn = run(f'journalctl --user -u {GATEWAY_SERVICE} -n 8 --no-pager 2>&1 | tail -5', timeout=20)
                _ustate_write(running=False, ok=False, result=f'⚠️ 修复后 Gateway 仍异常（{gw}）\n日志：{jn.strip()[:300]}\n需要人工处理')
        except Exception as e:
            _ustate_write(running=False, ok=False, result=f'修复异常：{type(e).__name__} {str(e)[:200]}')

    threading.Thread(target=job, daemon=True).start()
    return _j.dumps({'started': True, 'msg': '修复已在后台开始'}), 200

@app.route('/version/status')
@login_required
def version_status():
    import json as _j
    return _j.dumps(_ustate_read(), ensure_ascii=False), 200

# ---------- 模型设置 ----------
# 国内服务商分组：(key, 显示名, 分组) 分组=sub订阅版/api普通版/local本地
CN_PROVIDERS = [
    # 订阅版（包月固定价；端点与按量版不同，均已对官方文档核实）
    ('zhipu-coding-cn', '智谱 GLM Coding Plan（国内订阅）', 'sub'),
    ('alibaba-coding-plan-cn', '阿里千问 Coding Plan（国内订阅，key须sk-sp-开头）', 'sub'),
    ('alibaba-token-plan-cn',  '阿里千问 Token Plan（国内订阅）', 'sub'),
    ('tencent-tokenplan',      '腾讯 Token Plan（订阅）', 'sub'),
    ('tencent-tokenhub',       '腾讯 TokenHub（订阅）', 'sub'),
    ('kimi-code-cn',           'Kimi Code 会员订阅（国内）', 'sub'),
    ('minimax-cn',             'MiniMax Coding Plan（国内订阅）', 'sub'),
    ('volcengine-ark-coding',  '火山方舟 Coding Plan（豆包订阅）', 'sub'),
    ('tencent-codingplan',     '腾讯 Coding Plan（混元等订阅）', 'sub'),
    ('qianfan-coding-cn',      '百度千帆 Coding Plan（文心订阅）', 'sub'),
    ('stepfun',                '阶跃星辰 Step Plan（订阅）', 'sub'),
    ('xiaomi',                 '小米 MiMo（订阅）', 'sub'),
    # 普通版（按量付费API）
    ('deepseek',          'DeepSeek（按量）', 'api'),
    ('zhipu-cn',          '智谱 GLM 开放平台（国内按量）', 'api'),
    ('zai',               '智谱 Z.AI 国际版（按量）', 'api'),
    ('alibaba-cn',        '阿里百炼 DashScope（国内按量）', 'api'),
    ('kimi-coding-cn',    'Kimi 开放平台 Moonshot（国内按量）', 'api'),
    ('minimax-cn-openai', 'MiniMax 开放平台（国内按量）', 'api'),
    ('volcengine-ark-cn', '火山方舟（豆包，字节）', 'api'),
    ('hunyuan-cn',        '腾讯混元（按量）', 'api'),
    ('qianfan-cn',        '百度千帆（文心ERNIE，按量）', 'api'),
    # 本地部署
    ('lmstudio',    'LM Studio（本机部署）', 'local'),
]

# 控制台内置扩展（框架注册表没有的国内端点；结构同注册表）
BUILTIN_PROVIDERS = {
    # 智谱订阅版：官方文档 docs.bigmodel.cn —— Coding Plan 必须用专用端点
    # /api/coding/paas/v4；用通用端点 /api/paas/v4 会不扣套餐额度
    'zhipu-coding-cn': {'name': 'Z.AI / GLM', 'cn_name': '智谱 GLM Coding Plan（国内订阅）', 'group': 'sub',
        'env_vars': ['GLM_CODING_API_KEY', 'GLMCODE_API_KEY'],
        'base_url_env': 'GLM_CODING_BASE_URL',
        'base_url': 'https://open.bigmodel.cn/api/coding/paas/v4'},
    # 智谱按量版（资源包/充值余额）：通用端点
    'zhipu-cn': {'name': 'Z.AI / GLM', 'cn_name': '智谱 GLM 开放平台（国内按量）', 'group': 'api',
        'env_vars': ['GLM_API_KEY', 'ZHIPU_API_KEY'],
        'base_url_env': 'GLM_BASE_URL',
        'base_url': 'https://open.bigmodel.cn/api/paas/v4'},
    # Kimi Code 会员订阅：api.kimi.com/coding/v1（kimi.com/coding/docs，按量平台是moonshot.cn）
    'kimi-code-cn': {'name': 'Kimi Code', 'cn_name': 'Kimi Code 会员订阅（国内）', 'group': 'sub',
        'env_vars': ['KIMI_CODE_API_KEY'],
        'base_url_env': 'KIMI_CODE_BASE_URL',
        'base_url': 'https://api.kimi.com/coding/v1'},
    # MiniMax 国内按量（OpenAI兼容）：api.minimaxi.com/v1；订阅版走框架minimax-cn（/anthropic）
    'minimax-cn-openai': {'name': 'MiniMax', 'cn_name': 'MiniMax 开放平台（国内按量）', 'group': 'api',
        'env_vars': ['MINIMAX_API_KEY'],
        'base_url_env': 'MINIMAX_BASE_URL',
        'base_url': 'https://api.minimaxi.com/v1'},
    # 火山方舟（字节豆包）：官方OpenAI兼容端点 ark.cn-beijing.volces.com/api/v3（2026.10.6实测存活）
    'volcengine-ark-cn': {'name': 'Volcengine Ark', 'cn_name': '火山方舟（豆包，字节）', 'group': 'api',
        'env_vars': ['ARK_API_KEY', 'VOLC_API_KEY'],
        'base_url_env': 'ARK_BASE_URL',
        'base_url': 'https://ark.cn-beijing.volces.com/api/v3'},
    # 腾讯混元：官方OpenAI兼容端点 api.hunyuan.cloud.tencent.com/v1（实测存活）
    'hunyuan-cn': {'name': 'Tencent Hunyuan', 'cn_name': '腾讯混元（按量）', 'group': 'api',
        'env_vars': ['HUNYUAN_API_KEY'],
        'base_url_env': 'HUNYUAN_BASE_URL',
        'base_url': 'https://api.hunyuan.cloud.tencent.com/v1'},
    # 百度千帆（文心ERNIE）：官方OpenAI兼容端点 qianfan.baidubce.com/v2（实测存活）
    'qianfan-cn': {'name': 'Baidu Qianfan', 'cn_name': '百度千帆（文心ERNIE，按量）', 'group': 'api',
        'env_vars': ['QIANFAN_API_KEY'],
        'base_url_env': 'QIANFAN_BASE_URL',
        'base_url': 'https://qianfan.baidubce.com/v2'},
    # ===== 订阅版（Coding Plan，官方文档核实，端点均实测存活 2026.10.6）=====
    # 火山方舟Coding Plan：OpenAI协议专用端点 /api/coding/v3；官方警告用 /api/v3 不扣套餐额度、转按量收费
    'volcengine-ark-coding': {'name': 'Volcengine Ark Coding', 'cn_name': '火山方舟 Coding Plan（豆包订阅）', 'group': 'sub',
        'env_vars': ['ARK_CODING_API_KEY'],
        'base_url_env': 'ARK_CODING_BASE_URL',
        'base_url': 'https://ark.cn-beijing.volces.com/api/coding/v3'},
    # 腾讯Coding Plan（含混元HY2.0/GLM-5/K2.5等）：/coding/v3；与按量lkeap端点不互通，key为sk-sp-格式
    'tencent-codingplan': {'name': 'Tencent Coding Plan', 'cn_name': '腾讯 Coding Plan（混元等订阅）', 'group': 'sub',
        'env_vars': ['TENCENT_CODING_API_KEY'],
        'base_url_env': 'TENCENT_CODING_BASE_URL',
        'base_url': 'https://api.lkeap.cloud.tencent.com/coding/v3'},
    # 百度千帆Coding Plan：专属key+专属端点 /v2/coding；禁止用按量key混用（官方错误码401）
    'qianfan-coding-cn': {'name': 'Baidu Qianfan Coding', 'cn_name': '百度千帆 Coding Plan（文心订阅）', 'group': 'sub',
        'env_vars': ['QIANFAN_CODING_API_KEY'],
        'base_url_env': 'QIANFAN_CODING_BASE_URL',
        'base_url': 'https://qianfan.baidubce.com/v2/coding'},
}

def provider_info(provider):
    reg = registry()['providers']
    return reg.get(provider) or BUILTIN_PROVIDERS.get(provider) or {}

def provider_list():
    reg = dict(registry()['providers'])
    reg.update(BUILTIN_PROVIDERS)
    # 本地Ollama（框架oauth类，手工补一个api形态入口走自定义即可，这里只放lmstudio）
    items, seen = [], set()
    for k, label, grp in CN_PROVIDERS:
        if label is None or k in seen or k not in reg:
            continue
        seen.add(k)
        info = dict(reg[k])
        info['cn_name'] = label
        info['group'] = grp
        items.append((k, info))
    # 自定义入口（写custom_providers）
    items.append(('custom', {'cn_name': '自定义（任何OpenAI/Anthropic兼容端点）', 'group': 'custom',
                             'name': '自定义', 'env_vars': [], 'base_url_env': '', 'base_url': ''}))
    return items, reg

@app.route('/model', methods=['GET', 'POST'])
@login_required
def model():
    cfg = load_cfg()
    envd, order = load_env()
    m = cfg.get('model') or {}
    plist, reg = provider_list()
    if request.method == 'POST':
        provider = request.form.get('provider', '').strip()
        model_name = request.form.get('model_name', '').strip()
        api_key = request.form.get('api_key', '').strip()
        base_url = request.form.get('base_url', '').strip()
        if not provider or not model_name:
            flash('请选择服务商并填写模型名', 'err')
        elif provider == 'custom':
            cname = request.form.get('custom_name', '').strip() or 'custom'
            if not base_url:
                flash('自定义服务商必须填 Base URL', 'err')
                return redirect(url_for('model'))
            keyname = f'CUSTOM_{re.sub(r"[^A-Z0-9]", "_", cname.upper())}_API_KEY'
            cps = cfg.get('custom_providers') or []
            if not isinstance(cps, list):
                cps = []
            entry = next((e for e in cps if isinstance(e, dict) and e.get('base_url', '').rstrip('/') == base_url.rstrip('/')), None)
            if entry is None:
                entry = {'name': cname, 'base_url': base_url}
                cps.append(entry)
            entry['model'] = model_name
            entry['key_env'] = keyname
            cfg['custom_providers'] = cps
            cfg.setdefault('model', {})
            cfg['model']['default'] = model_name
            cfg['model']['provider'] = cname
            cfg['model']['base_url'] = base_url
            save_cfg(cfg)
            if api_key:
                envd[keyname] = api_key
                if keyname not in order:
                    order.append(keyname)
                save_env(envd, order)
            _record_model(cname, model_name, base_url, keyname if api_key else '')
            flash(f'自定义服务商「{cname}」已保存（custom_providers+{keyname}）。建议重启Gateway生效。', 'ok')
        else:
            pinfo = provider_info(provider)
            in_fw_reg = provider in registry().get('providers', {})
            builtin_extra = pinfo and not in_fw_reg
            cfg.setdefault('model', {})
            cfg['model']['default'] = model_name
            if builtin_extra:
                # 框架注册表没有的内置扩展服务商：写入custom_providers，Hermes才认识
                real_url = base_url or pinfo.get('base_url', '')
                cname = provider
                keyname = (pinfo.get('env_vars') or ['CUSTOM_API_KEY'])[0]
                cps = cfg.get('custom_providers') or []
                if not isinstance(cps, list):
                    cps = []
                entry = next((e for e in cps if isinstance(e, dict) and e.get('name') == cname), None)
                if entry is None:
                    entry = {'name': cname}
                    cps.append(entry)
                entry['base_url'] = real_url
                entry['model'] = model_name
                entry['key_env'] = keyname
                cfg['custom_providers'] = cps
                cfg['model']['provider'] = cname
                cfg['model']['base_url'] = real_url
            else:
                cfg['model']['provider'] = provider
                if base_url:
                    cfg['model']['base_url'] = base_url
                keyname = (pinfo.get('env_vars') or ['CUSTOM_API_KEY'])[0]
            save_cfg(cfg)
            if api_key:
                envd[keyname] = api_key
                if keyname not in order:
                    order.append(keyname)
                benv = pinfo.get('base_url_env')
                if benv and base_url:
                    envd[benv] = base_url
                    if benv not in order:
                        order.append(benv)
                save_env(envd, order)
            _record_model(provider, model_name, base_url or pinfo.get('base_url', ''),
                          keyname if api_key else '')
            flash('模型配置已保存并生效（原配置备份为 config.yaml.console-bak）。建议到「会话通道」页重启Gateway。', 'ok')
        return redirect(url_for('model'))
    with db() as conn:
        models = [dict(r) for r in conn.execute('SELECT * FROM models ORDER BY is_current DESC, created DESC')]
    models += _scan_external_models(models, envd, cfg, plist)
    # provider id → 中文名映射，模板显示中文
    pnames = {k: p.get('cn_name') or p.get('name') or k for k, p in reg.items()}
    return render_template('model.html', cur=m, plist=plist, reg=reg, models=models, pnames=pnames)

@app.route('/model/models', methods=['POST'])
@login_required
def model_models():
    """读取服务商可用模型列表：GET {base_url}/models，OpenAI兼容；失败再试Anthropic风格。"""
    import requests as rq
    provider = request.form.get('provider', '')
    api_key = request.form.get('api_key', '').strip()
    base_url = request.form.get('base_url', '').strip()
    envd, _ = load_env()
    if provider == 'custom':
        cname = request.form.get('custom_name', '').strip() or 'custom'
        keyname = f'CUSTOM_{re.sub(r"[^A-Z0-9]", "_", cname.upper())}_API_KEY'
        api_key = api_key or envd.get(keyname, '')
    else:
        pinfo = provider_info(provider)
        if not api_key:
            for ev in pinfo.get('env_vars', []):
                if envd.get(ev):
                    api_key = envd[ev]
                    break
        if not base_url and pinfo.get('base_url_env'):
            base_url = envd.get(pinfo['base_url_env'], '')
        if not base_url:
            base_url = pinfo.get('base_url', '')
    import json as _json
    if not base_url:
        return _json.dumps({'ok': False, 'error': '该服务商没有默认地址且未填Base URL'}, ensure_ascii=False)
    if not api_key:
        return _json.dumps({'ok': False, 'error': '无API key（未填且未存过）'}, ensure_ascii=False)
    base = base_url.rstrip('/')
    tries = [
        (base + '/models', {'Authorization': f'Bearer {api_key}'}),
        (base + '/models', {'x-api-key': api_key, 'anthropic-version': '2023-06-01'}),
    ]
    last = ''
    for url, headers in tries:
        try:
            r = rq.get(url, headers=headers, timeout=20)
            if r.status_code == 200:
                d = r.json()
                ids = []
                if isinstance(d, dict) and isinstance(d.get('data'), list):
                    ids = [x.get('id') for x in d['data'] if isinstance(x, dict) and x.get('id')]
                elif isinstance(d, dict) and isinstance(d.get('models'), list):
                    ids = [x.get('id') if isinstance(x, dict) else x for x in d['models']]
                elif isinstance(d, list):
                    ids = [x.get('id') if isinstance(x, dict) else x for x in d]
                ids = [i for i in ids if i]
                if ids:
                    return _json.dumps({'ok': True, 'models': sorted(set(ids))}, ensure_ascii=False)
                last = f'HTTP 200 但没解析出模型列表：{r.text[:150]}'
            else:
                last = f'HTTP {r.status_code}：{r.text[:150]}'
        except Exception as e:
            last = f'{type(e).__name__}: {str(e)[:120]}'
    return _json.dumps({'ok': False, 'error': last}, ensure_ascii=False)

@app.route('/model/test', methods=['POST'])
@login_required
def model_test():
    import requests as rq
    provider = request.form.get('provider', '')
    model_name = request.form.get('model_name', '')
    api_key = request.form.get('api_key', '')
    base_url = request.form.get('base_url', '')
    if not api_key:  # 用已存的key
        envd, _ = load_env()
        pinfo = provider_info(provider)
        keyname = (pinfo.get('env_vars') or ['CUSTOM_API_KEY'])[0]
        api_key = envd.get(keyname, '')
        if pinfo.get('base_url_env'):
            base_url = base_url or envd.get(pinfo['base_url_env'], '')
        if not base_url:
            base_url = pinfo.get('base_url', '')
    if not api_key:
        return '失败：无API key（未填且未存过）', 400
    try:
        if provider.startswith('anthropic') and not base_url:
            r = rq.post('https://api.anthropic.com/v1/messages',
                        headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01'},
                        json={'model': model_name, 'max_tokens': 16, 'messages': [{'role': 'user', 'content': 'ping'}]},
                        timeout=30)
        else:
            url = (base_url or (provider_info(provider).get('base_url') or '')).rstrip('/') + '/chat/completions'
            r = rq.post(url, headers={'Authorization': f'Bearer {api_key}'},
                        json={'model': model_name, 'max_tokens': 16, 'messages': [{'role': 'user', 'content': 'ping'}]},
                        timeout=30)
        if r.status_code == 200:
            return '连通成功 ✓（HTTP 200，模型真实响应）'
        return f'失败：HTTP {r.status_code} — {r.text[:200]}'
    except Exception as e:
        return f'失败：{type(e).__name__} {str(e)[:200]}'

def _scan_external_models(db_models, envd, cfg, plist):
    """扫描外部配置（命令行/配置文件直接配的key），补进已配置模型列表。
    判定：provider 的 env_vars 在 .env 里有值，且该 provider/key 不在控制台登记表中。"""
    known_providers = {m.get('provider') for m in db_models}
    known_keys = {m.get('key_env') for m in db_models if m.get('key_env')}
    cur_prov = (cfg.get('model') or {}).get('provider', '')
    cur_model = (cfg.get('model') or {}).get('default', '')
    cur_url = (cfg.get('model') or {}).get('base_url', '')
    extra = []
    for k, info in plist:
        if k == 'custom' or k in known_providers:
            continue
        keyname = ''
        for ev in info.get('env_vars', []):
            if envd.get(ev):
                keyname = ev
                break
        if not keyname or keyname in known_keys:
            continue
        url = cur_url if k == cur_prov else (envd.get(info.get('base_url_env', ''), '') or info.get('base_url', ''))
        extra.append({'id': None, 'provider': k,
                      'model_name': cur_model if k == cur_prov else '',
                      'base_url': url, 'key_env': keyname,
                      'is_current': 1 if k == cur_prov else 0,
                      'external': True})
    # config.yaml custom_providers 里的外部自定义端点
    for e in cfg.get('custom_providers') or []:
        if not isinstance(e, dict):
            continue
        name = e.get('name', '')
        if not name or name in known_providers:
            continue
        ke = e.get('key_env', '')
        if ke and (not envd.get(ke) or ke in known_keys):
            continue
        extra.append({'id': None, 'provider': name,
                      'model_name': e.get('model', '') or (cur_model if name == cur_prov else ''),
                      'base_url': e.get('base_url', ''), 'key_env': ke,
                      'is_current': 1 if name == cur_prov else 0,
                      'external': True})
    return extra

def _record_model(provider, model_name, base_url, key_env):
    import time as _t
    with db() as conn:
        row = conn.execute('SELECT id FROM models WHERE provider=? AND model_name=?', (provider, model_name)).fetchone()
        if row:
            conn.execute('UPDATE models SET base_url=?, key_env=?, is_current=1 WHERE id=?',
                         (base_url, key_env, row['id']))
        else:
            conn.execute('INSERT INTO models(provider,model_name,base_url,key_env,is_current,created) VALUES(?,?,?,?,1,?)',
                         (provider, model_name, base_url, key_env, _t.time()))
        cur = conn.execute('SELECT id FROM models WHERE provider=? AND model_name=?', (provider, model_name)).fetchone()
        conn.execute('UPDATE models SET is_current=0 WHERE id != ?', (cur['id'],))

# ---------- 已配置模型管理 ----------
@app.route('/model/del_external', methods=['POST'])
@login_required
def model_del_external():
    """真删外部配置：从 .env 移除该provider的API密钥，custom_providers 同名条目一并移除。"""
    provider = request.form.get('provider', '').strip()
    key_env = request.form.get('key_env', '').strip()
    if not provider:
        flash('参数缺失', 'err')
        return redirect(url_for('model'))
    cfg = load_cfg()
    envd, order = load_env()
    plist, reg = provider_list()
    info = reg.get(provider) or {}
    targets = {key_env} if key_env else set()
    targets |= set(info.get('env_vars') or [])
    removed = [k for k in list(envd) if k in targets and envd.get(k)]
    for k in removed:
        del envd[k]
    if removed:
        order = [k for k in order if k in envd]
        save_env(envd, order)
    # custom_providers 同名条目
    cps = cfg.get('custom_providers') or []
    if isinstance(cps, list):
        new_cps = [e for e in cps if not (isinstance(e, dict) and e.get('name') == provider)]
        if len(new_cps) != len(cps):
            cfg['custom_providers'] = new_cps
            removed.append('custom_providers:' + provider)
            save_cfg(cfg)
    # 当前激活的正是它→强提醒
    cur_prov = (cfg.get('model') or {}).get('provider', '')
    if cur_prov == provider:
        flash(f'已删除：{", ".join(removed) or "无匹配项"}。⚠️ config.yaml 当前仍指向该provider，请立即重新选择模型！', 'err')
    else:
        flash(f'已删除：{", ".join(removed) or "无匹配项"}（.env密钥已移除）', 'ok')
    return redirect(url_for('model'))

@app.route('/model/list')
@login_required
def model_list():
    with db() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM models ORDER BY is_current DESC, created DESC')]
    import json as _j
    return _j.dumps(rows, ensure_ascii=False)

@app.route('/model/setcurrent/<int:mid>', methods=['POST'])
@login_required
def model_setcurrent(mid):
    cfg = load_cfg()
    with db() as conn:
        row = conn.execute('SELECT * FROM models WHERE id=?', (mid,)).fetchone()
        if not row:
            flash('记录不存在', 'err')
            return redirect(url_for('model'))
        conn.execute('UPDATE models SET is_current=0')
        conn.execute('UPDATE models SET is_current=1 WHERE id=?', (mid,))
    cfg.setdefault('model', {})
    cfg['model']['default'] = row['model_name']
    cfg['model']['provider'] = row['provider']
    if row['base_url']:
        cfg['model']['base_url'] = row['base_url']
    else:
        cfg.get('model', {}).pop('base_url', None) if isinstance(cfg.get('model'), dict) else None
    save_cfg(cfg)
    flash(f"当前模型已切换为 {row['provider']} / {row['model_name']}（重启Gateway后对话生效）", 'ok')
    return redirect(url_for('model'))

@app.route('/model/delete/<int:mid>', methods=['POST'])
@login_required
def model_delete(mid):
    with db() as conn:
        conn.execute('DELETE FROM models WHERE id=?', (mid,))
    flash('已删除该模型记录（不影响已存密钥）', 'ok')
    return redirect(url_for('model'))

# ---------- 会话通道 ----------
PLATFORM_KEYS = ['weixin', 'wechat', 'dingtalk', 'telegram', 'discord', 'slack',
                 'whatsapp', 'signal', 'email', 'matrix', 'qqbot', 'feishu', 'lark']

# 国内平台定义（按Hermes框架真实env变量整理）
CN_PLATFORMS = {
  'weixin': {'label': '微信（个人号 iLink）', 'emoji': '💬', 'order': 1,
    'instructions': ['1. 下方填 iLink 账户ID（bot注册后获得）', '2. 保存后重启Gateway，按日志提示扫码配对', '3. 也可SSH执行 hermes gateway setup 走交互向导'],
    'vars': [{'name': 'WEIXIN_ACCOUNT_ID', 'prompt': 'iLink 账户ID', 'password': False, 'help': 'WEIXIN_ACCOUNT_ID'},
             {'name': 'WEIXIN_TOKEN', 'prompt': 'Token（如有）', 'password': True, 'help': 'WEIXIN_TOKEN'}]},
  'dingtalk': {'label': '钉钉（企业机器人）', 'emoji': '📌', 'order': 2,
    'instructions': ['1. 钉钉开放平台 open-dev.dingtalk.com 创建企业内部应用', '2. 应用能力→机器人→获取 Client ID 与 Client Secret', '3. 填入下方保存，重启Gateway生效'],
    'vars': [{'name': 'DINGTALK_CLIENT_ID', 'prompt': 'Client ID (AppKey)', 'password': False, 'help': ''},
             {'name': 'DINGTALK_CLIENT_SECRET', 'prompt': 'Client Secret (AppSecret)', 'password': True, 'help': ''},
             {'name': 'DINGTALK_HOME_CHANNEL', 'prompt': '默认频道ID（可空）', 'password': False, 'help': ''}]},
  'feishu': {'label': '飞书（企业机器人）', 'emoji': '🕊️', 'order': 3,
    'instructions': ['1. 飞书开放平台 open.feishu.cn 创建企业自建应用', '2. 凭证与基础信息里复制 App ID / App Secret', '3. 事件订阅开启加密时填 Encrypt Key 与 Verification Token'],
    'vars': [{'name': 'FEISHU_APP_ID', 'prompt': 'App ID', 'password': False, 'help': ''},
             {'name': 'FEISHU_APP_SECRET', 'prompt': 'App Secret', 'password': True, 'help': ''},
             {'name': 'FEISHU_VERIFICATION_TOKEN', 'prompt': 'Verification Token（可空）', 'password': True, 'help': ''},
             {'name': 'FEISHU_ENCRYPT_KEY', 'prompt': 'Encrypt Key（可空）', 'password': True, 'help': ''}]},
  'wecom': {'label': '企业微信', 'emoji': '🏢', 'order': 4,
    'instructions': ['1. 企业微信管理后台创建自建应用', '2. 填 Corp ID / Agent ID / Secret；回调模式需配 Callback 三件套'],
    'vars': [{'name': 'WECOM_CALLBACK_CORP_ID', 'prompt': 'Corp ID（企业ID）', 'password': False, 'help': ''},
             {'name': 'WECOM_CALLBACK_AGENT_ID', 'prompt': 'Agent ID', 'password': False, 'help': ''},
             {'name': 'WECOM_CALLBACK_CORP_SECRET', 'prompt': 'Corp Secret', 'password': True, 'help': ''},
             {'name': 'WECOM_BOT_ID', 'prompt': 'Bot ID（可空）', 'password': False, 'help': ''}]},
  'qqbot': {'label': 'QQ 官方机器人', 'emoji': '🐧', 'order': 5,
    'instructions': ['1. q.qq.com 开发者平台创建机器人', '2. 复制 AppID 与 Client Secret 填入'],
    'vars': [{'name': 'QQ_APP_ID', 'prompt': 'App ID', 'password': False, 'help': ''},
             {'name': 'QQ_CLIENT_SECRET', 'prompt': 'Client Secret', 'password': True, 'help': ''},
             {'name': 'QQ_ALLOWED_USERS', 'prompt': '允许的用户（逗号分隔，可空）', 'password': False, 'help': ''},
             {'name': 'QQ_HOME_CHANNEL', 'prompt': '默认频道（可空）', 'password': False, 'help': ''}]},
  'yuanbao': {'label': '腾讯元宝', 'emoji': '💎', 'order': 6,
    'instructions': ['填入元宝应用的 App ID 与 App Secret'],
    'vars': [{'name': 'YUANBAO_APP_ID', 'prompt': 'App ID', 'password': False, 'help': ''},
             {'name': 'YUANBAO_APP_SECRET', 'prompt': 'App Secret', 'password': True, 'help': ''}]},
}

# 海外/其他平台折叠在"更多平台"里（默认不展开）
OTHER_PLATFORMS = {
  'telegram': {'label': 'Telegram', 'emoji': '✈️', 'order': 90,
    'instructions': ['1. 在Telegram里找 @BotFather，发 /newbot 创建机器人', '2. 复制Bot Token粘贴到下方'],
    'vars': [{'name': 'TELEGRAM_BOT_TOKEN', 'prompt': 'Bot Token', 'password': True, 'help': ''}]},
  'discord': {'label': 'Discord', 'emoji': '🎮', 'order': 91,
    'instructions': ['discord.com/developers 创建Application→Bot，复制Token'],
    'vars': [{'name': 'DISCORD_BOT_TOKEN', 'prompt': 'Bot Token', 'password': True, 'help': ''}]},
}

def all_platforms():
    plats = dict(CN_PLATFORMS)
    plats.update(OTHER_PLATFORMS)
    # 框架注册表里有vars定义的平台补充进来（不覆盖国内定制）
    for k, v in registry()['platforms'].items():
        if k not in plats and v.get('vars'):
            plats[k] = dict(v); plats[k]['order'] = 95
    return dict(sorted(plats.items(), key=lambda kv: kv[1].get('order', 99)))



@app.route('/channels')
@login_required
def channels():
    cfg = load_cfg()
    envd, _ = load_env()
    plats = all_platforms()
    # 已配置=有token_var对应env或config里有平台段
    status = {}
    for k, p in plats.items():
        tv = p.get('token_var', '')
        envset = any(envd.get(v['name']) for v in p.get('vars', [])) if p.get('vars') else bool(tv and envd.get(tv))
        cfgset = str(k).lower() in [str(x).lower() for x in cfg]
        status[k] = 'on' if (envset or cfgset) else 'off'
    envmask = {v['name']: mask(envd.get(v['name'], ''), 3) for k2, pp in plats.items() for v in pp.get('vars', []) if envd.get(v['name'])}
    rc, log = run(f'journalctl --user -u {GATEWAY_SERVICE} -n 60 --no-pager 2>&1 | tail -60', timeout=20)
    return render_template('channels.html', gw=gateway_status(), plats=plats, status=status, log=log, envmask=envmask)

@app.route('/channels/save', methods=['POST'])
@login_required
def channels_save():
    key = request.form.get('platform', '')
    plats = all_platforms()
    p = plats.get(key)
    if not p:
        abort(400)
    envd, order = load_env()
    filled = 0
    for v in p.get('vars', []):
        val = request.form.get(v['name'], '').strip()
        if val:
            envd[v['name']] = val
            if v['name'] not in order:
                order.append(v['name'])
            filled += 1
    tv = p.get('token_var')
    if tv and tv not in envd and p.get('vars'):
        first = request.form.get(p['vars'][0]['name'], '').strip()
        if first:
            envd[tv] = first
            if tv not in order:
                order.append(tv)
    if filled:
        save_env(envd, order)
        flash(f'{p.get("label", key)} 已保存{filled}项配置（写入.env，重启Gateway后生效）', 'ok')
    else:
        flash('没有填写任何值', 'err')
    return redirect(url_for('channels'))

@app.route('/channels/ctl', methods=['POST'])
@login_required
def channels_ctl():
    action = request.form.get('action', '')
    rc, out = gateway_ctl(action)
    flash(f'gateway {action}: {"成功" if rc == 0 else "失败 " + out[:200]}', 'ok' if rc == 0 else 'err')
    return redirect(url_for('channels'))

# ---------- 技能中文描述 ----------
# 机制①：技能目录SKILL.md frontmatter里写 description_zh → 自动显示（对新增技能最自然）
# 机制②：控制台内置词典 skill_zh.json（path→中文），启动时同步进DB
# 机制③：页面上手工编辑（存DB skill_zh表），优先级最高
ZH_DICT_PATH = Path(__file__).resolve().parent / 'skill_zh.json'

def _load_zh_dict():
    import json as _j
    try:
        return _j.loads(ZH_DICT_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}

def sync_zh_dict():
    d = _load_zh_dict()
    with db() as conn:
        for path, zh in d.items():
            row = conn.execute('SELECT desc_zh, src_hash FROM skill_zh WHERE path=?', (path,)).fetchone()
            if row and row['src_hash'] == 'manual':
                continue  # 手工编辑过，不被词典覆盖
            conn.execute('INSERT INTO skill_zh(path,desc_zh,src_hash) VALUES(?,?,"dict") '
                         'ON CONFLICT(path) DO UPDATE SET desc_zh=excluded.desc_zh', (path, zh))

def zh_desc(rel_path, desc_en):
    with db() as conn:
        row = conn.execute('SELECT desc_zh FROM skill_zh WHERE path=?', (rel_path,)).fetchone()
        return row['desc_zh'] if row else ''

@app.route('/skills/zh', methods=['POST'])
@login_required
def skills_zh_save():
    rel = request.form.get('path', '')
    zh = request.form.get('desc_zh', '').strip()
    if not rel or '..' in rel:
        abort(400)
    with db() as conn:
        if zh:
            conn.execute('INSERT INTO skill_zh(path,desc_zh,src_hash) VALUES(?,?,"manual") '
                         'ON CONFLICT(path) DO UPDATE SET desc_zh=excluded.desc_zh, src_hash="manual"', (rel, zh))
        else:
            conn.execute('DELETE FROM skill_zh WHERE path=?', (rel,))
    flash('中文描述已更新' if zh else '已清除中文描述', 'ok')
    return redirect(url_for('skills'))

# ---------- 技能管理 ----------
def parse_skill_md(p):
    try:
        t = Path(p).read_text(encoding='utf-8', errors='replace')
        m = re.match(r'^---\n(.*?)\n---', t, re.S)
        name = desc = desc_zh = ''
        if m:
            fm = yaml.load(m.group(1)) or {}
            name = str(fm.get('name', ''))
            desc = str(fm.get('description', ''))
            desc_zh = str(fm.get('description_zh', '') or '')
        return name, desc, desc_zh, t
    except Exception:
        return '', '', '', ''

@app.route('/skills')
@login_required
def skills():
    sdir = HERMES_HOME / 'skills'
    ddir = HERMES_HOME / 'skills-disabled'
    items = []
    for base, on in ((sdir, True), (ddir, False)):
        if not base.exists():
            continue
        for d in sorted(base.rglob('SKILL.md')):
            name, desc, desc_zh_fm, _ = parse_skill_md(d)
            rel = str(d.parent.relative_to(base))
            zh = desc_zh_fm or zh_desc(rel, desc)          # frontmatter优先，其次DB
            items.append({'path': rel, 'name': name or rel,
                          'desc': (zh or desc)[:160], 'en': desc[:160], 'zh': zh, 'on': on})
    return render_template('skills.html', items=items)

@app.route('/skills/toggle', methods=['POST'])
@login_required
def skills_toggle():
    rel = request.form.get('path', '')
    on = request.form.get('on') == '1'
    sdir, ddir = HERMES_HOME / 'skills', HERMES_HOME / 'skills-disabled'
    src = (sdir if on else ddir) / rel
    dst = (ddir if on else sdir) / rel
    if not src.exists() or '..' in rel:
        abort(400)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    flash(f'技能 {rel} 已{"禁用" if on else "启用"}', 'ok')
    return redirect(url_for('skills'))

@app.route('/skills/view')
@login_required
def skills_view():
    rel = request.args.get('path', '')
    if '..' in rel:
        abort(400)
    for base in (HERMES_HOME / 'skills', HERMES_HOME / 'skills-disabled'):
        p = (base / rel / 'SKILL.md')
        try:
            if not p.resolve().is_relative_to(base.resolve()):
                abort(400)
        except (OSError, ValueError):
            abort(400)
        if p.exists():
            _, _, _, t = parse_skill_md(p)
            return render_template('skill_view.html', path=rel, text=t)
    abort(404)

@app.route('/skills/upload', methods=['POST'])
@login_required
def skills_upload():
    f = request.files.get('zip')
    name = request.form.get('name', '').strip()
    if not f or not name or not re.match(r'^[a-z0-9][a-z0-9_-]{1,63}$', name):
        flash('需要zip文件+合法技能名（小写字母数字-_，2~64位）', 'err')
        return redirect(url_for('skills'))
    target = HERMES_HOME / 'skills' / name
    if target.exists():
        flash(f'技能 {name} 已存在', 'err')
        return redirect(url_for('skills'))
    try:
        zf = zipfile.ZipFile(io.BytesIO(f.read()))
        bad = [n for n in zf.namelist() if n.startswith('/') or '..' in n]
        if bad:
            flash('zip内含非法路径，拒绝', 'err')
            return redirect(url_for('skills'))
        target.mkdir(parents=True)
        import sys as _sys
        if _sys.version_info >= (3, 12, 5) or _sys.version_info >= (3, 13):
            zf.extractall(target, filter='data')
        else:
            zf.extractall(target)
        # 若解压后只有一层目录，上提
        subs = list(target.iterdir())
        if len(subs) == 1 and subs[0].is_dir() and not (target / 'SKILL.md').exists():
            for item in subs[0].iterdir():
                shutil.move(str(item), str(target / item.name))
            subs[0].rmdir()
        if not (target / 'SKILL.md').exists():
            shutil.rmtree(target)
            flash('zip里没有SKILL.md，已回滚', 'err')
        else:
            flash(f'技能 {name} 上传成功', 'ok')
    except Exception as e:
        flash(f'上传失败：{e}', 'err')
    return redirect(url_for('skills'))

# ---------- 备份还原 ----------
@app.route('/backups')
@login_required
def backups():
    items = []
    for f in sorted(BACKUP_DIR.glob('*.tar.gz'), reverse=True):
        st = f.stat()
        items.append({'name': f.name, 'size': f'{st.st_size / 1048576:.1f}M',
                      'kind': '全量' if f.name.startswith('hermes_full') else ('普通' if f.name.startswith('hermes_backup') else '系统'),
                      'time': time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))})
    return render_template('backups.html', items=items, home=str(HERMES_HOME),
                           excludes=sorted(BACKUP_EXCLUDES))

@app.route('/backups/create', methods=['POST'])
@login_required
def backups_create():
    kind = request.form.get('kind', 'normal')   # normal=排除缓存快而小；full=完整无排除
    ts = time.strftime('%Y%m%d_%H%M%S')
    prefix = 'hermes_full' if kind == 'full' else 'hermes_backup'
    out = BACKUP_DIR / f'{prefix}_{ts}.tar.gz'
    # 备份包自身永远排除：BACKUP_DIR若在HERMES_HOME内则跳过，
    # 以及HERMES_HOME里任何 hermes_backup_*/hermes_full_*/pre_restore_* 的tar.gz
    def _is_backup_pkg(path: Path):
        try:
            if BACKUP_DIR.resolve() in path.resolve().parents or path.resolve() == BACKUP_DIR.resolve():
                return True
        except OSError:
            pass
        n = path.name
        return n.endswith('.tar.gz') and n.startswith(('hermes_backup_', 'hermes_full_', 'pre_restore_'))

    def flt(ti):
        p_ = Path(ti.name)
        if _is_backup_pkg(p_):
            return None
        if kind == 'full':
            return ti
        parts = p_.parts
        return None if (len(parts) > 1 and parts[1] in BACKUP_EXCLUDES) else ti
    try:
        with tarfile.open(out, 'w:gz') as tar:
            tar.add(HERMES_HOME, arcname='.hermes', filter=flt)
        label = '全量备份' if kind == 'full' else '普通备份'
        flash(f'{label}完成：{out.name}（{out.stat().st_size / 1048576:.1f}M）', 'ok')
    except Exception as e:
        flash(f'备份失败：{e}', 'err')
    return redirect(url_for('backups'))

@app.route('/backups/download/<name>')
@login_required
def backups_download(name):
    p = BACKUP_DIR / name
    if not p.exists() or not name.endswith('.tar.gz') or '..' in name:
        abort(404)
    if p.resolve().parent != BACKUP_DIR.resolve():
        abort(404)
    return send_file(p, as_attachment=True)

@app.route('/backups/restore/<name>', methods=['POST'])
@login_required
def backups_restore(name):
    p = BACKUP_DIR / name
    if not p.exists() or '..' in name:
        abort(404)
    if request.form.get('confirm') != 'RESTORE':
        flash('还原需在确认框输入 RESTORE', 'err')
        return redirect(url_for('backups'))
    try:
        # 1. 还原前先给当前状态留一份保险备份
        pre = BACKUP_DIR / f'pre_restore_{time.strftime("%Y%m%d_%H%M%S")}.tar.gz'
        def _flt_pre(ti):
            pp = Path(ti.name)
            try:
                if BACKUP_DIR.resolve() in pp.resolve().parents:
                    return None
            except OSError:
                pass
            if pp.name.endswith('.tar.gz') and pp.name.startswith(('hermes_backup_', 'hermes_full_', 'pre_restore_')):
                return None
            return ti
        with tarfile.open(pre, 'w:gz') as tar:
            tar.add(HERMES_HOME, arcname='.hermes', filter=_flt_pre)
        # 2. 停gateway
        gateway_ctl('stop')
        # 3. 解包覆盖
        with tarfile.open(p, 'r:gz') as tar:
            tar.extractall(HERMES_HOME.parent, filter='data')
        # 4. 起gateway
        time.sleep(1)
        gateway_ctl('start')
        flash(f'还原完成（还原前状态已存为 {pre.name}）', 'ok')
    except Exception as e:
        gateway_ctl('start')
        flash(f'还原失败：{e}', 'err')
    return redirect(url_for('backups'))

@app.route('/backups/delete/<name>', methods=['POST'])
@login_required
def backups_delete(name):
    p = BACKUP_DIR / name
    if p.exists() and name.endswith('.tar.gz') and '..' not in name:
        p.unlink()
        flash(f'已删除 {name}', 'ok')
    return redirect(url_for('backups'))

def seed_current_model():
    """把config.yaml当前生效的model种进models表（已存在则跳过）"""
    cfg = load_cfg()
    m = cfg.get('model') or {}
    prov, name = m.get('provider', ''), m.get('default', '')
    if not prov or not name:
        return
    import time as _t
    with db() as conn:
        row = conn.execute('SELECT id FROM models WHERE provider=? AND model_name=?', (prov, name)).fetchone()
        if not row:
            conn.execute('INSERT INTO models(provider,model_name,base_url,key_env,is_current,created) VALUES(?,?,?,?,1,?)',
                         (prov, name, m.get('base_url', ''), '', _t.time()))
            conn.execute('UPDATE models SET is_current=0 WHERE id != last_insert_rowid()')
        elif not conn.execute('SELECT id FROM models WHERE is_current=1').fetchone():
            conn.execute('UPDATE models SET is_current=1 WHERE id=?', (row['id'],))

init_db()
sync_zh_dict()
seed_current_model()
def _cleanup_stale_tasks():
    """启动时把running态的chat_tasks置失败（worker重启丢线程的残留）。"""
    try:
        with db() as conn:
            conn.execute("UPDATE chat_tasks SET status='error', error='服务重启中断，请重发' WHERE status='running'")
    except Exception:
        pass

_cleanup_stale_tasks()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8787, debug=False)
