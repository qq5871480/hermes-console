"""仪表盘/日志/指标：核心读路径。"""


def test_dashboard_content(client):
    h = client.get('/').get_data(as_text=True)
    assert '运维状态' in h
    assert 'Gateway' in h
    assert 'mchart' in h  # 资源曲线容器


def test_metrics_api_shape(client):
    import json as jj
    r = jj.loads(client.get('/metrics/api?hours=6').get_data(as_text=True))
    assert r['ok'] is True
    assert isinstance(r['rows'], list)
    assert r['rows'], '至少应有惰性采样产生的1个点'
    row = r['rows'][-1]
    assert all(k in row for k in ('ts', 'cpu', 'mem', 'load1'))


def test_logs_page_and_filter(client):
    h = client.get('/logs?n=50').get_data(as_text=True)
    assert 'logbox' in h
    assert 'logq' in h
    # 关键词过滤
    import json as jj
    r = jj.loads(client.get('/logs/api?n=50&q=boom').get_data(as_text=True))
    assert r['ok'] is True
    assert len(r['lines']) == 1
    assert 'boom' in r['lines'][0]
    # 级别过滤
    r2 = jj.loads(client.get('/logs/api?n=50&level=ERROR').get_data(as_text=True))
    assert len(r2['lines']) == 1


def test_system_reboot_requires_confirm(app_module, client):
    import json as jj
    r = client.post('/system/reboot', json={'confirm': 'WRONG'})
    assert jj.loads(r.get_data(as_text=True))['ok'] is False
