import hashlib
import html
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(r"C:\Users\hello Teng\3D Objects\ComfyUI-aki-v3-IntelArc_20261031\ComfyUI-aki-v3-IntelArc\ComfyUI\output").resolve()
TOOL_DIR = Path(__file__).resolve().parent
CACHE_DIR = TOOL_DIR / ".thumb_cache"
PORT = 8193
HOST = "127.0.0.1"
MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm"}
VIDEO_EXTENSIONS = {".mp4", ".webm"}

PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ComfyUI 最近输出</title>
<style>
:root,html[data-theme="jin"]{color-scheme:dark;--bg:#16120d;--panel:#211a13;--panel2:#2a2117;--line:rgba(217,168,78,.16);--line2:rgba(217,168,78,.34);--text:#ede4d3;--dim:#a2988a;--accent:#d9a84e;--accent2:#ecc27a;--input:#1b160f;--ok:#7cc4a1;--thumb:#0b0906;--header:rgba(29,23,16,.96)}
html[data-theme="xuan"]{color-scheme:light;--bg:#f6f1e5;--panel:#fffdf7;--panel2:#fdf8ec;--line:rgba(154,116,42,.24);--line2:rgba(154,116,42,.5);--text:#2b2620;--dim:#7a6f60;--accent:#b8892f;--accent2:#9a742a;--input:#fffdf7;--ok:#3e8a67;--thumb:#eae3d4;--header:rgba(255,253,247,.96)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 "Microsoft YaHei",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--header);border-bottom:1px solid var(--line);padding:16px 20px;backdrop-filter:blur(10px)}
h1{margin:0;font-size:20px;color:var(--accent2)}.meta{color:var(--dim);font-size:12px;margin-top:4px}.controls{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-top:12px}
input,select,button{font:inherit;color:var(--text);background:var(--input);border:1px solid var(--line);border-radius:9px;padding:8px 10px}input[type=search]{min-width:260px;flex:1}button{cursor:pointer}button:hover{border-color:var(--accent);color:var(--accent)}.check{display:flex;gap:6px;align-items:center;color:var(--dim)}
main{max-width:1800px;margin:auto;padding:18px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;cursor:pointer;transition:border-color .15s,transform .15s,box-shadow .15s}.card:hover{border-color:var(--line2);transform:translateY(-2px);box-shadow:0 10px 26px rgba(0,0,0,.16)}.thumb{aspect-ratio:1;background:var(--thumb);display:flex;align-items:center;justify-content:center;overflow:hidden}.thumb img{width:100%;height:100%;object-fit:contain}.video{color:var(--dim);font-size:42px}.info{padding:9px 10px}.name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}.sub{color:var(--dim);font-size:11px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.empty{padding:60px;text-align:center;color:var(--dim)}
dialog{width:min(1200px,94vw);height:fit-content;max-width:94vw;max-height:90dvh;margin:auto;padding:0;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}dialog[open]{display:flex;flex-direction:column}dialog::backdrop{background:rgba(0,0,0,.82)}.stage{min-width:0;width:100%;min-height:120px;max-height:72dvh;height:auto;display:flex;align-items:center;justify-content:center;background:var(--thumb);overflow:hidden}.stage img,.stage video{width:auto;height:auto;max-width:100%;max-height:72dvh;object-fit:contain}.details{padding:8px 12px;border-top:1px solid var(--line);background:var(--panel)}.path{color:var(--dim);font:11px/1.45 ui-monospace,Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.details .sub{display:none}.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px}.badge{display:inline-block;color:var(--ok);font-size:12px;margin-left:8px}.preview-position{margin-left:auto;color:var(--dim);font-size:12px}
dialog.fullscreen,dialog:fullscreen{width:100vw;height:100vh;width:100dvw;height:100dvh;max-width:none;max-height:none;margin:0;border:0;border-radius:0}dialog.fullscreen[open],dialog:fullscreen[open]{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr) auto}dialog.fullscreen .stage,dialog:fullscreen .stage{height:100%;min-height:0;max-height:none}dialog.fullscreen .stage img,dialog.fullscreen .stage video,dialog:fullscreen .stage img,dialog:fullscreen .stage video{width:100%;height:100%;max-width:100%;max-height:100%;object-fit:contain}
@media(max-width:600px){
  body{font-size:13px}
  header{padding:10px 10px calc(10px + env(safe-area-inset-bottom))}
  .meta{font-size:11px}
  html.embedded .meta{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .controls{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  input[type=search]{grid-column:1/-1;min-width:0;width:100%}
  input,select,button{min-height:38px;padding:8px 10px}
  #refresh{grid-column:1}
  .check{grid-column:2;min-height:30px;justify-content:flex-end;font-size:11px}
  main{padding:10px 10px calc(20px + env(safe-area-inset-bottom))}
  .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
  .card{border-radius:10px}
  .info{padding:7px}.name{font-size:12px}.sub{font-size:10px}
  dialog{width:96vw;max-width:96vw;max-height:88dvh}
  .stage,.stage img,.stage video{max-height:66dvh}
  .details{padding:8px 10px calc(8px + env(safe-area-inset-bottom))}
  .actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}
  .actions a{display:block}.actions button,.actions a button{width:100%;padding:8px 9px}
  .preview-position{grid-column:1/-1;order:-1;margin:0;text-align:center}
  #openFile,#openFolder{display:none}
}
html.embedded h1{display:none}html.embedded header{padding:10px 12px}html.embedded .controls{margin-top:8px}html.embedded main{padding:12px}button:disabled{opacity:.5;cursor:default}
</style>
</head>
<body>
<header>
<h1>ComfyUI 最近输出 <span id="status" class="badge"></span></h1>
<div id="meta" class="meta">正在扫描…</div>
<div class="controls">
<input id="q" type="search" placeholder="搜索文件名或子目录">
<select id="type"><option value="all">图片 + 视频</option><option value="image">只看图片</option><option value="video">只看视频</option></select>
<select id="limit"><option value="60">最近 60</option><option value="120" selected>最近 120</option><option value="240">最近 240</option></select>
<button id="refresh">刷新</button>
<label class="check"><input id="auto" type="checkbox" checked> 每 15 秒自动刷新</label>
</div>
</header>
<main><div id="grid" class="grid"></div><div id="empty" class="empty" hidden>没有匹配的媒体文件</div></main>
<dialog id="viewer"><div id="stage" class="stage"></div><div class="details"><div id="name" class="name"></div><div id="path" class="path"></div><div id="sub" class="meta"></div><div class="actions"><button type="button" id="recentPrevious">← 上一张</button><button type="button" id="recentNext">下一张 →</button><output id="recentPosition" class="preview-position" aria-live="polite"></output><button id="recentFullscreen" type="button">全屏</button><button id="recentClose" type="button">关闭</button><button id="openFile">打开文件</button><button id="openFolder">打开所在文件夹</button><button id="copyPath">复制路径</button><a id="download" download><button type="button">下载</button></a></div></div></dialog>
<script>
const $=id=>document.getElementById(id);let items=[];let current=null;let loading=false,reloadPending=false;
const EMBEDDED=new URLSearchParams(location.search).get('embedded')==='1';
let parentOrigin='*';
function parentTargetOrigin(){return parentOrigin!=='*'?parentOrigin:(document.referrer?new URL(document.referrer).origin:location.origin)}
window.addEventListener('message',event=>{
  if(event.source!==parent)return;
  if(event.data?.type==='zmf-theme'&&['jin','xuan'].includes(event.data.theme)){
    parentOrigin=event.origin;document.documentElement.dataset.theme=event.data.theme;
    if(EMBEDDED)parent.postMessage({type:'zmf-theme-applied',theme:event.data.theme},parentOrigin);
  }
  if(event.data?.type==='zmf-recent-fullscreen-state'){
    $('viewer').classList.toggle('fullscreen',!!event.data.active);
    $('recentFullscreen').textContent=event.data.active?'退出全屏':'全屏';
  }
});
const fmtSize=n=>n>=1073741824?(n/1073741824).toFixed(2)+' GB':n>=1048576?(n/1048576).toFixed(1)+' MB':(n/1024).toFixed(0)+' KB';
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){if(loading){reloadPending=true;return}loading=true;$('status').textContent='扫描中';try{const p=new URLSearchParams({limit:$('limit').value,q:$('q').value,type:$('type').value});const r=await fetch('./api/recent?'+p);const data=await r.json();items=data.items;$('meta').textContent=`输出目录共 ${data.total_media} 个媒体文件 · 当前显示 ${items.length} 个 · 扫描 ${data.scan_ms} ms · 更新时间 ${new Date().toLocaleTimeString()}`;$('status').textContent='就绪';render()}catch(e){$('status').textContent='扫描失败';$('meta').textContent=String(e)}finally{loading=false;if(reloadPending){reloadPending=false;load()}}}
function render(){const grid=$('grid');grid.replaceChildren();$('empty').hidden=items.length>0;for(const item of items){const card=document.createElement('article');card.className='card';const thumb=document.createElement('div');thumb.className='thumb';if(item.kind==='video'){const v=document.createElement('div');v.className='video';v.textContent='▶';thumb.appendChild(v)}else{const img=document.createElement('img');img.loading='lazy';img.decoding='async';img.src='./thumb?path='+encodeURIComponent(item.path);img.alt=item.name;img.onerror=()=>{img.onerror=null;img.src='./file?path='+encodeURIComponent(item.path)};thumb.appendChild(img)}const info=document.createElement('div');info.className='info';const name=document.createElement('div');name.className='name';name.textContent=item.name;name.title=item.path;const sub=document.createElement('div');sub.className='sub';sub.textContent=new Date(item.mtime*1000).toLocaleString()+' · '+fmtSize(item.size);info.append(name,sub);card.append(thumb,info);card.onclick=()=>show(item);grid.appendChild(card)}}
async function show(item){current=item;const stage=$('stage');stage.replaceChildren();if(item.kind==='video'){const v=document.createElement('video');v.controls=true;v.autoplay=true;v.src='./file?path='+encodeURIComponent(item.path);stage.appendChild(v)}else{const img=document.createElement('img');img.src='./file?path='+encodeURIComponent(item.path);img.alt=item.name;stage.appendChild(img)}$('name').textContent=item.name;$('path').textContent=item.full_path;$('sub').textContent=new Date(item.mtime*1000).toLocaleString()+' · '+fmtSize(item.size)+' · '+item.path;$('download').href='./file?path='+encodeURIComponent(item.path);setPreviewPosition(item);if(!viewer.open)viewer.showModal()}
async function openItem(mode){if(!current)return;const r=await fetch('./open',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:current.path,mode})});const data=await r.json();if(!data.ok)alert(data.error||'打开失败')}
async function copyPath(){if(!current)return;const text=current.full_path;const flash=()=>{const b=$('copyPath'),old=b.textContent;b.textContent='已复制';setTimeout(()=>b.textContent=old,900)};try{await navigator.clipboard.writeText(text);flash()}catch(e){const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();flash()}}
// Preview order is a snapshot of the currently filtered results.
let previewItems = [], previewIndex = -1;
const viewer = $('viewer');
const previous = $('recentPrevious'), next = $('recentNext');
const position = $('recentPosition');
const fullscreenButton = $('recentFullscreen');
function setFullscreenState(active){viewer.classList.toggle('fullscreen',active);fullscreenButton.textContent=active?'退出全屏':'全屏'}
async function lockLandscape(){try{if(matchMedia('(pointer:coarse)').matches&&screen.orientation?.lock)await screen.orientation.lock('landscape')}catch(e){}}
function unlockOrientation(){try{screen.orientation?.unlock?.()}catch(e){}}
async function toggleFullscreen(){
  if(EMBEDDED){parent.postMessage({type:'zmf-recent-fullscreen',action:'toggle'},parentTargetOrigin());return}
  try{
    if(document.fullscreenElement){await document.exitFullscreen();unlockOrientation()}
    else{await document.documentElement.requestFullscreen();setFullscreenState(true)}
  }catch(e){}
}
fullscreenButton.onclick=toggleFullscreen;
document.addEventListener('fullscreenchange',()=>{const active=!!document.fullscreenElement;setFullscreenState(active);if(active)lockLandscape();else unlockOrientation()});
function changePreview(step) {
  if (previewItems.length < 2) return;
  previewIndex = (previewIndex + step + previewItems.length) % previewItems.length;
  show(previewItems[previewIndex]);
}
previous.onclick = () => changePreview(-1);
next.onclick = () => changePreview(1);
viewer.addEventListener('keydown', event => {
  if (event.altKey || event.ctrlKey || event.metaKey || event.target.closest('video')) return;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault(); changePreview(event.key === 'ArrowLeft' ? -1 : 1);
  }
});
function resetPreview(){
  if(EMBEDDED)parent.postMessage({type:'zmf-recent-fullscreen',action:'exit'},parentTargetOrigin());
  else if(document.fullscreenElement)document.exitFullscreen().catch(()=>{});
  setFullscreenState(false);
  $('stage').replaceChildren(); previewItems = []; previewIndex = -1; current = null;
}
function closePreview(){if(viewer.open)viewer.close();resetPreview()}
viewer.addEventListener('close', resetPreview);
function setPreviewPosition(item) {
  if (!viewer.open) {
    previewItems = items.slice();
    previewIndex = previewItems.findIndex(candidate => candidate.path === item.path);
    if (previewIndex < 0) { previewItems = [item]; previewIndex = 0; }
  }
  position.textContent = `${previewIndex + 1} / ${previewItems.length}`;
  previous.disabled = next.disabled = previewItems.length < 2;
}

$('recentClose').onclick = closePreview;
if (EMBEDDED) {
  document.documentElement.classList.add('embedded');
  $('auto').checked = false;
  parent.postMessage({type:'zmf-recent-ready'},'*');
}

$('refresh').onclick=load;let searchTimer;$('q').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(load,230)};$('type').onchange=load;$('limit').onchange=load;$('openFile').onclick=()=>openItem('file');$('openFolder').onclick=()=>openItem('folder');$('copyPath').onclick=copyPath;$('viewer').addEventListener('click',e=>{if(e.target===$('viewer'))closePreview()});setInterval(()=>{if($('auto').checked)load()},15000);load();
</script>
</body>
</html>'''


def scan_media():
    started = time.perf_counter()
    found = []
    stack = [ROOT]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            suffix = Path(entry.name).suffix.lower()
                            if suffix not in MEDIA_EXTENSIONS:
                                continue
                            stat = entry.stat(follow_symlinks=False)
                            path = Path(entry.path)
                            found.append({
                                "name": entry.name,
                                "path": path.relative_to(ROOT).as_posix(),
                                "full_path": str(path),
                                "mtime": stat.st_mtime,
                                "size": stat.st_size,
                                "kind": "video" if suffix in VIDEO_EXTENSIONS else "image",
                            })
                    except OSError:
                        continue
        except OSError:
            continue
    found.sort(key=lambda item: (item["mtime"], item["name"]), reverse=True)
    return found, round((time.perf_counter() - started) * 1000, 1)


def safe_path(value):
    candidate = (ROOT / value).resolve()
    if not candidate.is_relative_to(ROOT) or not candidate.is_file():
        raise ValueError("文件不存在或路径越界")
    return candidate


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/":
            self.send_bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/recent":
            media, scan_ms = scan_media()
            search = query.get("q", [""])[0].casefold()
            kind = query.get("type", ["all"])[0]
            try:
                limit = max(1, min(500, int(query.get("limit", ["120"])[0])))
            except ValueError:
                limit = 120
            filtered = [item for item in media if (kind == "all" or item["kind"] == kind) and (not search or search in item["path"].casefold())]
            self.send_json({"total_media": len(media), "scan_ms": scan_ms, "items": filtered[:limit]})
            return
        if parsed.path in ("/file", "/thumb"):
            try:
                path = safe_path(query.get("path", [""])[0])
            except ValueError as error:
                self.send_json({"error": str(error)}, 404)
                return
            if parsed.path == "/thumb":
                self.serve_thumbnail(path)
            else:
                self.serve_file(path)
            return
        self.send_json({"error": "Not found"}, 404)

    def serve_thumbnail(self, path):
        try:
            from PIL import Image, ImageOps
        except ImportError:
            self.send_json({"error": "Pillow unavailable"}, 404)
            return
        try:
            stat = path.stat()
            key = hashlib.sha256(f"{path}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8")).hexdigest()
            CACHE_DIR.mkdir(exist_ok=True)
            cached = CACHE_DIR / f"{key}.jpg"
            if not cached.exists():
                with Image.open(path) as source:
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    image.thumbnail((420, 420), Image.Resampling.LANCZOS)
                    temporary = cached.with_suffix(".tmp")
                    image.save(temporary, "JPEG", quality=84, optimize=True)
                    os.replace(temporary, cached)
            self.send_bytes(cached.read_bytes(), "image/jpeg")
        except Exception:
            self.send_json({"error": "thumbnail failed"}, 404)

    def serve_file(self, path):
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match:
                if match.group(1):
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else size - 1
                elif match.group(2):
                    start = max(0, size - int(match.group(2)))
                end = min(end, size - 1)
                status = 206
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def trusted_origin(self):
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
            host = urlsplit("//" + self.headers.get("Host", "")).netloc
            if parsed.scheme in ("http", "https") and parsed.netloc == host:
                return True
            return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1") and parsed.port in (None, PORT)
        except ValueError:
            return False

    def do_POST(self):
        if urlsplit(self.path).path != "/open":
            self.send_json({"error": "Not found"}, 404)
            return
        if not self.trusted_origin():
            self.send_json({"error": "rejected origin"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            path = safe_path(payload.get("path", ""))
            mode = payload.get("mode")
            if mode == "file":
                os.startfile(path)
            elif mode == "folder":
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                raise ValueError("未知打开方式")
            self.send_json({"ok": True})
        except Exception as error:
            self.send_json({"ok": False, "error": str(error)}, 400)


def port_is_open():
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((HOST, PORT)) == 0


def main():
    if port_is_open():
        webbrowser.open(f"http://{HOST}:{PORT}/")
        return
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.25, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
    print(f"ComfyUI recent viewer: http://{HOST}:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
