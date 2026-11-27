"""有限清单双采夜跑。默认只预检，--run 才提交；不修改工作流。"""
import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import unicodedata
from urllib.request import urlopen, Request

ROOT = Path('C:/tmp_zmf')
GALLERY = 'http://127.0.0.1:8191'
COMFY = 'http://127.0.0.1:8188'
LABEL = re.compile(r'[A-Za-z0-9_-]{1,80}')


def canonical(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))


class Used:
    def __init__(self):
        self.ids, self.words = set(), set()

    def add(self, ident, caption):
        if ident:
            self.ids.add(str(ident).casefold())
        if caption:
            self.words.add(hashlib.sha256(canonical(caption).encode()).hexdigest())

    def has(self, ident, caption):
        return (str(ident).casefold() in self.ids or
                hashlib.sha256(canonical(caption).encode()).hexdigest() in self.words)


def check(row, recipes):
    if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not LABEL.fullmatch(row['id']):
        raise ValueError('题材 ID 无效')
    if not isinstance(row.get('recipe'), str) or row['recipe'] not in recipes:
        raise ValueError(f"{row['id']} 没有明确的、当前可用的正式配方 ID")
    text = row.get('caption')
    if not isinstance(text, str) or not canonical(text) or len(text) > 12000:
        raise ValueError(f"{row['id']} 缺有效成稿")
    return row['id'], text.strip(), row['recipe']


def registered(manifest, files):
    found = []
    for b in manifest.get('batches', []):
        for p in b.get('prompts', []):
            found.append([(i.get('file'), i.get('mode', '')) for i in p.get('images', []) if i.get('file') in files])
    matches = [group for group in found if group]
    return (len(matches) == 1 and len(matches[0]) == 2 and
            {f for f, _ in matches[0]} == set(files) and
            all(mode.startswith('MOSS' if f == files[0] else 'Krea2') for f, mode in matches[0]))


def completed(entry, tag):
    if entry.get('status', {}).get('status_str') != 'success':
        raise ValueError('任务未成功')
    result = []
    for node, stage in [('437', 'M1'), ('407', 'M2')]:
        pat = re.compile(rf'ZMF_DUAL_{re.escape(tag)}_{stage}_[0-9]+_?\.png')
        names = [i['filename'] for i in entry.get('outputs', {}).get(node, {}).get('images', [])
                 if i.get('type') == 'output' and not i.get('subfolder') and pat.fullmatch(i.get('filename', ''))]
        if len(names) != 1:
            raise ValueError(f'保存节点 {node} 未返回唯一匹配文件')
        result.extend(names)
    return result


def read(path, default=None):
    if default is not None and not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.night-')
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def run_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as f:
            f.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def request(base, route, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf8')
    req = Request(base + route, data=data, headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=30) as resp:
        return resp.read()


def api(base, route, body=None):
    return json.loads(request(base, route, body))


def binding(path, files, recipe, pid):
    rows = read(path, {})
    for f, stage in zip(files, ('MOSS', 'Krea2')):
        value = {'recipe': recipe, 'group': 'night-' + pid, 'stage': stage}
        if f in rows and rows[f] != value:
            raise ValueError('文件已有不同评分绑定：' + f)
        rows[f] = value
    save(path, rows)


def image_signature(path):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        rgb = ImageOps.exif_transpose(im).convert('RGB')
        pixel = hashlib.sha256(f'{rgb.width}x{rgb.height}:'.encode() + rgb.tobytes()).hexdigest()
        thumb = rgb.resize((16, 16), Image.Resampling.LANCZOS).tobytes().hex()
    return {'pixel': pixel, 'thumb': thumb}


def near(a, b):
    if a['pixel'] == b['pixel']:
        return True
    x, y = bytes.fromhex(a['thumb']), bytes.fromhex(b['thumb'])
    return sum(abs(m - n) for m, n in zip(x, y)) / len(x) <= 6


def recover(row, ledger, state, timeout=1800):
    pid, tag = row.get('prompt_id'), row['tag']
    if not isinstance(pid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', pid):
        raise ValueError(f'提交回执不明：{tag}。先查队列/history，不自动重发。')
    files = row.get('files')
    if not files:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            entry = api(COMFY, '/history/' + pid).get(pid)
            if entry and entry.get('status', {}).get('completed'):
                files = completed(entry, tag)
                break
            if entry and entry.get('status', {}).get('status_str') == 'error':
                raise ValueError('ComfyUI 报错：' + pid)
            time.sleep(12)
        if not files:
            raise ValueError('等待 history 超时，不重发：' + pid)
        row['files'] = files
        save(ledger, state)
    end = time.monotonic() + 100
    while True:
        manifest = api(GALLERY, '/manifest.json')
        out = Path(manifest['outputDir'])
        if registered(manifest, files) and all((out / f).is_file() and (out / f).stat().st_size for f in files):
            break
        if time.monotonic() > end:
            raise ValueError('双采文件尚未在图库唯一同组登记；保留台账，勿重发。')
        time.sleep(10)
    signatures = [image_signature(out / f) for f in files]
    # 同题 M1/M2 本来就应相似；只拦完全相同。同工具跨题则检查近似。
    previous = [sig for item in state if item is not row for sig in item.get('image_signatures', [])]
    if signatures[0]['pixel'] == signatures[1]['pixel'] or any(near(sig, old) for sig in signatures for old in previous):
        row['status'] = 'duplicate_output'
        save(ledger, state)
        raise ValueError('发现在本工具覆盖范围内像素相同或缩图近似的产物；已停止，不删图、不绑定评分。')
    binding(ROOT/'gallery/人物挑片映射.json', files, row['recipe'], pid)
    ratings = api(GALLERY, '/api/ratings').get('ratings', {})
    if not all(f in ratings for f in files):
        raise ValueError('图库已登记，但评分接口未认定两张图可反馈；停止，不重发。')
    row.update(status='done', image_signatures=signatures)
    save(ledger, state)
    print('已验收图库与评分资格', row['id'], *files, flush=True)


def run(args):
    work = Path(__file__).resolve().parent
    ledger = work/'夜跑台账.json'
    plan = read(args.plan)
    if not isinstance(plan, dict) or not isinstance(plan.get('items'), list) or not plan['items']:
        raise ValueError('清单须是包含非空 items 的 JSON 对象')
    def catalog():
        text = request(GALLERY, '/handraw/gallery/zmf-data.js').decode('utf8')
        return json.JSONDecoder().raw_decode(text[text.index('{'):])[0]
    with run_lock(work/'夜跑.lock'):
        state = read(ledger, [])
        if args.run:
            try:
                from PIL import Image
            except ImportError:
                raise ValueError('请使用已包含 Pillow 的本机 Python；未提交') from None
            mapping = ROOT/'gallery/人物挑片映射.json'
            if mapping.exists():
                save(work/f'评分绑定备份_{time.time_ns()}.json', read(mapping))
            for row in state:
                if row['status'] == 'duplicate_output':
                    raise ValueError('台账有待人工复核的重复产物')
                if row['status'] != 'done':
                    recover(row, ledger, state)
        h = catalog()
        recipes = {q['id'] for q in h['RECIPES'] if q.get('auto')}
        used = Used()
        for row in state:
            used.add(row['id'], row['caption'])
        for batch in api(GALLERY, '/manifest.json')['batches']:
            for row in batch.get('prompts', []):
                used.add(None, row.get('prompt') or '')
        for file in ROOT.glob('night_*/*ledger.json'):
            for row in read(file):
                used.add(row.get('id'), row.get('caption') or '')
        for row in read(ROOT/'simple_create_records.json', []):
            used.add(row.get('id'), row.get('prompt') or row.get('caption') or row.get('skeleton') or '')
        accepted = []
        for row in plan['items']:
            ident, caption, recipe = check(row, recipes)
            if used.has(ident, caption):
                print('跳过已用题材或词', ident)
                continue
            used.add(ident, caption)
            accepted.append((ident, caption, recipe))
        print(f'预检通过 {len(accepted)} 条；本次最多提交 {min(args.max_groups,len(accepted))} 组。', flush=True)
        if not args.run:
            return
        deadline = time.monotonic() + args.hours * 3600
        for ident, caption, recipe in accepted[:args.max_groups]:
            q = api(GALLERY, '/api/queue')
            if time.monotonic() >= deadline or not q.get('reachable') or q.get('running') or q.get('pending'):
                print('到点或队列不可用，停止提交。'); return
            h = catalog()
            if not any(x['id'] == recipe and x.get('auto') for x in h['RECIPES']):
                raise ValueError('配方已暂停或排除：' + recipe)
            while True:
                tag = 'NIGHT_' + secrets.token_hex(10)
                if all(r['tag'] != tag for r in state):
                    break
            row = dict(id=ident, caption=caption, recipe=recipe, tag=tag,
                       seed=secrets.randbelow(2**31-1)+1, status='submitting')
            state.append(row)
            save(ledger, state)
            res = api(GALLERY, '/api/generate_dual', {'prompt': caption, 'recipe': recipe,
                      'pool_revision': h['POOL_REVISION'], 'tag': tag, 'seed': row['seed']})
            if not res.get('ok') or not res.get('prompt_id'):
                raise ValueError('未拿到任务 ID；先人工核对，不重提：' + tag)
            row.update(prompt_id=res['prompt_id'], status='accepted')
            save(ledger, state)
            recover(row, ledger, state)


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('plan', type=Path)
    cli.add_argument('--run', action='store_true')
    cli.add_argument('--hours', type=float, default=6)
    cli.add_argument('--max-groups', type=int, default=60)
    args = cli.parse_args()
    if not 0 < args.hours <= 12 or not 1 <= args.max_groups <= 300:
        cli.error('时间须大于 0 且不超过 12 小时，组数 1–300')
    try:
        run(args)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        cli.exit(1, f'停止：{exc}\n')
