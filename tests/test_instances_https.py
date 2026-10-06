"""多实例与HTTPS：登记/切换/删除/证书。"""


def test_instances_seeded_and_page(client):
    client.get('/instances')  # 触发种子
    h = client.get('/instances').get_data(as_text=True)
    assert '默认实例' in h
    assert 'instances/add' in h


def test_instances_add_and_switch(client, tmp_path):
    # 造第二个实例
    home2 = tmp_path / 'bot2' / '.hermes'
    home2.mkdir(parents=True)
    (home2 / 'config.yaml').write_text('model:\n  default: other\n')
    r = client.post('/instances/add', data={'name': '二号', 'home': str(home2)}, follow_redirects=True)
    assert r.status_code == 200
    # 找到它的id并切换
    import json as jj
    # 切换后session里是home2
    with client.session_transaction() as s:
        pass
    # 用页面解析id较繁琐：直接遍历1..5试切换
    switched = False
    for iid in range(1, 6):
        rc = client.post(f'/instances/switch/{iid}', follow_redirects=True)
        if rc.status_code == 200:
            with client.session_transaction() as s:
                if s.get('inst_home') == str(home2):
                    switched = True
                    # 切回默认
                    client.post('/instances/switch/1', follow_redirects=True)
                    break
    assert switched


def test_instances_bad_path_rejected(client):
    client.post('/instances/add', data={'name': 'bad', 'home': '/no/such/dir'}, follow_redirects=True)
    import app as A
    with A.db() as conn:
        names = [x['name'] for x in conn.execute('SELECT name FROM instances')]
    assert 'bad' not in names


def test_https_page_and_cert(client, tmp_path, monkeypatch):
    import app as A
    A.CERT_DIR = tmp_path / 'certs'
    import json as jj
    r = jj.loads(client.post('/https/generate').get_data(as_text=True))
    assert r['ok'] is True
    assert (A.CERT_DIR / 'console.crt').exists()
    assert (A.CERT_DIR / 'console.key').exists()
    # key权限600
    key_mode = (A.CERT_DIR / 'console.key').stat().st_mode & 0o777
    assert key_mode == 0o600
    # 证书可解析
    import subprocess
    p = subprocess.run(f'openssl x509 -in {A.CERT_DIR}/console.crt -noout -subject',
                       shell=True, capture_output=True, text=True)
    assert 'CN' in p.stdout


def test_https_status_shape(client):
    import json as jj
    s = jj.loads(client.get('/https/status').get_data(as_text=True))
    assert 'managed' in s
