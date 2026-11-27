# -*- coding: utf-8 -*-
"""ZMF 图库输出管理服务：浏览 + 按提示词分组 + 删除（移入 _trash 可恢复）。
端口 8191。GET / 看图库；POST /api/refresh 重建清单；POST /api/delete 删除单张。
"""
import copy
import hashlib
import json
import os
import re
import time
import glob
import shutil
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

TMP = 'C:/tmp_zmf'
OUT = r'C:/Users/hello Teng/3D Objects/ComfyUI-aki-v3-IntelArc_20261031/ComfyUI-aki-v3-IntelArc/ComfyUI/output'
TRASH = os.path.join(OUT, '_trash')
REF_ROOT = r'C:/Users/hello Teng/Documents/UP主风格研究'
HANDRAW = r'C:/Users/hello Teng/.zcode/skills/handraw-skill'
COMFY = 'http://127.0.0.1:8188'
MANIFEST = os.path.join(TMP, 'gallery', 'manifest.json')  # 独立清单，避免与 8191 旧实例互踩
HERE = os.path.dirname(os.path.abspath(__file__))

SAFE_IMG = re.compile(r'^(?:MODE0[12]|ZMF_[A-Za-z0-9_\-]+)_[A-Za-z0-9_\-]+\.png$')
SAFE_IMG_CHARS = re.compile(r'^[A-Za-z0-9_\-]+\.(png|jpg|jpeg|webp)$')

# 散图收录：用户自己跑的图登记后允许 /img 访问与删除
ADOPTED_FILE = os.path.join(TMP, 'gallery', 'adopted_files.json')
ADOPTED = set()

def load_adopted():
    global ADOPTED
    try:
        ADOPTED = set(json.load(open(ADOPTED_FILE, encoding='utf-8')))
    except Exception:
        ADOPTED = set()

def save_adopted():
    json.dump(sorted(ADOPTED), open(ADOPTED_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

def img_allowed(fn):
    """字符校验（防路径穿越）+ 前缀白名单或散图收录名单。"""
    return bool(SAFE_IMG_CHARS.match(fn)) and (SAFE_IMG.match(fn) or fn in ADOPTED)

LLM_CONFIG = os.path.join(TMP, 'gallery', 'llm_config.json')

# ---------- AI 创作接口数据层（zmf-create 技能消费） ----------
HUB_DATA = os.path.join(HANDRAW, 'gallery', 'zmf-data.js')
CREATE_LOG = os.path.join(TMP, 'gallery', 'create_log.json')

import importlib.util
_spec = importlib.util.spec_from_file_location('rating_feedback', os.path.join(HERE,'rating_feedback.py'))
feedback = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(feedback)

def feedback_view(hub, body=None):
    return feedback.view(HERE, MANIFEST, OUT, hub, img_allowed, body)

def load_hub(raw=False):
    s = open(HUB_DATA, encoding='utf-8').read()
    i = s.index('{')
    depth = 0; in_str = False; esc = False
    for j in range(i, len(s)):
        c = s[j]
        if in_str:
            if esc: esc = False
            elif c == '\\': esc = True
            elif c == '"': in_str = False
            continue
        if c == '"': in_str = True
        elif c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                hub = json.loads(s[i:j + 1])
                if not raw:
                    stats = feedback_view(hub)['recipes']
                    for q in hub['RECIPES']:
                        if q['id'] in stats:
                            q['feedback_weight'] = stats[q['id']]['weight']
                            if not q['feedback_weight']: q['auto'] = False
                return hub
    raise ValueError('zmf-data.js 括号不配平')

def exclusion_items(items):
    if not isinstance(items, list) or len(items) > 10000:
        raise ValueError('不借鉴清单必须是数组，最多 10000 条')
    keys, clean = set(), []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('不借鉴清单条目格式无效')
        bvid, scene = item.get('bvid'), item.get('scene')
        if not isinstance(bvid, str) or not re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid) or type(scene) is not int or scene < 0:
            raise ValueError('不借鉴清单的段号无效')
        key = f'{bvid}/s{scene:02d}'
        if key in keys:
            raise ValueError('不借鉴清单存在重复段号')
        keys.add(key)
        clean.append({'bvid': bvid, 'scene': scene, 'key': f'{bvid}|{scene}',
                      'series': str(item.get('series', ''))[:100], 'title': str(item.get('title', ''))[:200]})
    return keys, clean


def recipe_exclusions(hub):
    """重建基线持续有效，再叠加最新一次已导出的排除清单。"""
    denied = set(hub.get('EXCLUDED_SOURCES', []))
    matches = sorted(glob.glob(os.path.join(TMP, '不借鉴清单_*.json')))
    if matches:
        with open(matches[-1], encoding='utf-8-sig') as file:
            data = json.load(file)
        keys, clean = exclusion_items(data['items'])
        if len(clean) != data['count']:
            raise ValueError('排除清单计数不一致')
        denied.update(keys)
    if os.path.exists(CASCADE_STATE):
        with open(CASCADE_STATE, encoding='utf-8-sig') as file:
            state = json.load(file)
        denied.update(f"{row['bvid']}/s{row['scene']:02d}" for row in state.get('items', []))
    return denied


def recipe_pool(hub, denied):
    return [q for q in hub.get('RECIPES', []) if not denied.intersection(q['sources'])]


def active_catalog(hub, denied):
    """界面和自动路由使用同一来源过滤；共享风格的代表图从剩余配方重新选。"""
    pool = recipe_pool(hub, denied)
    kinds = {'COLORS': ('id', 'color'), 'LAYOUTS': ('id', 'layout'), 'SCENES': ('id', 'id')}
    result = {**hub, 'RECIPES': pool, 'POOL_REVISION': recipe_revision(hub, denied),
              'EXCLUDED_COUNT': len(denied)}
    for key, (ident, column) in kinds.items():
        used = {q[column] for q in pool}
        result[key] = [row for row in hub[key] if row[ident] in used]
    result['STYLES'] = []
    for st in hub['STYLES']:
        support = [q for q in pool if q['style'] == st['number']]
        if support:
            result['STYLES'].append({**st, 'img': support[0]['img'], 'reference': support[0]['source'],
                                     'sources': sorted({key for q in support for key in q['sources']})})
    result['LAMP'] = {c['id']: hub['LAMP'][c['id']] for c in result['COLORS']}
    result['COMPGUIDE'] = {l['id']: hub['COMPGUIDE'][l['id']] for l in result['LAYOUTS']}
    return result


def current_catalog():
    hub = load_hub()
    return active_catalog(hub, recipe_exclusions(hub))


def recipe_revision(hub, denied):
    import hashlib
    data = json.dumps([hub, sorted(denied)], sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(data.encode('ascii')).hexdigest()[:16]


CASCADE_STATE = os.path.join(TMP, 'gallery', 'gallery_cascade_state.json')
CASCADE_LOCK = threading.Lock()


def _json_atomic(path, data):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=os.path.dirname(path),
                                         suffix='.tmp', delete=False) as file:
            tmp = file.name
            json.dump(data, file, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def _hub_bytes(hub):
    return ('const ZMF_DATA = ' + json.dumps(hub, ensure_ascii=False, separators=(',', ':')) + ';\n').encode('utf-8')


def _source_identity(value):
    match = re.fullmatch(r'(BV[0-9A-Za-z]{10})/s(\d+)', str(value or ''))
    return (match.group(1), int(match.group(2))) if match else None


def _cascade_hub(baseline, items):
    hub = copy.deepcopy(baseline)
    deleted = {(row['bvid'], row['scene']) for row in items}
    removed_recipes = []
    kept_recipes = []
    for recipe in hub.get('RECIPES', []):
        sources = set(recipe.get('sources') or [])
        if recipe.get('source'):
            sources.add(recipe['source'])
        identities = {_source_identity(source) for source in sources}
        identities.discard(None)
        if identities & deleted:
            removed_recipes.append(recipe)
        else:
            kept_recipes.append(recipe)
    hub['RECIPES'] = kept_recipes

    recipe_ids = {recipe['id'] for recipe in kept_recipes}
    scene_ids = {row['id'] for row in hub.get('SCENES', [])} & recipe_ids
    hub['RECIPES'] = [recipe for recipe in kept_recipes
                      if (recipe.get('scene_id') or recipe['id']) in scene_ids]
    used_scenes = {recipe.get('scene_id') or recipe['id'] for recipe in hub['RECIPES']}
    used_colors = {recipe['color'] for recipe in hub['RECIPES'] if recipe.get('color')}
    used_layouts = {recipe['layout'] for recipe in hub['RECIPES'] if recipe.get('layout')}
    used_styles = {recipe['style'] for recipe in hub['RECIPES'] if recipe.get('style')}
    hub['SCENES'] = [row for row in hub.get('SCENES', []) if row['id'] in used_scenes]
    hub['COLORS'] = [row for row in hub.get('COLORS', []) if row['id'] in used_colors]
    hub['LAYOUTS'] = [row for row in hub.get('LAYOUTS', []) if row['id'] in used_layouts]
    hub['STYLES'] = [row for row in hub.get('STYLES', []) if row['number'] in used_styles]
    hub['LAMP'] = {key: value for key, value in hub.get('LAMP', {}).items() if key in used_colors}
    hub['COMPGUIDE'] = {key: value for key, value in hub.get('COMPGUIDE', {}).items() if key in used_layouts}

    scene_ids = {row['id'] for row in hub['SCENES']}
    color_ids = {row['id'] for row in hub['COLORS']}
    layout_ids = {row['id'] for row in hub['LAYOUTS']}
    style_ids = {row['number'] for row in hub['STYLES']}
    recipe_ids = {row['id'] for row in hub['RECIPES']}
    for recipe in hub['RECIPES']:
        if not recipe.get('binding_complete', True):
            continue
        if recipe['style'] not in style_ids or recipe['color'] not in color_ids or recipe['layout'] not in layout_ids:
            raise ValueError('级联删除后配方引用损坏')
        if (recipe.get('scene_id') or recipe['id']) not in scene_ids:
            raise ValueError('级联删除后场景引用损坏')
    if not scene_ids <= recipe_ids:
        raise ValueError('级联删除后场景缺少同ID主配方')
    return hub, removed_recipes


def apply_gallery_cascade(items):
    with CASCADE_LOCK:
        _, clean = exclusion_items(items)
        current_bytes = open(HUB_DATA, 'rb').read()
        current_hash = hashlib.sha256(current_bytes).hexdigest()
        state = None
        if os.path.exists(CASCADE_STATE):
            with open(CASCADE_STATE, encoding='utf-8-sig') as file:
                state = json.load(file)
        if state and current_hash != state.get('current_sha256'):
            raise ValueError('正式四库已被其他操作更新，请先重新导入或恢复备份')
        if not clean and not state:
            return {'ok': True, 'count': 0, 'removed': {}, 'counts': {}, 'restored': False}

        from datetime import datetime
        stamp = datetime.now()
        if state is None:
            backup = os.path.join(os.path.dirname(HUB_DATA), '_备份_图鉴级联删除前_' + stamp.strftime('%Y%m%d_%H%M%S_%f') + '.js')
            with open(backup, 'wb') as file:
                file.write(current_bytes)
            baseline_bytes = current_bytes
            state = {'baseline': backup, 'baseline_sha256': current_hash,
                     'current_sha256': current_hash, 'items': []}
        baseline_bytes = open(state['baseline'], 'rb').read()
        if hashlib.sha256(baseline_bytes).hexdigest() != state['baseline_sha256']:
            raise ValueError('级联删除基线备份校验失败')
        text = baseline_bytes.decode('utf-8-sig')
        baseline = json.loads(re.fullmatch(r'\s*const\s+ZMF_DATA\s*=\s*(\{.*\})\s*;?\s*', text, re.S).group(1))
        before = load_hub(raw=True)
        after, removed_recipes = _cascade_hub(baseline, clean)
        target_bytes = baseline_bytes if not clean else _hub_bytes(after)
        target_hash = hashlib.sha256(target_bytes).hexdigest()
        if target_hash != current_hash:
            backup = os.path.join(os.path.dirname(HUB_DATA), '_备份_图鉴级联删除_' + stamp.strftime('%Y%m%d_%H%M%S_%f') + '.js')
            with open(backup, 'wb') as file:
                file.write(current_bytes)
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(mode='wb', dir=os.path.dirname(HUB_DATA), suffix='.tmp', delete=False) as file:
                    tmp = file.name
                    file.write(target_bytes)
                os.replace(tmp, HUB_DATA)
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
        state.update({'current_sha256': target_hash, 'items': clean,
                      'updated_at': stamp.isoformat(timespec='seconds')})
        _json_atomic(CASCADE_STATE, state)
        kinds = ('STYLES', 'COLORS', 'LAYOUTS', 'SCENES', 'RECIPES')
        return {'ok': True, 'count': len(clean), 'restored': not clean,
                'removed': {kind: len(before.get(kind, [])) - len(after.get(kind, [])) for kind in kinds},
                'removed_recipe_ids': [row['id'] for row in removed_recipes],
                'counts': {kind: len(after.get(kind, [])) for kind in kinds},
                'backup': state['baseline']}


def assemble_recipe(hub, recipe, theme):
    color = next(c for c in hub['COLORS'] if c['id'] == recipe['color'])
    layout = next(l for l in hub['LAYOUTS'] if l['id'] == recipe['layout'])
    style = next(s for s in hub['STYLES'] if s['number'] == recipe['style'])
    lid, cid = layout['id'], color['id']
    lamp = hub['LAMP'][cid]
    sk = (f'横幅东方幻想壁纸。画面主体：{theme.rstrip("。！；，.")}。\n\n'
          f'{layout["mechanism"]}。{hub["COMPGUIDE"][lid]}。\n\n'
          f'{style["traits"]}\n\n{color["prompt_zh"]}避开{lamp[4]}。'
          '画面没有题字、水印、装饰边框或界面叠层。')
    return {'skeleton': sk, 'color': cid, 'comp': lid, 'style': style['number'], 'lamp': lamp,
            'series_style': style['generation_name'], 'theme': theme, 'env': recipe['series'],
            'recipe': recipe['id'], 'source': recipe['source'], 'version': hub.get('VERSION', '')}


def recipe_deck(recipes, pick_seed):
    """按系列交错，每套先发一个题材，再进入第二轮，不重复抽同题。"""
    import random
    rng = random.Random(pick_seed)
    themes = {}
    for row in recipes:
        themes[row['id']] = list(row['themes'])
        rng.shuffle(themes[row['id']])
    previous = None
    deck = []
    for pass_index in range(max((len(v) for v in themes.values()), default=0)):
        lanes = {}
        for row in recipes:
            options = themes[row['id']]
            if pass_index < len(options):
                lanes.setdefault(row['series'], []).append((row, options[pass_index]))
        order = list(lanes)
        rng.shuffle(order)
        for lane in lanes.values():
            rng.shuffle(lane)
        while order:
            if len(order) > 1 and order[0] == previous:
                order.append(order.pop(0))
            current = order.pop(0)
            deck.append(lanes[current].pop())
            previous = current
            if lanes[current]:
                order.append(current)
    # 按相对权重决定每个题材是否进入本轮，不复制题材。
    ceiling = max((row.get('feedback_weight',1) for row in recipes), default=1)
    if ceiling and any(row.get('feedback_weight',1)!=ceiling for row in recipes):
        deck = [(row,t) for row,t in deck if rng.random()*ceiling < row.get('feedback_weight',1)]
    return deck


def recipe_response(hub, denied, path, body):
    if not isinstance(body, dict):
        raise ValueError('需要 JSON 对象')
    pool = recipe_pool(hub, denied)
    revision = recipe_revision(hub, denied)
    if body.get('pool_revision') is not None and body['pool_revision'] != revision:
        return 409, {'ok': False, 'msg': '配置或不借鉴清单已更新，请重新选参', 'pool_revision': revision}
    if path == '/api/assemble':
        qid = body.get('recipe')
        automatic = body.get('automatic', False)
        theme = body.get('theme', '')
        supplied = {k: body[k] for k in ('color', 'comp', 'style') if k in body}
        if ((qid is not None and not isinstance(qid, str)) or type(automatic) is not bool
                or not isinstance(theme, str) or len(theme) > 600
                or any(not isinstance(v, str) for v in supplied.values())):
            raise ValueError('配方、主题或编号格式无效；主题最多 600 字符')
        if qid:
            q = next((x for x in pool if x['id'] == qid), None)
        else:
            q = next((x for x in pool if (x['color'], x['layout'], x['style']) ==
                      (supplied.get('color'), supplied.get('comp'), supplied.get('style'))), None)
        field_map = {'color': 'color', 'comp': 'layout', 'style': 'style'}
        if q is None or any(q[field_map[k]] != v for k, v in supplied.items()):
            raise ValueError('该搭配没有配方或已被排除，请重新选择场景卡')
        if automatic and not q['auto']:
            raise ValueError('此配方只供人工借构图，不能进自动创作')
        return 200, {'ok': True, 'pool_revision': revision, **assemble_recipe(hub, q, theme.strip() or q['themes'][0])}

    import secrets
    n = body.get('n', 1)
    seed = body.get('pick_seed')
    cursor = body.get('cursor', 0)
    series = body.get('series', '')
    if (type(n) is not int or not 1 <= n <= 8 or type(cursor) is not int or cursor < 0
            or (seed is not None and (type(seed) is not int or not 0 <= seed < 2**53))
            or not isinstance(series, str)):
        raise ValueError('n 为 1–8，cursor 为非负整数，pick_seed 必须是有效的非负整数')
    auto = [q for q in pool if q['auto']]
    if series and not any(q['series'] == series for q in auto):
        raise ValueError('该系列没有可用自动配方')
    if series:
        auto = [q for q in auto if q['series'] == series]
    seed = secrets.randbits(53) if seed is None else seed
    deck = recipe_deck(auto, seed)
    picks = [assemble_recipe(hub, q, theme) for q, theme in deck[cursor:cursor+n]]
    return 200, {'ok': bool(picks), 'seeds': picks, 'pick_seed': seed, 'pool_revision': revision,
                 'cursor': cursor, 'next_cursor': cursor+len(picks), 'total': len(deck),
                 'msg': '' if picks else '这轮的可用题材已用完，或当前没有自动配方'}


def queue_busy():
    q = {}
    try:
        with urllib.request.urlopen(COMFY + '/queue', timeout=5) as r:
            q = json.loads(r.read())
    except Exception:
        return {'reachable': False, 'running': 0, 'pending': 0}
    return {'reachable': True, 'running': len(q.get('queue_running', [])), 'pending': len(q.get('queue_pending', []))}

EXPAND_LEVELS = {
    1: '保真补充：只补已有物件的形态、材质和交接，不增加主体、道具、光源。',
    2: '可控增强：可细化已有材质、笔触、虚实和光影过渡，但不得新增主体、道具、光源或改变主色。',
}
EXPAND_LOCKS = ('将骨架写成中文壁纸提示词，只返回正文。保留主体数、占位、尺度、取景、引导线、'
                '遮挡、光色落点和暗部。骨架明确有人物时保留其数量与位置；只有骨架明确无人时才不添加人物。'
                '不加雾、金尘、水印、文字或边框。')


class ExpandError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def expansion_text(value, name, limit, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ExpandError(400, f'{name} 无效（最多 {limit} 字）')
    return value.strip()


def expansion_model(system, user, temperature, tokens, response_format=None):
    from urllib.parse import urlsplit
    from urllib.error import HTTPError
    try:
        with open(LLM_CONFIG, encoding='utf-8') as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError):
        raise ExpandError(500, '扩写配置无法读取') from None
    if not isinstance(cfg, dict):
        raise ExpandError(500, '扩写配置格式错误')
    key = expansion_text(cfg.get('key', ''), 'API key（请在 AI 扩写设置配置）', 2048)
    model = expansion_text(cfg.get('model', ''), '模型', 200)
    endpoint = expansion_text(cfg.get('endpoint', ''), '接口地址', 2048).rstrip('/')
    try:
        u = urlsplit(endpoint)
        valid = (u.scheme == 'https' or (u.scheme == 'http' and u.hostname in ('127.0.0.1', 'localhost', '::1'))) and u.hostname and not (u.username or u.password or u.query or u.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ExpandError(400, '接口地址须为 HTTPS（或本机 HTTP），不能包含凭据、查询或片段')
    if not endpoint.endswith('/chat/completions'):
        endpoint += '/chat/completions'
    payload = {'model': model, 'messages': [{'role': 'system', 'content': system},
               {'role': 'user', 'content': user}], 'temperature': temperature,
               'max_tokens': tokens, 'stream': False}
    if response_format is not None:
        payload['response_format'] = response_format
    if model.casefold().startswith('glm-'):
        payload['thinking'] = {'type': 'disabled'}
    # 不跟随重定向，避免把 Authorization 发送到另一个站点。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode('utf-8'),
              headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=180) as resp:
            blob = resp.read(1_048_577)
        if len(blob) > 1_048_576:
            raise ExpandError(502, '模型响应过大')
        result = json.loads(blob)
        choice = result['choices'][0]
        text = choice['message'].get('content')
        if choice.get('finish_reason') not in (None, 'stop') or not isinstance(text, str) or not text.strip():
            raise ExpandError(502, '模型未返回完整正文；请检查思考模式或 token 上限')
        usage = result.get('usage')
        return text.strip(), usage.get('total_tokens') if isinstance(usage, dict) else None
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise ExpandError(502, f'上游模型接口错误 HTTP {status}；请核对模型、权限或额度') from None
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ExpandError(502, '无法读取上游模型响应，请检查接口地址或网络') from None


def expand_response(path, body):
    if not isinstance(body, dict):
        raise ExpandError(400, '需要 JSON 对象')
    if path == '/api/expand':
        level = body.get('level', 1)
        if type(level) is not int or level not in EXPAND_LEVELS:
            raise ExpandError(400, 'level 只接受 1 或 2；请用创意分支接口探索 3 档')
        skeleton = expansion_text(body.get('skeleton', body.get('prompt', '')), '骨架', 12000)
        theme = expansion_text(body.get('theme', ''), '题材', 600, False)
        text, usage = expansion_model(EXPAND_LOCKS + EXPAND_LEVELS[level],
                    json.dumps({'theme': theme, 'skeleton': skeleton}, ensure_ascii=False),
                    0.3 if level == 1 else 0.55, 1600)
        return {'ok': True, 'level': level, 'expanded': text, 'usage': usage}
    import unicodedata
    rid = expansion_text(body.get('recipe'), '母配方', 80)
    rev = expansion_text(body.get('pool_revision'), '配置版本', 80)
    n = body.get('n', 3)
    if type(n) is not int or not 1 <= n <= 3:
        raise ExpandError(400, 'n 只接受 1–3 的整数')
    def catalog():
        try:
            return current_catalog()
        except (OSError, ValueError, TypeError, KeyError):
            raise ExpandError(500, '无法读取配方或不借鉴清单') from None
    h = catalog()
    if h['POOL_REVISION'] != rev:
        raise ExpandError(409, '配置已更新，请刷新页面后再试')
    q = next((q for q in h['RECIPES'] if q['id'] == rid), None)
    if not q:
        raise ExpandError(404, '母配方不存在或已被排除')
    skeleton = assemble_recipe(h, q, q['themes'][0])['skeleton']
    system = ('你是3档创意分支策划。每条保留2–4个可辨识的视觉关系，改变主体、场地或局部布局，'
              '不只是换名或改色；若母配方与分支冲突，请把构图、画法和光色写成自洽的可见画面。'
              '只返回JSON对象，branches数组长度必须等于n。每项包含字符串 title, theme, layout, style, lighting，'
              '以及非空字符串数组 kept, changed。title不超过30字，theme不超过80字，layout/style/lighting各不超过180字，'
              'kept/changed各2–4条、每条不超过40字；表达精炼，不重复。不要编造来源，不要复制已有题材。')
    text, usage = expansion_model(system, json.dumps({'n': n, 'skeleton': skeleton,
                       'existing': q['themes']}, ensure_ascii=False), 0.75, 5000,
                       {'type': 'json_object'})
    if catalog()['POOL_REVISION'] != rev:
        raise ExpandError(409, '请求期间配置已更新，请刷新后重试')
    def normalize(s):
        return ''.join(c for c in unicodedata.normalize('NFKC', s).casefold()
                       if not c.isspace() and not unicodedata.category(c).startswith('P'))
    seen = {normalize(t) for r in h['RECIPES'] for t in r['themes']}
    try:
        # 一些兼容服务会给 JSON 附上代码围栏。
        if text.startswith('```') and text.endswith('```'):
            text = text.split(chr(10), 1)[1][:-3].strip()
        items = json.loads(text)['branches']
        if not isinstance(items, list) or len(items) != n:
            raise ValueError('候选数量错误')
        branches = []
        for x in items:
            if not isinstance(x, dict):
                raise ValueError('候选格式错误')
            row = {k: expansion_text(x.get(k), k, limit) for k, limit in
                   [('title', 60), ('theme', 600), ('layout', 800), ('style', 800), ('lighting', 800)]}
            for k in ('kept', 'changed'):
                v = x.get(k)
                if not isinstance(v, list) or not 1 <= len(v) <= 6:
                    raise ValueError('保留/变化字段错误')
                row[k] = [expansion_text(y, k, 180) for y in v]
            norm = normalize(row['theme'])
            if not norm or norm in seen:
                raise ValueError('题材重复')
            seen.add(norm)
            row.update(parent_recipe=rid, source=q['source'], sources=list(q['sources']))
            branches.append(row)
    except (ValueError, KeyError, IndexError, TypeError, ExpandError):
        raise ExpandError(502, '模型候选格式错误、数量不符或题材重复；没有保存或入队') from None
    return {'ok': True, 'level': 3, 'pool_revision': rev, 'branches': branches, 'usage': usage}


def _load_night_module(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + '.py'))
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


night_core = _load_night_module('night_core')
night_worker = _load_night_module('night_worker')
night = night_worker.Worker(night_core, os.path.join(HERE, 'night_state'),
                            current_catalog, lambda: night_core.read(night_core.Path(MANIFEST)),
                            lambda: feedback_view(load_hub(raw=True)), feedback.lock)


_night_tracks = _load_night_module('tracks_api')


def _night_gallery_captions():
    denied = recipe_exclusions(load_hub(raw=True))
    captions = {}
    for row in _night_tracks._load_data():
        source = f"{row['bvid']}/s{row['scene']:02d}"
        if source not in denied:
            if source in captions:
                raise ValueError('图鉴来源重复')
            captions[source] = (row.get('ann') or {}).get('prompt_en')
    return captions


night.gallery_captions = _night_gallery_captions


def build_manifest():
    """重建清单：对 images 不足 2 张的提示词重查 ComfyUI 队列与历史。"""
    def prompts_for(n):
        f = 'zmf_prompts.json' if n == 1 else f'zmf_prompts_batch{n}.json'
        return json.load(open(os.path.join(TMP, f), encoding='utf-8'))['prompts']

    def ids_for(n):
        f = 'queue_ids.txt' if n == 1 else f'queue_ids_b{n}.txt'
        p = os.path.join(TMP, f)
        if not os.path.exists(p):
            return []  # 缺 ids 的批次跳过，不再抛异常（b54-59 尚未跑图）
        return open(p, encoding='utf-8').read().split()

    def get(url):
        try:
            return json.load(urllib.request.urlopen(url, timeout=30))
        except Exception:
            return {}

    queue = get(COMFY + '/queue')
    run_pids = {x[1] for x in queue.get('queue_running', [])}
    pend_pids = {x[1] for x in queue.get('queue_pending', [])}

    # 批次文件动态发现：zmf_prompts.json 为批次1，zmf_prompts_batchN.json 为批次N
    batch_nos = [1]
    for f in glob.glob(os.path.join(TMP, 'zmf_prompts_batch*.json')):
        m = re.match(r'^zmf_prompts_batch(\d+)\.json$', os.path.basename(f))
        if m:
            batch_nos.append(int(m.group(1)))
    batch_nos = sorted(set(batch_nos))

    batches = []
    total_p = total_i = pending = 0
    for n in batch_nos:
        ps = prompts_for(n)
        ids = ids_for(n)
        plist = []
        for i, p in enumerate(ps):
            if i >= len(ids):
                break
            pid = ids[i]
            imgs = []
            if pid not in run_pids and pid not in pend_pids:
                h = get(f'{COMFY}/history/{pid}')
                entry = h.get(pid, {})
                got = {'moss': None, 'krea': None}
                for nid, o in entry.get('outputs', {}).items():
                    for im in o.get('images', []):
                        fn = im.get('filename', '')
                        if im.get('type') == 'output' and not im.get('subfolder') and SAFE_IMG_CHARS.fullmatch(fn):
                            if fn.startswith('MODE01'):
                                got['moss'] = fn
                            elif fn.startswith('MODE02'):
                                got['krea'] = fn
                            elif nid=='437' and re.fullmatch(r'ZMF_DUAL_[A-Za-z0-9_-]+_M1_[0-9]+_?\.png',fn):
                                got['moss'] = fn
                            elif nid=='407' and re.fullmatch(r'ZMF_DUAL_[A-Za-z0-9_-]+_M2_[0-9]+_?\.png',fn):
                                got['krea'] = fn
                for key, mode in (('moss', 'MOSS直出'), ('krea', 'Krea2精修')):
                    fn = got[key]
                    if fn and os.path.exists(os.path.join(OUT, fn)):
                        fp = os.path.join(OUT, fn)
                        imgs.append({'mode': mode, 'file': fn,
                                     'size': os.path.getsize(fp),
                                     'mtime': time.strftime('%Y-%m-%d %H:%M:%S',
                                                            time.localtime(os.path.getmtime(fp)))})
            else:
                pending += 1
            plist.append({'idx': i + 1, 'name': p.get('name', ''), 'ref': p.get('ref', ''),
                          'pid': pid, 'prompt': p['prompt'], 'images': imgs,
                          'pending': len(imgs) < 2 and (pid in run_pids or pid in pend_pids)})
            total_p += 1
            total_i += len(imgs)
        batches.append({'no': n, 'title': ('参考元素 8 景' if n == 1 else '图鉴场景'), 'prompts': plist})

    prev = None
    try:
        prev = json.load(open(MANIFEST, encoding='utf-8'))
    except Exception:
        prev = None
    # 防止 ComfyUI 重启后 history 为空时把已有图片映射整体清空
    if prev and prev.get('totalImages', 0) > 0 and total_i == 0:
        return prev
    # 防覆盖守卫（2026-11-18）：重建结果比现有清单少 20 张以上时拒绝写入。
    # 离线/历史丢失时 history 查不到旧图，直接覆盖会毁掉已恢复的映射（含 900+ 幸存批）。
    if prev and prev.get('totalImages', 0) - total_i > 20:
        print('[守卫] 重建仅 %d 图 < 现有 %d 图，拒绝覆盖，保留现有清单' % (total_i, prev.get('totalImages', 0)))
        return prev
    if prev:
        try:
            json.dump(prev, open(MANIFEST + '.bak', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        except Exception:
            pass
    manifest = {'generated': time.strftime('%Y-%m-%d %H:%M:%S'), 'outputDir': OUT,
                'totalPrompts': total_p, 'totalImages': total_i, 'pending': pending,
                'batches': batches}
    json.dump(manifest, open(MANIFEST, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return manifest


def trash_file(fn):
    if not img_allowed(fn):
        return False, '非法文件名'
    src = os.path.join(OUT, fn)
    if not os.path.exists(src):
        return False, '文件不存在'
    d = os.path.join(TRASH, time.strftime('%Y%m%d_%H%M%S'))
    os.makedirs(d, exist_ok=True)
    shutil.move(src, os.path.join(d, fn))
    return True, '已移入 _trash'


def prune_manifest(files):
    """删除落盘后同步 manifest.json：移除对应图片记录，清空提示词/批次，重算总数。"""
    try:
        m = json.load(open(MANIFEST, encoding='utf-8'))
        fset = set(files)
        for b in m.get('batches', []):
            keep = []
            for p in b.get('prompts', []):
                kept = [i for i in p.get('images', []) if i.get('file') not in fset]
                if kept:
                    p['images'] = kept
                    keep.append(p)
            b['prompts'] = keep
        m['batches'] = [b for b in m.get('batches', []) if b.get('prompts')]
        m['totalPrompts'] = sum(len(b.get('prompts', [])) for b in m['batches'])
        m['totalImages'] = sum(len(p.get('images', [])) for b in m['batches'] for p in b.get('prompts', []))
        json.dump(m, open(MANIFEST, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        return True
    except Exception as e:
        print('prune_manifest failed:', e)
        return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    # 2026-11-18 收紧 CORS：仅允许本机来源，阻止任意网页跨域调用删除/改配置接口。
    ALLOWED_ORIGINS = {'http://127.0.0.1:8191', 'http://localhost:8191',
                       'http://127.0.0.1:8192', 'http://localhost:8192'}

    def _cors_origin(self):
        origin = self.headers.get('Origin', '')
        if origin in self.ALLOWED_ORIGINS:
            return origin
        try:
            parsed = urlparse(origin)
            host = urlparse('//' + self.headers.get('Host', '')).netloc
            if parsed.scheme in ('http', 'https') and parsed.netloc == host:
                return origin
        except ValueError:
            pass
        return None

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        ao = self._cors_origin()
        if ao:
            self.send_header('Access-Control-Allow-Origin', ao)
            self.send_header('Vary', 'Origin')
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(200)
        ao = self._cors_origin()
        if ao:
            self.send_header('Access-Control-Allow-Origin', ao)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _file(self, path, ctype, cache=True):
        try:
            data = open(path, 'rb').read()
        except Exception:
            self._send(404, {'error': 'not found'})
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'max-age=3600' if cache else 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        if path == '/api/night/status': return self._send(200, night.status())
        if path == '/api/ratings': return self.rating_api()
        if path in ('/', '/zmf.html'):
            self.send_response(302)
            self.send_header('Location', '/handraw/gallery/zmf.html')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path == '/index.html':
            return self._file(os.path.join(HERE, 'index.html'), 'text/html; charset=utf-8', cache=False)
        if path == '/manifest.json':
            return self._file(MANIFEST, 'application/json; charset=utf-8', cache=False)
        m = re.match(r'^/img/(.+)$', path)
        if m:
            fn = m.group(1)
            if img_allowed(fn) and os.path.exists(os.path.join(OUT, fn)):
                return self._file(os.path.join(OUT, fn), 'image/png')
            return self._send(404, {'error': 'not found'})
        if path == '/ref':
            q = parse_qs(u.query)
            rel = q.get('path', [''])[0]
            full = os.path.normpath(os.path.join(REF_ROOT, rel))
            if not os.path.splitext(full)[1]:
                for ext in ('_mid.jpg', '.jpg', '.jpeg', '.png', '.webp'):
                    cand = full + ext
                    if os.path.exists(cand):
                        full = cand
                        break
            if full.startswith(os.path.normpath(REF_ROOT)) and os.path.exists(full) and full.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                return self._file(full, 'image/jpeg')
            return self._send(404, {'error': 'not found'})
        if path.startswith('/tujian/thumb/'):
            # 图鉴卡片缩略图：480px JPEG，首次请求懒生成，之后走缓存
            rel = os.path.normpath(unquote(path[len('/tujian/thumb/'):]))
            src = os.path.normpath(os.path.join(REF_ROOT, rel))
            if not src.startswith(os.path.normpath(REF_ROOT)) or not os.path.isfile(src):
                return self._send(404, {'error': 'not found'})
            dst = os.path.normpath(os.path.join(REF_ROOT, 'thumb', rel))
            if not os.path.isfile(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    from PIL import Image
                    with Image.open(src) as im:
                        im = im.convert('RGB')
                        w = 480
                        if im.width > w:
                            im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
                        im.save(dst, 'JPEG', quality=72)
                except Exception:
                    return self._file(src, 'image/jpeg')
            return self._file(dst, 'image/jpeg')
        if path.startswith('/tujian/'):
            rel = os.path.normpath(path[len('/tujian/'):])
            full = os.path.normpath(os.path.join(REF_ROOT, rel))
            if not full.startswith(os.path.normpath(REF_ROOT)) or not os.path.isfile(full):
                return self._send(404, {'error': 'not found'})
            if full.endswith('.js'):
                return self._file(full, 'application/javascript; charset=utf-8', cache=False)
            if full.lower().endswith(('.jpg', '.jpeg')):
                return self._file(full, 'image/jpeg')
            if full.endswith('.png'):
                return self._file(full, 'image/png')
            if full.endswith('.webp'):
                return self._file(full, 'image/webp')
            return self._send(404, {'error': 'not found'})
        if path == '/handraw/gallery/zmf-data.js':
            try:
                catalog = current_catalog()
                payload = 'const ZMF_DATA = ' + json.dumps(catalog, ensure_ascii=True) + ';\n'
                return self._send(200, payload.encode('ascii'), 'application/javascript; charset=utf-8')
            except (ValueError, TypeError, OSError, KeyError) as exc:
                print('读取创作配置失败:', type(exc).__name__, str(exc))
                empty = {'STYLES': [], 'LAYOUTS': [], 'COLORS': [], 'SCENES': [], 'LAMP': {},
                         'COMPGUIDE': {}, 'GROUPORDER': [], 'GROUPZH': {}, 'RECIPES': [], 'LOAD_ERROR': True}
                payload = 'const ZMF_DATA = ' + json.dumps(empty) + ';\n'
                return self._send(200, payload.encode('ascii'), 'application/javascript; charset=utf-8')
        if path == '/handraw/gallery/zmf-manifest.json':
            return self._file(os.path.join(HANDRAW, 'gallery', 'zmf-manifest.json'), 'application/manifest+json; charset=utf-8', cache=False)
        if path.startswith('/handraw/'):
            rel = os.path.normpath(path[len('/handraw/'):])
            full = os.path.normpath(os.path.join(HANDRAW, rel))
            if not full.startswith(os.path.normpath(HANDRAW)) or not os.path.isfile(full):
                return self._send(404, {'error': 'not found'})
            ctype = 'text/html; charset=utf-8' if full.endswith('.html') else \
                    'application/javascript' if full.endswith('.js') else \
                    'application/json; charset=utf-8' if full.endswith('.json') else \
                    'image/webp' if full.endswith('.webp') else \
                    'image/png' if full.endswith('.png') else \
                    'image/jpeg' if full.endswith(('.jpg', '.jpeg')) else \
                    'text/plain; charset=utf-8'
            return self._file(full, ctype, cache=not (full.endswith('.html') or full.endswith('.js')))
        if path.startswith('/images/'):
            rel = os.path.normpath(path[len('/images/'):])
            full = os.path.normpath(os.path.join(HANDRAW, 'images', rel))
            if not full.startswith(os.path.normpath(HANDRAW)) or not os.path.isfile(full):
                return self._send(404, {'error': 'not found'})
            ctype = 'image/webp' if full.endswith('.webp') else \
                    'image/png' if full.endswith('.png') else \
                    'image/jpeg'
            return self._file(full, ctype)
        if path.startswith('/api/llmconfig'):
            cfg = json.load(open(LLM_CONFIG, encoding='utf-8')) if os.path.exists(LLM_CONFIG) else {}
            key = cfg.get('key', '')
            return self._send(200, {'ok': True, 'endpoint': cfg.get('endpoint', ''),
                                    'model': cfg.get('model', ''), 'hasKey': bool(key),
                                    'keyMasked': (key[:8] + '...' + key[-4:]) if len(key) > 16 else key})
        if path in ('/api/tracks', '/api/track'):
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    'tracks_api', os.path.join(HERE, 'tracks_api.py'))
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)
                code, result = m.handle(path, parse_qs(u.query, keep_blank_values=True))
            except Exception as e:
                code, result = 500, {'ok': False, 'msg': str(e)}
            return self._send(code, result)
        if path == '/api/stats':
            return self._send(200, {'running': 'ok', 'time': time.strftime('%H:%M:%S')})
        if path == '/api/gallery_cascade':
            try:
                state = json.load(open(CASCADE_STATE, encoding='utf-8-sig')) if os.path.exists(CASCADE_STATE) else None
                return self._send(200, {'ok': True, 'exists': state is not None,
                                        'items': state.get('items', []) if state else []})
            except (OSError, ValueError, TypeError) as exc:
                print('读取图鉴级联删除失败:', type(exc).__name__, str(exc))
                return self._send(500, {'ok': False, 'msg': '读取图鉴删除状态失败'})
        if path == '/api/hub':
            try:
                hub = current_catalog()
                auto = [q for q in hub['RECIPES'] if q['auto']]
                return self._send(200, {'ok': True, 'version': hub.get('VERSION', ''),
                    'pool_revision': hub['POOL_REVISION'], 'excluded_count': hub['EXCLUDED_COUNT'],
                    'recipe_count': len(hub['RECIPES']), 'auto_count': len(auto),
                    'theme_count': sum(len(q['themes']) for q in auto), 'counts': {
                    'styles': len(hub['STYLES']), 'layouts': len(hub['LAYOUTS']),
                    'colors': len(hub['COLORS']), 'scenes': len(hub['SCENES'])},
                    'LAMP': hub['LAMP'], 'COMPGUIDE': hub['COMPGUIDE'],
                    'GROUPORDER': hub.get('GROUPORDER', []), 'GROUPZH': hub.get('GROUPZH', {})})
            except (ValueError, TypeError, OSError, KeyError) as exc:
                print('读取创作配置失败:', type(exc).__name__, str(exc))
                return self._send(500, {'ok': False, 'msg': '创作配置或不借鉴清单读取失败'})
        if path == '/api/queue':
            return self._send(200, {'ok': True, **queue_busy()})
        if path == '/api/create_log':
            try:
                log = json.load(open(CREATE_LOG, encoding='utf-8')) if os.path.exists(CREATE_LOG) else []
            except Exception:
                log = []
            return self._send(200, {'ok': True, 'entries': log[-50:]})
        if path == '/api/preferences':
            try:
                log = json.load(open(CREATE_LOG, encoding='utf-8')) if os.path.exists(CREATE_LOG) else []
            except Exception:
                log = []
            from collections import defaultdict
            combo = defaultdict(lambda: {'n': 0, 'kept': 0, 'score': 0.0})
            for e in log:
                k = (e.get('color', ''), e.get('comp', ''), e.get('style', ''))
                if not any(k):
                    continue
                c = combo[k]
                c['n'] += 1
                if e.get('kept'):
                    c['kept'] += 1
                sc = e.get('scores') or {}
                c['score'] += float(sc.get('total', 0) or 0)
            rows = [{'color': k[0], 'comp': k[1], 'style': k[2], 'n': v['n'], 'kept': v['kept'],
                     'avg': round(v['score'] / v['n'], 1) if v['n'] else 0}
                    for k, v in combo.items()]
            rows.sort(key=lambda r: (-r['kept'], -r['avg']))
            return self._send(200, {'ok': True, 'combos': rows[:20], 'samples': len(log)})
        if path.startswith('/gen/'):
            fn = path[len('/gen/'):]
            if re.match(r'^ZMF_[A-Za-z0-9_\-]+\.png$', fn) and os.path.exists(os.path.join(OUT, fn)):
                return self._file(os.path.join(OUT, fn), 'image/png')
            return self._send(404, {'error': 'not found'})
        if path.startswith('/api/task'):
            pid = parse_qs(u.query).get('id', [''])[0]
            if not pid:
                return self._send(400, {'ok': False, 'msg': 'missing id'})
            entry = self._comfy(f'/history/{pid}').get(pid)
            if not entry:
                q = self._comfy('/queue')
                if any(x[1] == pid for x in q.get('queue_running', []) + q.get('queue_pending', [])):
                    return self._send(200, {'ok': True, 'state': 'running'})
                return self._send(200, {'ok': True, 'state': 'queued'})
            st = entry.get('status', {})
            files = [im.get('filename') for nid, o in entry.get('outputs', {}).items()
                     for im in o.get('images', []) if im.get('type') == 'output']
            return self._send(200, {'ok': True, 'state': st.get('status_str', '?'),
                                    'files': [f for f in files if f.startswith('ZMF_') or f.startswith('MODE0')] or files})
        return self._send(404, {'error': 'not found'})

    def _comfy(self, path, payload=None):
        url = COMFY + path
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            return {'error': str(e)}

    def do_HEAD(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        if path.startswith('/img/'):
            fn = path[len('/img/'):]
            full = os.path.join(OUT, fn) if SAFE_IMG.match(fn) else None
        elif path.startswith('/handraw/'):
            rel = os.path.normpath(path[len('/handraw/'):])
            full = os.path.normpath(os.path.join(HANDRAW, rel)) if not rel.startswith('..') else None
        elif path.startswith('/images/'):
            rel = os.path.normpath(path[len('/images/'):])
            full = os.path.normpath(os.path.join(HANDRAW, 'images', rel))
        elif path == '/ref':
            q = parse_qs(u.query)
            full = os.path.normpath(os.path.join(REF_ROOT, q.get('path', [''])[0]))
        else:
            full = None
        ok = full and os.path.normpath(full).startswith(os.path.normpath(OUT)) or \
             (full and (os.path.normpath(full).startswith(os.path.normpath(REF_ROOT)) or
                        os.path.normpath(full).startswith(os.path.normpath(HANDRAW))))
        self.send_response(200 if ok and full and os.path.isfile(full) else 404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _read_body(self):
        te = (self.headers.get('Transfer-Encoding') or '').lower()
        if 'chunked' in te:
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                try:
                    size = int(line.split(b';')[0], 16)
                except Exception:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b''.join(chunks)
        n = int(self.headers.get('Content-Length', 0) or 0)
        return self.rfile.read(n) if n else b''

    def rating_api(self, write=False):
        try:
            body=None
            if write:
                if self.headers.get('Origin') and not self._cors_origin():
                    return self._send(403, {'ok':False,'msg':'来源不允许'})
                try:
                    if self.headers.get_content_type()!='application/json' or not 0<int(self.headers.get('Content-Length','0'))<=2048:
                        raise ValueError('需要不超过 2KB 的 JSON')
                    body=json.loads(self._read_body().decode('utf8'), object_pairs_hook=feedback.unique)
                    if not isinstance(body,dict): raise ValueError('需要 JSON 对象')
                except ValueError as exc: raise feedback.Invalid(str(exc))
            return self._send(200,feedback_view(load_hub(raw=True),body))
        except feedback.Invalid as exc:
            return self._send(400,{'ok':False,'msg':str(exc)})
        except Exception as exc:
            return self._send(503,{'ok':False,'msg':'评分数据不可用：'+type(exc).__name__})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path in ('/api/night/preview', '/api/night/start', '/api/night/stop', '/api/night/resume'):
            if self.headers.get('Origin') and not self._cors_origin():
                return self._send(403, {'ok': False, 'msg': '来源不允许'})
            try:
                if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding') or not 0 < int(self.headers.get('Content-Length', '0')) <= 4_000_000:
                    raise ValueError('需要有限大小 JSON 清单')
                self.connection.settimeout(10)
                obj = json.loads(self._read_body(), object_pairs_hook=feedback.unique)
                if not isinstance(obj, dict): raise ValueError('需要 JSON 对象')
                action = u.path.rsplit('/', 1)[-1]
                if action == 'start' and obj.get('confirm') is not True:
                    raise ValueError('请确认启动')
                if action == 'stop': result = night.stop()
                elif action == 'resume': result = night.resume()
                else: result = night.start(obj, preview=action == 'preview')
                return self._send(200, result)
            except (ValueError, KeyError, TypeError, OSError, ImportError) as exc:
                return self._send(400, {'ok': False, 'msg': str(exc)[:200]})
        if u.path == '/api/ratings': return self.rating_api(True)
        body = {}
        raw = self._read_body()
        if raw:
            try:
                body = json.loads(raw.decode('utf-8'))
            except Exception:
                body = {}
        if u.path == '/api/refresh':
            m = build_manifest()
            return self._send(200, {'ok': True, 'generated': m['generated'],
                                    'totalImages': m['totalImages'], 'pending': m['pending']})
        if u.path in ('/api/assemble', '/api/random'):
            try:
                hub = load_hub()
                denied = recipe_exclusions(hub)
            except (ValueError, TypeError, OSError, KeyError) as exc:
                print('读取创作配置失败:', type(exc).__name__, str(exc))
                return self._send(500, {'ok': False, 'msg': '创作配置或不借鉴清单读取失败'})
            try:
                code, result = recipe_response(hub, denied, u.path, body)
            except ValueError as exc:
                code, result = 400, {'ok': False, 'msg': str(exc)}
            except (KeyError, TypeError, StopIteration) as exc:
                print('配方引用损坏:', type(exc).__name__, str(exc))
                code, result = 500, {'ok': False, 'msg': '配方配置不完整'}
            return self._send(code, result)
        if u.path == '/api/gallery_cascade':
            if not isinstance(body, dict) or 'items' not in body:
                return self._send(400, {'ok': False, 'msg': '请提供图鉴删除清单'})
            try:
                result = apply_gallery_cascade(body['items'])
            except ValueError as exc:
                return self._send(400, {'ok': False, 'msg': str(exc)})
            except (OSError, KeyError, TypeError) as exc:
                print('图鉴级联删除失败:', type(exc).__name__, str(exc))
                return self._send(500, {'ok': False, 'msg': '级联删除保存失败，正式四库未确认修改'})
            return self._send(200, result)
        if u.path == '/api/delset_export':
            import tempfile
            if not isinstance(body, dict) or 'items' not in body:
                return self._send(400, {'ok': False, 'msg': '请提供不借鉴清单'})
            try:
                _, items = exclusion_items(body['items'])
            except ValueError as exc:
                return self._send(400, {'ok': False, 'msg': str(exc)})
            from datetime import datetime
            stamp = datetime.now()
            out = {'ts': stamp.isoformat(timespec='seconds'), 'count': len(items), 'items': items}
            fn = os.path.join(TMP, '不借鉴清单_'+stamp.strftime('%Y%m%d_%H%M%S_%f')+'.json')
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=TMP, suffix='.tmp', delete=False) as f:
                    tmp = f.name
                    json.dump(out, f, ensure_ascii=False, indent=1)
                os.replace(tmp, fn)
            except OSError as exc:
                print('清单导出失败:', type(exc).__name__, str(exc))
                if tmp and os.path.isfile(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                return self._send(500, {'ok': False, 'msg': '未能保存不借鉴清单'})
            return self._send(200, {'ok': True, 'saved': fn, 'count': len(items)})
        if u.path == '/api/interrupt':
            # 取消 ComfyUI 当前正在执行的任务（已排队的不受影响）
            try:
                self._comfy('/interrupt', {})
                return self._send(200, {'ok': True, 'msg': '已发送取消指令'})
            except Exception as e:
                return self._send(502, {'ok': False, 'msg': '取消失败: ' + str(e)})
        if u.path == '/api/record':
            entry = {
                'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                'theme': body.get('theme', ''), 'color': body.get('color', ''),
                **{k: body[k] for k in ('recipe', 'source', 'pick_seed', 'cursor', 'seed', 'config_version') if k in body},
                'comp': body.get('comp', ''), 'style': body.get('style', ''),
                'file': body.get('file', ''), 'mode': body.get('mode', ''),
                'scores': body.get('scores', {}), 'verdict': body.get('verdict', ''),
                'kept': bool(body.get('kept')), 'note': body.get('note', ''),
            }
            try:
                log = json.load(open(CREATE_LOG, encoding='utf-8')) if os.path.exists(CREATE_LOG) else []
            except Exception:
                log = []
            log.append(entry)
            json.dump(log, open(CREATE_LOG, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            return self._send(200, {'ok': True, 'total': len(log)})
        if u.path == '/api/delete':
            fn = body.get('file', '')
            ok, msg = trash_file(fn)
            return self._send(200 if ok else 400, {'ok': ok, 'msg': msg, 'file': fn})
        if u.path == '/api/delete_group':
            results = []
            ok_files = []
            for fn in body.get('files', []):
                ok, msg = trash_file(fn)
                results.append({'file': fn, 'ok': ok, 'msg': msg})
                if ok:
                    ok_files.append(fn)
            pruned = prune_manifest(ok_files) if ok_files else False
            for r in results:
                if r['ok']:
                    ADOPTED.discard(r['file'])
            if ok_files:
                save_adopted()
            return self._send(200, {'ok': True, 'results': results, 'pruned': pruned})
        if u.path == '/api/adopt':
            """散图收录：扫描输出目录中未登记的图片，登入「批950 散图收录」。body.dry=true 只预览。"""
            try:
                man = json.load(open(MANIFEST, encoding='utf-8'))
                known = set()
                for b in man.get('batches', []):
                    for p in b.get('prompts', []):
                        for i in (p.get('images') or []):
                            known.add(i.get('file'))
                found = []
                for f in os.listdir(OUT):
                    if not SAFE_IMG_CHARS.match(f):
                        continue
                    if f in known or f in ADOPTED:
                        continue
                    fp = os.path.join(OUT, f)
                    if not os.path.isfile(fp):
                        continue
                    st = os.stat(fp)
                    found.append({'mode': '散图', 'file': f, 'size': st.st_size,
                                  'mtime': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))})
                if body.get('dry') or not found:
                    return self._send(200, {'ok': True, 'added': 0, 'found': len(found)})
                b950 = None
                for b in man.get('batches', []):
                    if b.get('no') == 950:
                        b950 = b
                        break
                if b950 is None:
                    b950 = {'no': 950, 'name': '散图收录', 'prompts': []}
                    man['batches'].append(b950)
                idx = max([p.get('idx', 0) for p in b950['prompts']] or [0])
                for im in found:
                    idx += 1
                    ADOPTED.add(im['file'])
                    b950['prompts'].append({'idx': idx, 'name': '散图·' + im['file'],
                                            'prompt': '', 'images': [im], 'ref': ''})
                save_adopted()
                man['totalPrompts'] = sum(len(b.get('prompts', [])) for b in man['batches'])
                man['totalImages'] = sum(len(p.get('images', [])) for b in man['batches'] for p in b.get('prompts', []))
                json.dump(man, open(MANIFEST, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
                return self._send(200, {'ok': True, 'added': len(found), 'found': len(found),
                                        'totalImages': man['totalImages']})
            except Exception as e:
                return self._send(500, {'ok': False, 'msg': '收录失败: ' + str(e)})
        if u.path in ('/api/expand', '/api/creative_branches'):
            try:
                return self._send(200, expand_response(u.path, body))
            except ExpandError as exc:
                return self._send(exc.status, {'ok': False, 'msg': exc.message})
        if u.path == '/api/llmconfig':
            cfg = json.load(open(LLM_CONFIG, encoding='utf-8')) if os.path.exists(LLM_CONFIG) else {}
            if body.get('update'):
                for k in ('endpoint', 'key', 'model'):
                    if body['update'].get(k) is not None:
                        cfg[k] = body['update'][k]
                json.dump(cfg, open(LLM_CONFIG, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            cfg = json.load(open(LLM_CONFIG, encoding='utf-8'))
            key = cfg.get('key', '')
            return self._send(200, {'ok': True, 'endpoint': cfg.get('endpoint', ''),
                                    'model': cfg.get('model', ''), 'hasKey': bool(key),
                                    'keyMasked': (key[:8] + '...' + key[-4:]) if len(key) > 16 else key})
        if u.path == '/api/generate':
            prompt = (body.get('prompt') or '').strip()
            if not prompt:
                return self._send(400, {'ok': False, 'msg': '提示词为空'})
            seed = body.get('seed') or int(time.time()) % 2**31
            # 2026-11-18：默认快稿改走 MOSS 首采厚涂路（纯 Krea2 turbo 偏动漫/CG）。
            # 仅 mode=krea2 时才用下方旧 Krea2 配方（探索用）。
            if (body.get('mode') or 'moss') != 'krea2':
                qb = queue_busy()
                if not qb['reachable']:
                    return self._send(502, {'ok': False, 'msg': 'ComfyUI 不可达'})
                if qb['running'] or qb['pending']:
                    return self._send(409, {'ok': False, 'msg': '队列占用，让队', **qb})
                tpl = os.path.join(HERE, 'api_graph_moss.json')
                if not os.path.exists(tpl):
                    return self._send(500, {'ok': False, 'msg': '缺 api_graph_moss.json'})
                graph = json.load(open(tpl, encoding='utf-8'))
                graph['76']['inputs']['value'] = prompt
                graph['16']['inputs']['seed'] = seed
                graph['415']['inputs']['value'] = False
                r = self._comfy('/prompt', {'prompt': graph, 'client_id': 'zmf-create'})
                if r.get('error'):
                    return self._send(502, {'ok': False, 'msg': '提交失败', 'detail': r.get('error')})
                return self._send(200, {'ok': True, 'prompt_id': r.get('prompt_id'), 'mode': 'moss'})
            # 2026-11-18 修复风格跑偏：对齐 MJ4.5 工作流 Krea2 二采的已验证配方。
            # 旧版无 LoRA + euler/simple + 空负向 → 出写实/动漫图（非 ZMF）。
            # 现补 3 个风格 LoRA（TJFilmStyleKrea2_V2@0.9 / 影视柔光@0.55 / 风格滤镜@0.8）
            # 并改 er_sde/beta，与节点 276 一致；空负向保留（turbo cfg1 惯例）。
            graph = {
                "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "Krea2\\krea2_turbo_int8_convrot.safetensors", "weight_dtype": "default"}},
                "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors", "type": "krea2", "device": "default"}},
                "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
                "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
                "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
                "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1344, "height": 768, "batch_size": 1}},
                "10": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "krea2\\TJFilmStyleKrea2_V2.safetensors", "strength_model": 0.9}},
                "11": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["10", 0], "lora_name": "krea2\\krea2-Cc-MJ-影视柔光.safetensors", "strength_model": 0.55}},
                "12": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["11", 0], "lora_name": "krea2\\krea2-Cc-MJ-风格滤镜.safetensors", "strength_model": 0.8}},
                "7": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 8, "cfg": 1.0, "sampler_name": "er_sde",
                       "scheduler": "beta", "denoise": 1.0, "model": ["12", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
                "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
                "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ZMF_DIRECT", "images": ["8", 0]}},
            }
            r = self._comfy('/prompt', {'prompt': graph, 'client_id': 'zmf-gallery'})
            if r.get('error'):
                return self._send(502, {'ok': False, 'msg': 'ComfyUI 未运行或提交失败', 'detail': r.get('error')})
            return self._send(200, {'ok': True, 'prompt_id': r.get('prompt_id')})
        if u.path == '/api/generate_moss':
            prompt = (body.get('prompt') or '').strip()
            if not prompt:
                return self._send(400, {'ok': False, 'msg': '提示词为空'})
            qb = queue_busy()
            if not qb['reachable']:
                return self._send(502, {'ok': False, 'msg': 'ComfyUI 不可达'})
            if qb['running'] or qb['pending']:
                return self._send(409, {'ok': False, 'msg': '队列占用，让队', **qb})
            tpl_path = os.path.join(HERE, 'api_graph_moss.json')
            if not os.path.exists(tpl_path):
                return self._send(500, {'ok': False, 'msg': '缺 api_graph_moss.json 模板'})
            graph = json.load(open(tpl_path, encoding='utf-8'))
            seed = body.get('seed') or int(time.time()) % 2**31
            graph['76']['inputs']['value'] = prompt
            graph['16']['inputs']['seed'] = seed
            graph['415']['inputs']['value'] = False
            r = self._comfy('/prompt', {'prompt': graph, 'client_id': 'zmf-create-moss'})
            if r.get('error'):
                return self._send(502, {'ok': False, 'msg': '提交失败', 'detail': r.get('error')})
            return self._send(200, {'ok': True, 'prompt_id': r.get('prompt_id'), 'mode': 'moss'})
        if u.path == '/api/generate_dual':
            if 'recipe' in body or 'pool_revision' in body:
                qid, rev = body.get('recipe'), body.get('pool_revision')
                if not isinstance(qid, str) or not qid or not isinstance(rev, str) or not rev:
                    return self._send(400, {'ok': False, 'msg': '自动出图缺少配方或配置版本'})
                try:
                    current = current_catalog()
                except (ValueError, TypeError, OSError, KeyError):
                    return self._send(500, {'ok': False, 'msg': '创作配置或不借鉴清单读取失败'})
                if rev != current['POOL_REVISION'] or not any(q['id'] == qid and q['auto'] for q in current['RECIPES']):
                    return self._send(409, {'ok': False, 'msg': '配方已更改或被排除，未提交队列'})
            prompt = (body.get('prompt') or '').strip()
            if not prompt:
                return self._send(400, {'ok': False, 'msg': '提示词为空'})
            qb = queue_busy()
            if not qb['reachable']:
                return self._send(502, {'ok': False, 'msg': 'ComfyUI 不可达'})
            if qb['running'] or qb['pending']:
                return self._send(409, {'ok': False, 'msg': '队列占用，让队', **qb})
            tpl_path = os.path.join(HERE, 'api_graph_dual.json')
            if not os.path.exists(tpl_path):
                return self._send(500, {'ok': False, 'msg': '缺 api_graph_dual.json 模板'})
            graph = json.load(open(tpl_path, encoding='utf-8'))
            seed = body.get('seed')
            if seed is None:
                seed = int(time.time()) % 2**31
            # 注入提示词与种子；强制文生图（415=false），不改采样/LoRA/分辨率
            graph['76']['inputs']['value'] = prompt
            graph['16']['inputs']['seed'] = seed
            graph['276']['inputs']['noise_seed'] = seed
            graph['415']['inputs']['value'] = False
            # 干净输出前缀（API 图不解析 %date% 占位），tag 仅允许安全字符
            tag = re.sub(r'[^A-Za-z0-9_\-]', '', str(body.get('tag') or ''))[:40] or str(seed)
            graph['437']['inputs']['filename_prefix'] = 'ZMF_DUAL_%s_M1' % tag
            graph['407']['inputs']['filename_prefix'] = 'ZMF_DUAL_%s_M2' % tag
            # 可选：MP 直设 + 角色一致性参考图（洗图支路锁构图）；不传则行为与原版一致
            mp = body.get('mp')
            if mp:
                try:
                    mp = float(mp)
                except Exception:
                    mp = 0
                if 0.3 <= mp <= 4.0:
                    graph['19']['inputs']['megapixels'] = mp
                    ar = str(graph['19']['inputs'].get('aspect_ratio', '16:9'))
                    m = re.match(r'^\s*(\d+)\s*:\s*(\d+)', ar)
                    rw, rh = (int(m.group(1)), int(m.group(2))) if m else (16, 9)
                    longside = int((mp * 1e6 * max(rw, rh) / min(rw, rh)) ** 0.5)
                    graph['420']['inputs']['scale_to_length'] = longside - longside % 8
            ref = body.get('ref_image')
            if ref:
                if not re.match(r'^[A-Za-z0-9_\-\.]+\.(png|jpg|jpeg|webp)$', str(ref), re.I):
                    return self._send(400, {'ok': False, 'msg': '参考图文件名不合法'})
                graph['419']['inputs']['image'] = ref
                graph['415']['inputs']['value'] = True
                graph['417']['inputs']['value'] = float(body.get('ref_denoise') or 0.75)
            r = self._comfy('/prompt', {'prompt': graph, 'client_id': 'zmf-create-dual'})
            if r.get('error'):
                return self._send(502, {'ok': False, 'msg': '提交失败', 'detail': r.get('error')})
            return self._send(200, {'ok': True, 'prompt_id': r.get('prompt_id'), 'mode': 'dual'})
        if u.path == '/api/upload_ref':
            # 代理上传参考图到 ComfyUI input（角色一致性用）
            # 注意：do_POST 开头已读过 body，这里必须复用 raw，二次读取会阻塞
            raw_img = raw
            fn = parse_qs(u.query).get('name', [''])[0]
            if not raw_img or not fn:
                return self._send(400, {'ok': False, 'msg': '缺少图片或文件名'})
            fn = re.sub(r'[^A-Za-z0-9_\-\.]', '', fn)[:80]
            if not re.search(r'\.(png|jpg|jpeg|webp)$', fn, re.I):
                return self._send(400, {'ok': False, 'msg': '仅支持 png/jpg/webp'})
            boundary = 'zmfref%d' % int(time.time() * 1000)
            parts = []
            parts.append(('--%s\r\nContent-Disposition: form-data; name="image"; filename="%s"\r\n'
                          'Content-Type: application/octet-stream\r\n\r\n' % (boundary, fn)).encode())
            parts.append(raw_img)
            parts.append(('\r\n--%s\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
                          '--%s--\r\n' % (boundary, boundary)).encode())
            payload = b''.join(parts)
            req = urllib.request.Request(COMFY + '/upload/image', data=payload,
                                         headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    j = json.loads(r.read())
                return self._send(200, {'ok': True, 'name': j.get('name', fn)})
            except Exception as e:
                return self._send(502, {'ok': False, 'msg': '上传失败: %s' % e})
        if u.path == '/api/task':
            pid = body.get('prompt_id') or (parse_qs(u.query).get('id', [''])[0])
            if not pid:
                return self._send(400, {'ok': False, 'msg': '缺少 id'})
            h = self._comfy(f'/history/{pid}')
            entry = h.get(pid)
            if not entry:
                q = self._comfy('/queue')
                if any(x[1] == pid for x in q.get('queue_running', []) + q.get('queue_pending', [])):
                    return self._send(200, {'ok': True, 'state': 'running'})
                return self._send(200, {'ok': True, 'state': 'queued'})
            st = entry.get('status', {})
            files = [im.get('filename') for nid, o in entry.get('outputs', {}).items()
                     for im in o.get('images', []) if im.get('type') == 'output']
            return self._send(200, {'ok': True, 'state': st.get('status_str', '?'),
                                    'files': [f for f in files if f.startswith('ZMF_') or f.startswith('MODE0')] or files})
        return self._send(404, {'error': 'not found'})


REALTIME_NO = 949  # 直跑实时组：ZMF_DUAL_* 落盘后 ≤60s 进图库，正式归批后自动移除


def realtime_sync():
    """增量收录 OUT 里未登记的新图到批949「直跑实时」，并清理已正式归批的条目。
    线程每 30s 跑一次；原子写盘，无变化不动 manifest。"""
    man = json.load(open(MANIFEST, encoding='utf-8'))
    batches = man.setdefault('batches', [])
    known = set()          # 正式批次（非 949/950）已登记文件
    rt = None
    for b in batches:
        if b.get('no') == REALTIME_NO:
            rt = b
            continue
        if b.get('no') == 950:
            continue
        for p in b.get('prompts', []):
            for i in (p.get('images') or []):
                known.add(i.get('file'))
    changed = False
    if rt is None:
        rt = {'no': REALTIME_NO, 'name': '直跑实时', 'prompts': []}
        batches.append(rt)
        changed = True
    # 1. 清理：949 里已正式归批的图移除
    for p in rt.get('prompts', []):
        alive = [i for i in (p.get('images') or []) if i.get('file') not in known]
        if len(alive) != len(p.get('images') or []):
            p['images'] = alive
            changed = True
    rt['prompts'] = [p for p in rt.get('prompts', []) if p.get('images')]
    # 2. 收录：磁盘新图（未正式归批、未在 949）
    rt_files = {i.get('file') for p in rt.get('prompts', []) for i in (p.get('images') or [])}
    groups = {}
    try:
        names = os.listdir(OUT)
    except OSError:
        names = []
    for f in names:
        if not f.startswith('ZMF_DUAL_'):
            continue  # 949 只收直跑流产物；其他散图走 /api/adopt 手动收录
        if not SAFE_IMG_CHARS.match(f) or f in known or f in rt_files:
            continue
        fp = os.path.join(OUT, f)
        if not os.path.isfile(fp):
            continue
        try:
            st = os.stat(fp)
        except OSError:
            continue
        m = re.match(r'^ZMF_DUAL_([A-Za-z0-9_\-]+?)_M[12]_', f)
        key = ('直跑·' + m.group(1)) if m else ('散图·' + f[:40])
        groups.setdefault(key, []).append(
            {'mode': 'MOSS直出' if f.startswith(('MODE01', 'ZMF_DUAL')) and '_M1_' in f else 'Krea2精修',
             'file': f, 'size': st.st_size,
             'mtime': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))})
    if groups:
        idx = max([p.get('idx', 0) for p in rt['prompts']] or [0])
        by_name = {p.get('name'): p for p in rt['prompts']}
        for key, imgs in sorted(groups.items()):
            exist = by_name.get(key)
            if exist is not None:
                have = {i.get('file') for i in (exist.get('images') or [])}
                add = [i for i in imgs if i['file'] not in have]
                if add:
                    exist['images'].extend(add)
                    changed = True
                continue
            idx += 1
            rt['prompts'].append({'idx': idx, 'name': key, 'prompt': '', 'ref': '',
                                  'images': imgs, 'pending': False})
            by_name[key] = rt['prompts'][-1]
            changed = True
    if not changed:
        return 0
    man['totalPrompts'] = sum(len(b.get('prompts', [])) for b in batches)
    man['totalImages'] = sum(len(p.get('images', [])) for b in batches for p in b.get('prompts', []))
    man['generated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    tmp = MANIFEST + '.tmp_rt'
    json.dump(man, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    os.replace(tmp, MANIFEST)
    return 1


def realtime_loop():
    while True:
        try:
            if realtime_sync():
                print('[realtime] manifest 增量更新', flush=True)
        except Exception as e:
            print('[realtime] sync error:', e, flush=True)
        time.sleep(30)


if __name__ == '__main__':
    load_adopted()
    if not os.path.exists(MANIFEST):
        # 首次运行：从旧清单继承，保证已有数据不丢
        try:
            shutil.copy(os.path.join(TMP, 'gallery', 'manifest.json'), MANIFEST)
        except Exception:
            pass
        build_manifest()
    srv = ThreadingHTTPServer(('127.0.0.1', 8191), Handler)
    threading.Thread(target=realtime_loop, daemon=True).start()
    print('gallery v2 on http://127.0.0.1:8191/  (realtime sync: 30s)')
    srv.serve_forever()
