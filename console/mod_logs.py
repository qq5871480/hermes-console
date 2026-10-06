"""Gateway 日志模块（从app.py重构迁出，逻辑未改）。

动态引用约定：HERMES_HOME/HERMES_BIN等被多实例切换重绑，
一律 core.XXX 属性访问（延迟绑定），禁止 from-import 快照。
"""
from flask import request, jsonify, render_template
import re
import shlex
import subprocess
import threading
import time
import app as core


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




def logs():
    _, unit = core.run("systemctl --user list-units --type=service --no-legend 2>/dev/null | grep -E 'hermes-gateway|gateway' | awk '{print $1}' | head -1")
    unit = unit.strip() or 'hermes-gateway'
    try:
        n = int(request.args.get('n', 200))
    except ValueError:
        n = 200
    n = max(50, min(n, 2000))
    q = request.args.get('q', '')
    level = request.args.get('level', '')
    _, out = core.run(f"journalctl --user -u {shlex.quote(unit)} -n {n} --no-pager -o short-iso 2>&1 | tail -n {n}")
    if 'No journal files' in out or not out.strip() or 'No entries' in out:
        _, out2 = core.run(f"journalctl -u {shlex.quote(unit)} -n {n} --no-pager -o short-iso 2>&1 | tail -n {n}")
        out = out2 if out2.strip() and 'No entries' not in out2 and 'No journal' not in out2 else out
    if 'No journal files' in out or not out.strip() or 'No entries' in out:
        # 最终兜底：~/.hermes/logs/gateway.log
        _, out2 = core.run(f"tail -n {n} {shlex.quote(str(core.HERMES_HOME))}/logs/gateway.log 2>/dev/null")
        out = out2 if out2.strip() else out
    lines = _log_filter_lines(out.splitlines(), q, level)
    return render_template('logs.html', logs=lines, unit=unit, n=n, q=q, level=level)


_RATE_LIMIT_RE = re.compile(r'rate-?limited|429|Too Many Requests', re.I)


def _hermes_explain(prompt, timeout=170):
    """一次性调用 hermes CLI 做日志解读。限流时等25秒自动重试一次，再失败给人话提示。"""
    last_err = ''
    for attempt in (1, 2):
        try:
            out, _sid = core._hermes_ask(prompt, timeout=timeout)
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


_tidy_explain_lock = core.threading.Lock()


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


def logs_api():
    try:
        n = int(request.args.get('n', 100))
    except ValueError:
        n = 100
    n = max(20, min(n, 500))
    _, unit = core.run("systemctl --user list-units --type=service --no-legend 2>/dev/null | grep -E 'hermes-gateway|gateway' | awk '{print $1}' | head -1")
    unit = unit.strip() or 'hermes-gateway'
    _, out = core.run(f"journalctl --user -u {shlex.quote(unit)} -n {n} --no-pager -o short-iso 2>&1 | tail -n {n}")
    if 'No journal files' in out or not out.strip() or 'No entries' in out:
        _, out2 = core.run(f"tail -n {n} {shlex.quote(str(core.HERMES_HOME))}/logs/gateway.log 2>/dev/null")
        out = out2 if out2.strip() else out
    q = request.args.get('q', '')
    level = request.args.get('level', '')
    lines = [ln for _lv, ln in _log_filter_lines(out.splitlines(), q, level)]
    return jsonify({'ok': True, 'lines': lines})