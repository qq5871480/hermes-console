"""cron：列表、历史采集、id注入拦截。"""
import json
import time


def test_cron_page_lists_jobs(client):
    h = client.get('/cron').get_data(as_text=True)
    assert '测试任务' in h


def test_cron_action_rejects_bad_id(client):
    r = client.post('/cron/action/BAD!!ID/run')
    assert r.status_code == 400


def test_cron_run_records_history(client):
    # 桩run：cron run命令返回成功
    import app as A
    orig = A.run
    A.run = lambda cmd, timeout=30: (0, 'scheduled ok') if ' cron run ' in cmd else orig(cmd, timeout)
    try:
        client.post('/cron/action/abc123def456/run', follow_redirects=True)
    finally:
        A.run = orig
    import json as jj
    r = jj.loads(client.get('/cron/history?job_id=abc123def456').get_data(as_text=True))
    assert r['ok'] and len(r['rows']) >= 1
    row = r['rows'][0]
    assert row['ok'] == 1
    assert 'scheduled ok' in row['output']


def test_cron_history_clear(client):
    client.post('/cron/history/clear', follow_redirects=True)
    import json as jj
    r = jj.loads(client.get('/cron/history').get_data(as_text=True))
    assert r['rows'] == []
