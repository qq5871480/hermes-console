"""技能与备份：上传防穿越、下载白名单、还原确认词。"""
import io
import zipfile


def _mkzip(struct):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, content in struct.items():
            z.writestr(name, content)
    buf.seek(0)
    return buf


def test_skill_upload_ok(client, tmp_path):
    import app as A
    z = _mkzip({'my-skill/SKILL.md': '---\nname: my-skill\ndescription: t\n---\nbody'})
    r = client.post('/skills/upload', data={'name': 'my-skill', 'zip': (z, 's.zip')},
                    content_type='multipart/form-data', follow_redirects=True)
    assert r.status_code == 200
    assert (A.HERMES_HOME / 'skills' / 'my-skill' / 'SKILL.md').exists()


def test_skill_upload_bad_name_rejected(client):
    z = _mkzip({'x/SKILL.md': '---\nname: x\ndescription: t\n---\nbody'})
    r = client.post('/skills/upload', data={'name': 'Bad Name!', 'zip': (z, 's.zip')},
                    content_type='multipart/form-data', follow_redirects=True)
    assert r.status_code == 200
    import app as A
    assert not (A.HERMES_HOME / 'skills' / 'Bad Name!').exists()


def test_skill_upload_no_skillmd_rollback(client):
    z = _mkzip({'onlyfile.txt': 'no skill here'})
    client.post('/skills/upload', data={'name': 'noskill', 'zip': (z, 's.zip')},
                content_type='multipart/form-data', follow_redirects=True)
    import app as A
    assert not (A.HERMES_HOME / 'skills' / 'noskill').exists()


def test_backup_create_download_delete(client):
    r = client.post('/backups/create', data={'kind': 'normal'}, follow_redirects=True)
    assert r.status_code == 200
    h = client.get('/backups').get_data(as_text=True)
    assert '.tar.gz' in h
    # 拿到第一个包名下载
    import re
    m = re.search(r'href="/backups/download/([^"]+\.tar\.gz)"', h)
    if m:
        name = m.group(1)
        d = client.get(f'/backups/download/{name}')
        assert d.status_code == 200
        # 路径穿越拒绝
        assert client.get('/backups/download/..%2F..%2Fetc%2Fpasswd').status_code == 404
        # 删除
        client.post(f'/backups/delete/{name}', follow_redirects=True)


def test_backup_restore_needs_restore_word(client):
    h = client.get('/backups').get_data(as_text=True)
    import re
    m = re.search(r'action="/backups/restore/([^"]+\.tar\.gz)"', h)
    if m:
        name = m.group(1)
        import json as jj
        r = client.post(f'/backups/restore/{name}', data={'confirm': 'WRONG'})
        assert jj.loads(r.get_data(as_text=True))['ok'] is False if r.content_type == 'application/json' else True
