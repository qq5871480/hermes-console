#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes Console — 轻量级 Hermes Agent 管理控制台
功能：模型设置 / 会话通道 / 技能管理 / 备份还原 / 仪表盘
设计目标：通用（HERMES_HOME环境变量驱动，可管理任何Hermes实例）、可开源。
"""
import os
import re
import io
import sqlite3
import hashlib
import secrets
import subprocess
import tarfile
import time
import shutil
import zipfile
from pathlib import Path
from functools import wraps

from flask import (Flask, request, session, redirect, url_for,
                   render_template, flash, send_file, abort)
from ruamel.yaml import YAML

# ---------- 配置（全部环境变量驱动，无硬编码个人信息） ----------
HERMES_HOME = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
BACKUP_DIR = Path(os.environ.get('CONSOLE_BACKUP_DIR', str(Path.home() / 'hermes-console-backups')))
APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('CONSOLE_DB', str(APP_DIR / 'console.db')))
INIT_PASSWORD = os.environ.get('CONSOLE_INIT_PASSWORD', '')
if not INIT_PASSWORD:
    # 未指定时随机生成一次并落盘（600权限），保证多worker共用同一个初始密码
    _pwf = Path(os.environ.get('CONSOLE_DB', str(APP_DIR / 'console.db'))).with_suffix('.initpw')
    try:
        if _pwf.exists():
            INIT_PASSWORD = _pwf.read_text(encoding='utf-8').strip()
        if not INIT_PASSWORD:
            INIT_PASSWORD = 'hc-' + secrets.token_hex(8)
            _pwf.write_text(INIT_PASSWORD, encoding='utf-8')
            os.chmod(_pwf, 0o600)
        print(f'[hermes-console] 初始密码（首次登录用，登录后强制修改）: {INIT_PASSWORD}')
    except Exception:
        INIT_PASSWORD = 'hc-' + secrets.token_hex(8)
        print(f'[hermes-console] 初始密码（首次登录用，登录后强制修改）: {INIT_PASSWORD}')

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
app.secret_key = os.environ.get('CONSOLE_SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 技能zip上传上限100M

BACKUP_EXCLUDES = {'cache', 'audio_cache', 'tmp', 'backups', '__pycache__',
                   'venv', 'node_modules', 'browser_data', 'image_cache', 'tools', 'logs'}

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
        conn.execute('''CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT UNIQUE, pw_hash TEXT,
            salt TEXT, must_change INTEGER DEFAULT 1,
            login_fails INTEGER DEFAULT 0, locked_until REAL DEFAULT 0)''')
        if not conn.execute('SELECT id FROM users WHERE username="admin"').fetchone():
            salt = secrets.token_hex(16)
            conn.execute('INSERT OR IGNORE INTO users(username,pw_hash,salt,must_change) VALUES(?,?,?,1)',
                         ('admin', hashlib.pbkdf2_hmac('sha256', INIT_PASSWORD.encode(), salt.encode(), 200000).hex(), salt))

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
@app.route('/login', methods=['GET', 'POST'])
def login():
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
        elif new1 == INIT_PASSWORD:
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
    sdir = HERMES_HOME / 'skills'
    n_skills = len(list(sdir.rglob('SKILL.md'))) if sdir.exists() else 0
    cfg = load_cfg()
    m = cfg.get('model') or {}
    envd, _ = load_env()
    n_keys = sum(1 for k in envd if ('KEY' in k or 'TOKEN' in k) and envd.get(k))
    return render_template('dashboard.html', ver=hermes_version(), gw=gateway_status(),
                           disk=disk.strip(), mem=mem.strip(), home=str(HERMES_HOME),
                           n_skills=n_skills, n_backups=len(list(BACKUP_DIR.glob('*.tar.gz'))),
                           model_default=m.get('default', '（未设置）'), model_provider=m.get('provider', '（未设置）'),
                           n_keys=n_keys)

# ---------- 版本检查/更新 ----------
UPDATE_STATE_FILE = Path(os.environ.get('CONSOLE_DB', str(APP_DIR / 'console.db'))).with_suffix('.update.json')

def _ustate_read():
    import json as _j
    try:
        return _j.loads(UPDATE_STATE_FILE.read_text())
    except Exception:
        return {'running': False, 'stage': '', 'result': '', 'ok': None}

def _ustate_write(**kw):
    import json as _j
    s = _ustate_read()
    s.update(kw)
    UPDATE_STATE_FILE.write_text(_j.dumps(s, ensure_ascii=False))

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
            url = (base_url or dict((p[0], p[2]) for p in KNOWN_PROVIDERS).get(provider, '')).rstrip('/') + '/chat/completions'
            r = rq.post(url, headers={'Authorization': f'Bearer {api_key}'},
                        json={'model': model_name, 'max_tokens': 16, 'messages': [{'role': 'user', 'content': 'ping'}]},
                        timeout=30)
        if r.status_code == 200:
            return '连通成功 ✓（HTTP 200，模型真实响应）'
        return f'失败：HTTP {r.status_code} — {r.text[:200]}'
    except Exception as e:
        return f'失败：{type(e).__name__} {str(e)[:200]}'

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
        p = base / rel / 'SKILL.md'
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
    def flt(ti):
        if kind == 'full':
            return ti
        parts = Path(ti.name).parts
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
        with tarfile.open(pre, 'w:gz') as tar:
            tar.add(HERMES_HOME, arcname='.hermes')
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
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8787, debug=False)
