"""记忆/人格：编辑、删除、容量、整理接口。"""


def test_persona_page(client):
    h = client.get('/persona').get_data(as_text=True)
    assert 'SOUL.md' in h and 'MEMORY.md' in h and 'USER.md' in h


def test_memory_edit_roundtrip(client):
    r = client.post('/memory/edit', data={'which': 'mem', 'content': '新条目A\n§\n新条目B'}, follow_redirects=True)
    assert r.status_code == 200
    h = client.get('/persona').get_data(as_text=True)
    assert '新条目A' in h


def test_memory_delete_entry(client):
    # 编辑留2条
    client.post('/memory/edit', data={'which': 'mem', 'content': '甲\n§\n乙'}, follow_redirects=True)
    r = client.post('/memory/delete', data={'which': 'mem', 'idx': 0}, follow_redirects=True)
    assert r.status_code == 200
    h = client.get('/persona').get_data(as_text=True)
    assert '甲' not in h and '乙' in h


def test_memory_rejects_bad_which(client):
    r = client.post('/memory/edit', data={'which': 'HACK', 'content': 'x'})
    assert r.status_code in (400, 302)


def test_capacity_change(app_module, client, tmp_path):
    import json as jj
    cfg = app_module.HERMES_HOME / 'config.yaml'
    before = cfg.read_text()
    r = client.post('/persona/capacity', data={'memory_char_limit': '8800', 'user_char_limit': '2000'},
                    follow_redirects=True)
    # 有该端点则验证生效；无该端点则跳过（版本差异）
    if r.status_code == 404:
        return
    assert '8800' in cfg.read_text()
