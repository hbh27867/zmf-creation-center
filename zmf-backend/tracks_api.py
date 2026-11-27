# -*- coding: utf-8 -*-
"""ZMF 五轨读取接口模块（给 server.py 接线用）。

职责：解析 `Documents/UP主风格研究/data.js`（334 段，const DATA=[...]），
把五轨内容以 JSON 吐给创作中心网页。纯标准库、只读、不写任何用户文件。

对外函数：
  load_tracks()            -> 全量精简索引（默认，334 条）
  get_track(bvid, scene, tracks) -> 指定段的五轨全文
  handle(path, query)      -> 返回 (status, dict)，供 server.py 直接转发
"""
import json
import os
import re
import time

DATA_JS = r'C:/Users/hello Teng/Documents/UP主风格研究/data.js'
TRACKS = ('prompt_visual', 'prompt_full', 'prompt_moss', 'prompt_en', 'prompt_static',
          'prompt_h3')
SHORT_TRACKS = ('prompt_moss', 'prompt_en', 'prompt_static')

_cache = {'mtime': 0, 'size': 0, 'data': None}


def _load_data():
    """括号配平解析 data.js 的第一个数组字面量（字符串/转义感知）。"""
    st = os.stat(DATA_JS)
    if _cache['data'] is not None and _cache['mtime'] == st.st_mtime and _cache['size'] == st.st_size:
        return _cache['data']
    s = open(DATA_JS, encoding='utf-8').read()
    i = s.index('[')
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(s)):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                data = json.loads(s[i:j + 1])
                _cache.update(mtime=st.st_mtime, size=st.st_size, data=data)
                return data
    raise ValueError('data.js 数组括号不配平')


def load_tracks():
    data = _load_data()
    items = []
    for d in data:
        ann = d.get('ann', {}) or {}
        items.append({
            'bvid': d.get('bvid'),
            'scene': d.get('scene'),
            'series': d.get('series', ''),
            'title': ann.get('title', ''),
            'ref': d.get('ref', '') or '',
            'has': {t: bool(ann.get(t)) for t in TRACKS},
        })
    return items


def get_track(bvid, scene, data=None):
    data = data if data is not None else _load_data()
    for d in data:
        if d.get('bvid') == bvid and d.get('scene') == scene:
            ann = d.get('ann', {}) or {}
            out = {'bvid': bvid, 'scene': scene, 'series': d.get('series', ''),
                   'title': ann.get('title', ''), 'ref': d.get('ref', '') or '',
                   'tracks': {t: ann.get(t, '') for t in TRACKS}}
            return out
    return None


def handle(path, query):
    """返回 (status, dict)。path 形如 /api/tracks 或 /api/track。"""
    try:
        if path == '/api/tracks':
            items = load_tracks()
            return 200, {'ok': True, 'total': len(items), 'items': items,
                         'tracks': list(TRACKS), 'source': DATA_JS,
                         'generated': time.strftime('%H:%M:%S')}
        if path == '/api/track':
            bvid = query.get('bvid', [''])[0]
            scene_raw = query.get('scene', [''])[0]
            if not bvid or scene_raw == '':
                return 400, {'ok': False, 'msg': '需要 bvid 与 scene 参数'}
            try:
                scene = int(scene_raw)
            except ValueError:
                return 400, {'ok': False, 'msg': 'scene 必须是整数'}
            found = get_track(bvid, scene)
            if not found:
                return 404, {'ok': False, 'msg': '未找到该段', 'bvid': bvid, 'scene': scene}
            return 200, {'ok': True, **found}
    except Exception as e:
        return 500, {'ok': False, 'msg': str(e)}
    return 404, {'ok': False, 'msg': 'not found'}
