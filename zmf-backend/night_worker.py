"""创作中心夜跑编排。单批有限任务，退出网页不影响后台。"""
import hashlib
import secrets
import threading
import time
from pathlib import Path
from urllib.error import HTTPError


def outcome(reason):
    return 'wait' if reason in ('busy','offline') else ('pause' if reason == 'ambiguous' else 'continue')


class Worker:
    def __init__(self, core, directory, catalog, manifest, ratings, binding_lock, delay=12):
        self.c, self.directory, self.catalog = core, Path(directory), catalog
        self.path = self.directory / 'session.json'
        self.manifest, self.ratings, self.binding_lock = manifest, ratings, binding_lock
        self.delay = delay
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()

    def read(self):
        return self.c.read(self.path, {})

    def save(self, d):
        with self.lock:
            self.c.save(self.path, d)

    def status(self):
        with self.lock:
            d = self.read()
            active = bool(self.thread and self.thread.is_alive())
            return {'ok': True, 'active': active, 'deadline': d.get('deadline'),
                    'message': d.get('message','尚无夜跑批次'),
                    'rows': [{k:v for k,v in x.items() if k in ('id','recipe','state','note','pid','files')} for x in d.get('rows',[])]}

    def verify_gallery(self, row, captions):
        source = row.get('source')
        caption = row.get('caption')
        if (not isinstance(source, str) or row.get('track') != 'prompt_en'
                or row.get('id') != 'TJ-' + source.replace('/', '-')
                or not isinstance(caption, str) or not caption.strip() or captions.get(source) != caption):
            raise ValueError('未绑定配方的条目必须是当前未排除的图鉴英文轨原文')

    def prepare(self, body):
        items = body.get('items')
        hours, cap = body.get('hours',6), body.get('max_groups',60)
        if not isinstance(items,list) or not 1 <= len(items) <= 300 or type(hours) not in (int,float) or not 0 < hours <= 12 or type(cap) is not int or not 1 <= cap <= 300:
            raise ValueError('清单须有 1–300 项；时长 0–12 小时，组数 1–300。')
        hub = self.catalog()
        recipes = {q['id'] for q in hub['RECIPES'] if q.get('auto')}
        used = self.c.Used()
        d = self.read()
        for x in d.get('used',[]) + d.get('rows',[]):
            if x['state'] != 'waiting': used.add(x['id'],x['caption'])
        for b in self.manifest().get('batches',[]):
            for x in b.get('prompts',[]): used.add(None,x.get('prompt') or '')
        for path in sorted(self.c.ROOT.glob('night_*/*ledger.json')):
            for x in self.c.read(path): used.add(x.get('id'),x.get('caption') or '')
        for x in self.c.read(self.c.ROOT/'simple_create_records.json',[]):
            used.add(x.get('id'),x.get('prompt') or x.get('caption') or x.get('skeleton') or '')
        rows, skipped = [], []
        captions = None
        for x in items:
            source = None
            if isinstance(x, dict) and x.get('recipe') is None:
                if captions is None: captions = self.gallery_captions()
                self.verify_gallery(x, captions)
                source = x['source']
                ident, _, _ = self.c.check({**x, 'recipe':'_gallery'}, {'_gallery'})
                caption, recipe = x['caption'], None
            else:
                ident, caption, recipe = self.c.check(x, recipes)
            if used.has(ident,caption): skipped.append(ident); continue
            used.add(ident,caption)
            rows.append(dict(id=ident,caption=caption,recipe=recipe,**({'source':source,'track':'prompt_en'} if source else {}),state='waiting',tag='NIGHT_'+secrets.token_hex(12),seed=secrets.randbelow(2**31-1)+1))
        return rows[:cap], skipped, hours

    def start(self, body, preview=False):
        with self.lock:
            rows, skipped, hours = self.prepare(body)
            if preview: return {'ok':True,'accepted':len(rows),'skipped':skipped}
            if self.thread and self.thread.is_alive(): raise ValueError('已有夜跑批次正在运行')
            prior = self.read()
            if any(x['state'] in ('submitting','accepted','ambiguous') for x in prior.get('rows',[])):
                raise ValueError('上一批有未对账任务，请用继续对账，不可另开批次')
            if not rows: raise ValueError('没有未跑过且可评分的题材')
            from PIL import Image  # 先验证依赖，不入队后才发现缺包
            self.directory.mkdir(parents=True,exist_ok=True)
            self.save(dict(rows=rows, used=prior.get('used',[])+[x for x in prior.get('rows',[]) if x['state']!='waiting'],deadline=time.time()+hours*3600,message='已启动'))
            self.launch()
            return {'ok':True,'accepted':len(rows),'skipped':skipped}

    def launch(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.loop,daemon=True,name='ZMF-night')
        self.thread.start()

    def resume(self):
        with self.lock:
            if self.thread and self.thread.is_alive(): raise ValueError('已有夜跑在运行')
            if not self.read().get('rows'): raise ValueError('没有夜跑台账')
            self.launch()
            return {'ok':True}

    def stop(self):
        self.stop_event.set()
        return {'ok':True}

    def find(self, row):
        c = self.c
        q = c.api(c.COMFY,'/queue')
        history = c.api(c.COMFY,'/history?max_items=200')
        expected = 'ZMF_DUAL_'+row['tag']+'_M1'
        exact = []
        for item in list(q.get('queue_running',[]))+list(q.get('queue_pending',[]))+[x.get('prompt') for x in history.values()]:
            if not isinstance(item,list) or len(item)<3 or not isinstance(item[2],dict): continue
            g=item[2]
            if g.get('437',{}).get('inputs',{}).get('filename_prefix') == expected and g.get('76',{}).get('inputs',{}).get('value') == row['caption'] and g.get('16',{}).get('inputs',{}).get('seed') == row['seed']:
                exact.append(item[1])
        found=set(exact)
        return next(iter(found)) if len(found)==1 else None

    def collect(self, row, d):
        c = self.c
        man = self.manifest(); out = Path(man['outputDir']); files = row['files']
        if not c.registered(man,files) or not all((out/f).is_file() for f in files): return False
        if 'signatures' not in row:
            signatures=[c.image_signature(out/f) for f in files]
            previous=[sig for x in d.get('used',[])+d['rows'] if x is not row and x['state']=='done' for sig in x.get('signatures',[])]
            if signatures[0]['pixel']==signatures[1]['pixel'] or any(c.near(sig,old) for sig in signatures for old in previous):
                row.update(state='near',note='疑似重复，保留文件但不绑定评分；跳过本组')
                return True
            row['signatures']=signatures
        if row['recipe'] is None:
            row.update(state='done',note='同组登记；图鉴直跑，不计入配方权重')
            return True
        path = c.ROOT/'gallery/人物挑片映射.json' 
        with self.binding_lock:
            c.binding(path,files,row['recipe'],row['pid'])
        if not all(f in self.ratings().get('ratings',{}) for f in files): return False
        row.update(state='done',note='同组登记，已获评分资格')
        return True

    @staticmethod
    def _comfy_launcher():
        return (Path.home() / '3D Objects/ComfyUI-aki-v3-IntelArc_20261031'
                / 'ComfyUI-aki-v3-IntelArc/Stable_Start_MJ40_cute.bat')

    def _comfy_exists(self):
        import socket
        import subprocess
        try:
            with socket.create_connection(('127.0.0.1', 8188), timeout=1):
                return True
        except ConnectionRefusedError:
            pass
        except OSError:
            return None
        # 查进程而非只查端口。若无法读取本机进程列表，宁可不启动第二实例。
        command = ("$ErrorActionPreference='Stop'; "
                   "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
                   "Select-Object ExecutablePath,CommandLine) | ConvertTo-Json -Compress")
        try:
            import json
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                    capture_output=True, text=True, errors='replace', timeout=8,
                                    creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            data = json.loads(result.stdout)
            base = str(self._comfy_launcher().parent / 'python').casefold() + chr(92)
            for process in data if isinstance(data,list) else [data]:
                exe = process.get('ExecutablePath') or ''
                args = process.get('CommandLine') or ''
                if not exe and not args:
                    return None
                if exe.casefold().startswith(base) and (not args or 'main.py' in args.casefold()):
                    return True
            return False
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            return None

    def _launch_comfy(self):
        import os
        import subprocess
        path = self._comfy_launcher()
        if not path.is_file():
            raise FileNotFoundError('当前 ComfyUI 启动脚本不存在')
        executable = str(Path(os.environ['SystemRoot']) / 'System32/cmd.exe')
        with (self.directory / 'comfy_recovery.log').open('ab') as log:
            subprocess.Popen([executable, '/d', '/c', path.name], cwd=path.parent,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)

    def recover_comfy(self, d, now=None):
        if self.stop_event.is_set() or time.time() >= d['deadline']:
            return
        now = time.time() if now is None else now
        recovery = d.setdefault('recovery', {})
        if now - recovery.setdefault('offline_since', now) < 20:
            d['message'] = 'ComfyUI 暂不可达，等待确认'
        elif recovery.get('attempts', 0) >= 3:
            d['message'] = 'ComfyUI 自动恢复已达三次上限；等待手动恢复或到点结束'
        elif now - recovery.get('last_attempt', -float('inf')) < 120:
            d['message'] = '等待 ComfyUI 启动完成'
        elif self._comfy_exists() is not False:
            d['message'] = 'ComfyUI 仍在或无法确认已退出，不启动第二实例'
        elif not self.stop_event.is_set() and time.time() < d['deadline']:
            recovery['attempts'] = recovery.get('attempts', 0) + 1
            recovery['last_attempt'] = now
            d['message'] = '正在自动恢复 ComfyUI：第 %d/3 次' % recovery['attempts']
            self.save(d)  # 启动动作前落盘，重启 8191 也不重置次数
            try:
                self._launch_comfy()
            except OSError as exc:
                d['message'] = 'ComfyUI 启动失败：' + str(exc)[:150]
        self.save(d)

    def tick(self, d):
        c = self.c
        # 收图、评分暂未就绪不占渲染队列，下一条可以继续。
        for row in d['rows']:
            if row['state']=='register':
                try:
                    if not self.collect(row,d): row['note']='等待图库登记或评分确认'
                except (OSError,ValueError,KeyError) as e: row['note']='登记待重试：'+str(e)[:160]
                self.save(d)
        pending = next((x for x in d['rows'] if x['state'] in ('submitting','ambiguous','accepted')),None)
        has_work = pending is not None or any(x['state']=='waiting' for x in d['rows'])
        if has_work and not self.stop_event.is_set() and time.time() < d['deadline']:
            queue = c.api(c.GALLERY, '/api/queue')
            if not queue.get('reachable'):
                self.recover_comfy(d)
                return True
            recovery = d.get('recovery', {})
            if recovery.pop('offline_since', None) is not None:
                d['message'] = 'ComfyUI 已恢复，先对账再继续'
                self.save(d)
        if pending:
            if pending['state'] != 'accepted':
                pid=self.find(pending)
                if not pid:
                    if time.time() - pending.get('submitted_at', time.time()) < 180 and time.time() < d['deadline']:
                        d['message']='提交回执不明，继续查队列和历史'; self.save(d); return True
                    pending.update(state='review',note='未确认是否受理，绝不重提；队列空闲后才继续其他题材')
                    self.save(d); return True
                pending.update(state='accepted',pid=pid); self.save(d)
            entry=c.api(c.COMFY,'/history/'+pending['pid']).get(pending['pid'],{})
            status=entry.get('status',{})
            if status.get('status_str') in ('error','interrupted'):
                pending.update(state='failed',note='渲染失败，不重跑'); self.save(d); return True
            if status.get('status_str')=='success':
                try: pending.update(state='register',files=c.completed(entry,pending['tag']),note='等待同组收录')
                except ValueError as e: pending.update(state='failed',note=str(e))
                self.save(d); return True
            if not entry and time.time() - pending.get('submitted_at', time.time()) >= 180:
                queue=c.api(c.GALLERY,'/api/queue')
                if queue.get('reachable') and not (queue.get('running') or queue.get('pending')):
                    pending.update(state='review',note='历史缺失且队列空闲，保留任务 ID，不重提')
                    self.save(d); return True
            if time.time() >= d['deadline']+1800:
                d['message']='任务仍未取得终态，保留回执'; self.save(d); return False
            return True
        waiting=next((x for x in d['rows'] if x['state']=='waiting'),None)
        if self.stop_event.is_set() or time.time()>=d['deadline'] or waiting is None:
            d['message']='已停止新提交' if self.stop_event.is_set() else ('已到截止时间' if time.time()>=d['deadline'] else '清单已处理完')
            self.save(d)
            return not self.stop_event.is_set() and any(x['state']=='register' for x in d['rows']) and time.time()<min(d['deadline']+120,d.get('settle_until',float('inf')))
        queue=c.api(c.GALLERY,'/api/queue')
        if not queue.get('reachable') or queue.get('running') or queue.get('pending'):
            d['message']='等待 ComfyUI 恢复或让队'; self.save(d); return True
        h=self.catalog()
        if waiting['recipe'] is None:
            try:
                self.verify_gallery(waiting, self.gallery_captions())
            except ValueError as exc:
                waiting.update(state='skipped',note=str(exc)); self.save(d); return True
        elif not any(q['id']==waiting['recipe'] and q.get('auto') for q in h['RECIPES']):
            waiting.update(state='skipped',note='配方已暂停或排除'); self.save(d); return True
        if self.stop_event.is_set() or time.time()>=d['deadline']: return True
        waiting.update(state='submitting',submitted_at=time.time()); d['message']='提交 '+waiting['id']; self.save(d)
        try:
            payload = dict(prompt=waiting['caption'], tag=waiting['tag'], seed=waiting['seed'])
            if waiting['recipe'] is not None:
                payload.update(recipe=waiting['recipe'],pool_revision=h['POOL_REVISION'])
            res=c.api(c.GALLERY,'/api/generate_dual',payload)
        except HTTPError as e:
            if e.code==409:
                waiting['state']='waiting'; d['message']='让队或配方更新，等待复核'; self.save(d); return True
            if e.code==400:
                waiting.update(state='failed',note='请求被拒绝 HTTP 400'); self.save(d); return True
            raise
        pid=res.get('prompt_id')
        if res.get('ok') and isinstance(pid,str) and c.LABEL.fullmatch(pid): waiting.update(state='accepted',pid=pid)
        self.save(d)
        return True

    def loop(self):
        import os
        d=self.read()
        power = None
        try:
            if os.name == 'nt':
                import ctypes
                power = ctypes.windll.kernel32.SetThreadExecutionState
                power.argtypes = [ctypes.c_uint]
                power.restype = ctypes.c_uint
                if not power(0x80000001):
                    d['message']='无法阻止系统自动睡眠，未提交新任务'; self.save(d); return
            while True:
                if not any(x['state']=='waiting' for x in d['rows']) and not any(x['state'] in ('submitting','ambiguous','accepted') for x in d['rows']):
                    d.setdefault('settle_until',time.time()+120)
                try:
                    if not self.tick(d): break
                except (OSError,ValueError,KeyError) as e:
                    d['message']='等待重试：'+str(e)[:160]; self.save(d)
                    if time.time()>=d['deadline']+120 or self.stop_event.is_set(): break
                time.sleep(self.delay)
        except Exception as e:
            d['message']='后台异常，台账保留：'+type(e).__name__; self.save(d)
        finally:
            if power is not None:
                power(0x80000000)
