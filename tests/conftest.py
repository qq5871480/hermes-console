"""pytest公共夹具：假HERMES_HOME + 登录态client。零CLI依赖（桩掉外部调用）。"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def make_fake_home(tmp: Path, with_config=True, with_jobs=True) -> Path:
    home = tmp / '.hermes'
    (home / 'cron').mkdir(parents=True, exist_ok=True)
    (home / 'memories').mkdir(parents=True, exist_ok=True)
    (home / 'skills').mkdir(parents=True, exist_ok=True)
    if with_config:
        (home / 'config.yaml').write_text(
            'model:\n  default: mimo-test\n  provider: xiaomi\n'
            'memory:\n  memory_char_limit: 4400\n  user_char_limit: 1375\n')
    if with_jobs:
        (home / 'cron' / 'jobs.json').write_text(json.dumps(
            {'jobs': [{'id': 'abc123def456', 'name': '测试任务', 'schedule_display': '每天 07:30'}]}))
    (home / 'SOUL.md').write_text('测试人格' * 10)
    (home / 'memories' / 'MEMORY.md').write_text('条目一\n§\n条目二')
    (home / 'memories' / 'USER.md').write_text('')
    return home


@pytest.fixture()
def client(tmp_path, monkeypatch):
    home = make_fake_home(tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('CONSOLE_DB', str(tmp_path / 'test.db'))
    monkeypatch.setenv('CONSOLE_BACKUP_DIR', str(tmp_path / 'backups'))

    import app as A
    A.HERMES_HOME = home
    if hasattr(A, '_rebind_hermes_paths'):
        A._rebind_hermes_paths(str(home))
    A.init_db()
    # 预置admin（模拟已完成首次设置）
    import hashlib as _hl, secrets as _sc
    salt = _sc.token_hex(16)
    dk = _hl.pbkdf2_hmac('sha256', b'test-pass-123', bytes.fromhex(salt), 200000).hex()
    with A.db() as conn:
        conn.execute('INSERT OR IGNORE INTO users(username,pw_hash,salt,must_change) VALUES(?,?,?,0)',
                     ('admin', dk, salt))

    # 桩外部世界：run()记录不执行；_hermes_ask返回固定回复
    calls = []

    def fake_run(cmd, timeout=30):
        calls.append(('run', cmd))
        if 'openssl' in cmd:
            import subprocess as _sp
            p = _sp.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout
        if 'journalctl' in cmd:
            return 0, '2026-10-06T10:00:00 INFO test: fake log line\n2026-10-06T10:00:01 ERROR test: boom'
        if 'hostname -I' in cmd:
            return 0, '127.0.0.1\n'
        return 0, 'fake-ok'

    monkeypatch.setattr(A, 'run', fake_run)
    monkeypatch.setattr(A, '_hermes_ask',
                        lambda prompt, session_id=None, timeout=600: ('测试回复', '20260101_090000_aaa111'))
    monkeypatch.setattr(A, '_list_session_ids', lambda limit=8: [])

    c = A.app.test_client()
    with c.session_transaction() as s:
        s['user'] = 'admin'
        s['must_change'] = False
    c.calls = calls
    c.app_module = A
    yield c


@pytest.fixture()
def app_module(client):
    return client.app_module
