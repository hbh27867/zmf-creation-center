"""图库评分。映射、清单、磁盘、配方四方一致才接受；文件名不用于猜配方。"""
from pathlib import Path
from threading import RLock
from collections import defaultdict, Counter
import json, re, tempfile, os

lock = RLock()
RULE = '每组一票：同组已评阶段取平均，任一1星暂停配方；跨组等权平均。2/3/4/5星权重0.5/1/2/3，中间值线性插值；未评为1。仅在原自动池内生效。'


def unique(pairs):
    d = {}
    for k,v in pairs:
        if k in d: raise ValueError('JSON键重复')
        d[k] = v
    return d


def load(p):
    with p.open(encoding='utf-8-sig') as f: return json.load(f, object_pairs_hook=unique)


def save(p, data):
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix='.ratings-', suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(data,f,ensure_ascii=False,sort_keys=True); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,p)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


class Invalid(ValueError): pass


def view(root, manifest, out, hub, allowed, body=None):
    root, out = Path(root), Path(out).resolve()
    mapping, state = root/'人物挑片映射.json', root/'rating_votes_v1.json'
    with lock:
        rows = load(mapping) if mapping.exists() else {}
        ids = {r['id'] for r in hub['RECIPES']}
        valid, slots, owners = {}, set(), {}
        if rows:
            index = defaultdict(list)
            for bi,b in enumerate(load(Path(manifest))['batches']):
                for pi,p in enumerate(b['prompts']):
                    for im in p['images']:
                        if im['file'] in rows: index[im['file']].append(((bi,pi),im.get('mode','')))
            prompts = {}
            for fn,row in rows.items():
                recipe,group,stage = (row[k] for k in ('recipe','group','stage'))
                if not (allowed(fn) and re.fullmatch(r'[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|webp)',fn) and recipe in ids and isinstance(group,str) and 0<len(group)<=200 and stage in ('MOSS','Krea2')): continue
                if len(index[fn])!=1: continue
                pos,mode=index[fn][0]; path=out/fn
                if not mode.startswith(stage) or path.resolve().parent!=out or not path.is_file(): continue
                if (group,stage) in slots or owners.setdefault(group,(recipe,pos))!=(recipe,pos) or prompts.setdefault(pos,group)!=group: raise ValueError('组或阶段映射冲突')
                slots.add((group,stage)); valid[fn]={'recipe':recipe,'group':group,'stage':stage}
        votes = load(state) if state.exists() else {}
        if not isinstance(votes,dict) or any(not isinstance(r,dict) or type(r.get('stars')) is not int or not 1<=r['stars']<=5 for r in votes.values()): raise ValueError('评分库格式无效')
        if body is not None:
            if not isinstance(body,dict) or set(body)!={'file','stars'} or not isinstance(body['file'],str) or type(body['stars']) is not int or not 0<=body['stars']<=5: raise Invalid('仅接受 file 和整数 stars (0–5)')
            fn,s=body['file'],body['stars']
            if fn not in valid: raise Invalid('该图尚无可验证配方映射')
            if s: votes[fn]={**valid[fn],'stars':s}
            else: votes.pop(fn,None)
            save(state,votes)
        ratings={f:(votes[f]['stars'] if f in votes and all(votes[f].get(k)==v for k,v in row.items()) else 0) for f,row in valid.items()}
        groups=defaultdict(list); byrecipe=defaultdict(list)
        for f,s in ratings.items():
            if s: groups[(valid[f]['recipe'],valid[f]['group'])].append(s)
        for (rid,g),ss in groups.items(): byrecipe[rid].append(1 if 1 in ss else sum(ss)/len(ss))
        stats={}
        for rid,means in byrecipe.items():
            x=sum(means)/len(means); w=0 if 1 in means else (0.5+(x-2)*0.5 if x<3 else x-2)
            stats[rid]={'groups':len(means),'mean':x,'weight':w}
        return {'ok':True,'ratings':ratings,'recipes':stats,'rule':RULE}
