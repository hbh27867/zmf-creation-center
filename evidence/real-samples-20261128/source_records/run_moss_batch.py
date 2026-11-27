import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8191"
COMFY = "http://127.0.0.1:8188"
GRAPH = Path(r"C:/tmp_zmf/gallery/api_graph_moss.json")
EXPECTED_GRAPH_SHA256 = "3653b44c22f6368996febcbf3a66752e7f799a1a5f275150aa4a9f13348da74f"
DATA = Path(r"C:/Users/hello Teng/Documents/UP主风格研究/data.js")
EXCLUSION = Path(r"C:/Users/hello Teng/Documents/ZMF配置重建/优化版_部署候选/验收沙箱/不借鉴清单_20261122_193121.json")
OUT = Path(r"C:/Users/hello Teng/3D Objects/ComfyUI-aki-v3-IntelArc_20261031/ComfyUI-aki-v3-IntelArc/ComfyUI/output")
CATALOG = ROOT / "catalog.json"
AUDIT = ROOT / "exclusion_audit.json"
LEDGER = ROOT / "ledger.json"
SESSION = ROOT / "session.json"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def api(path, data=None, timeout=30, base=BASE):
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
        return exc.code, payload


def validate():
    catalog = load_json(CATALOG, [])
    audit = load_json(AUDIT, {})
    if len(catalog) != 199:
        raise RuntimeError("图鉴清单数量不是 199")
    if len({row["key"] for row in catalog}) != 199 or len({row["seed"] for row in catalog}) != 199:
        raise RuntimeError("来源键或 seed 不唯一")
    for row in catalog:
        if not isinstance(row["seed"], int) or not 1 <= row["seed"] < 2 ** 31:
            raise RuntimeError("seed 非法: %s" % row["key"])
        if sha256_text(row["prompt"]) != row["sha256"]:
            raise RuntimeError("Caption 指纹不匹配: %s" % row["key"])
        if len(row["prompt"]) != row["char_count"]:
            raise RuntimeError("Caption 字数不匹配: %s" % row["key"])
    if sha256_bytes(GRAPH) != EXPECTED_GRAPH_SHA256:
        raise RuntimeError("MOSS 工作流指纹已变化，停止")
    if sha256_bytes(DATA) != audit.get("data_sha256"):
        raise RuntimeError("图鉴源文件已变化，停止")
    if sha256_bytes(EXCLUSION) != audit.get("exclusion_sha256"):
        raise RuntimeError("不借鉴清单已变化，停止")
    exclusion = load_json(EXCLUSION, {})
    denied = {"{}/s{:02d}".format(row["bvid"], row["scene"]) for row in exclusion["items"]}
    denied_matches = sorted(denied.intersection(row["key"] for row in catalog))
    if denied_matches:
        raise RuntimeError("发现不借鉴来源，禁止提交: " + ", ".join(denied_matches))
    if audit.get("excluded_matches") != [] or audit.get("missing_prompts") != []:
        raise RuntimeError("排除审计不是零交集")
    print("validated", len(catalog), "items; excluded_matches=0; unique_seeds=199", flush=True)
    return catalog


def record_for(ledger, key):
    return next((row for row in ledger if row.get("key") == key), None)


def valid_png(path):
    path = Path(path)
    try:
        with path.open("rb") as handle:
            return path.stat().st_size > 0 and handle.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def files_from_task(task):
    names = task.get("files") or []
    files = []
    for name in names:
        if not isinstance(name, str) or not name.startswith("ZMF_MOSS_QUICK_") or not name.endswith(".png"):
            continue
        path = OUT / Path(name).name
        if valid_png(path):
            files.append(path)
    return files


def reconcile_new_output(not_before):
    candidates = []
    for path in OUT.glob("ZMF_MOSS_QUICK_*.png"):
        try:
            if path.stat().st_mtime >= not_before - 3 and valid_png(path):
                candidates.append(path)
        except OSError:
            pass
    if len(candidates) != 1:
        return None, candidates
    target = candidates[0]
    try:
        status, history = api("/history?max_items=50", base=COMFY, timeout=15)
        if status == 200:
            for prompt_id, entry in history.items():
                names = [im.get("filename") for node in entry.get("outputs", {}).values()
                         for im in node.get("images", []) if im.get("type") == "output"]
                if target.name in names:
                    return prompt_id, candidates
    except Exception:
        pass
    return None, candidates


def run():
    catalog = validate()
    ledger = load_json(LEDGER, [])
    session = load_json(SESSION, {"created": now_iso(), "submitted_count": 0, "max_items": len(catalog)})
    session["max_items"] = len(catalog)
    print("batch_start", now_iso(), "catalog", len(catalog), "ledger", len(ledger), flush=True)
    for item in catalog:
        existing = record_for(ledger, item["key"])
        if existing:
            if existing.get("status") == "complete":
                print("skip_complete", item["index"], item["key"], flush=True)
                continue
            print("stop_existing", item["index"], item["key"], existing.get("status"), flush=True)
            break
        if session["submitted_count"] >= session["max_items"]:
            print("stop_catalog_limit", session["submitted_count"], flush=True)
            break
        try:
            status, queue = api("/api/queue", timeout=15)
        except Exception as exc:
            print("stop_queue_error", repr(exc), flush=True)
            break
        if status != 200 or not queue.get("reachable"):
            print("stop_queue_unreachable", status, queue, flush=True)
            break
        if queue.get("running") or queue.get("pending"):
            print("stop_queue_busy", queue, flush=True)
            break
        row = {
            "index": item["index"],
            "key": item["key"],
            "bvid": item["bvid"],
            "scene": item["scene"],
            "series": item["series"],
            "title": item["title"],
            "reference": item["reference"],
            "prompt": item["prompt"],
            "char_count": item["char_count"],
            "sha256": item["sha256"],
            "seed": item["seed"],
            "submitted_at": now_iso(),
            "submit_wall": time.time(),
            "prompt_id": None,
            "status": "submitting",
            "files": [],
            "verdict": "pending",
        }
        ledger.append(row)
        session["submitted_count"] += 1
        session["updated"] = now_iso()
        atomic_json(LEDGER, ledger)
        atomic_json(SESSION, session)
        try:
            status, response = api("/api/generate_moss", {"prompt": item["prompt"], "seed": item["seed"]}, timeout=45)
        except Exception as exc:
            prompt_id, candidates = reconcile_new_output(row["submit_wall"])
            row["prompt_id"] = prompt_id
            row["files"] = [str(path) for path in candidates]
            row["status"] = "complete" if prompt_id and len(candidates) == 1 else "uncertain"
            row["reconciled"] = row["status"] == "complete"
            row["error"] = repr(exc)
            atomic_json(LEDGER, ledger)
            print("submission_uncertain", item["index"], item["key"], repr(exc), "reconciled", row["status"], flush=True)
            break
        row["http_status"] = status
        row["response"] = response
        if status == 409:
            session["submitted_count"] -= 1
            session["updated"] = now_iso()
            atomic_json(SESSION, session)
            row["status"] = "blocked"
            atomic_json(LEDGER, ledger)
            print("submission_blocked", item["index"], item["key"], response, flush=True)
            break
        if status != 200 or not response.get("ok") or not response.get("prompt_id"):
            if 400 <= status < 500:
                session["submitted_count"] -= 1
                session["updated"] = now_iso()
                atomic_json(SESSION, session)
            row["status"] = "failed"
            atomic_json(LEDGER, ledger)
            print("submission_failed", item["index"], item["key"], status, response, flush=True)
            break
        row["prompt_id"] = response["prompt_id"]
        row["status"] = "running"
        atomic_json(LEDGER, ledger)
        print("submitted", item["index"], item["key"], row["prompt_id"], item["seed"], flush=True)
        started = time.time()
        last_state = None
        while time.time() - started < 3600:
            try:
                status, task = api("/api/task?id=" + urllib.parse.quote(str(row["prompt_id"])), timeout=20)
            except Exception as exc:
                status, task = 0, {"error": repr(exc)}
            state = task.get("state") if status == 200 else None
            if state != last_state:
                print("task_state", item["index"], item["key"], state, task.get("error", ""), flush=True)
                last_state = state
            if state == "success":
                files = files_from_task(task)
                deadline = time.time() + 30
                while time.time() < deadline and len(files) != 1:
                    time.sleep(2)
                    files = files_from_task(task)
                row["files"] = [str(path) for path in files]
                row["history_files"] = task.get("files") or []
                row["status"] = "complete" if len(files) == 1 else "incomplete"
                row["completed_at"] = now_iso()
                if row["status"] == "complete":
                    row["output_sha256"] = sha256_bytes(files[0])
                    row["output_bytes"] = files[0].stat().st_size
                atomic_json(LEDGER, ledger)
                print("task_complete", item["index"], item["key"], row["status"], len(files), flush=True)
                break
            if state == "error":
                row["status"] = "failed"
                row["failed_at"] = now_iso()
                row["task"] = task
                atomic_json(LEDGER, ledger)
                print("task_error", item["index"], item["key"], task, flush=True)
                break
            time.sleep(5)
        else:
            row["status"] = "uncertain"
            row["error"] = "task timeout after 3600s"
            atomic_json(LEDGER, ledger)
            print("task_timeout", item["index"], item["key"], flush=True)
        if row["status"] != "complete":
            print("batch_stop", item["index"], item["key"], row["status"], flush=True)
            break
        try:
            time.sleep(2)
        except KeyboardInterrupt:
            print("interrupted_after_complete", item["index"], flush=True)
            break
    print("batch_end", now_iso(), "submitted", session["submitted_count"], "complete", sum(r.get("status") == "complete" for r in ledger), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "run"], nargs="?", default="check")
    args = parser.parse_args()
    validate()
    if args.action == "run":
        run()


if __name__ == "__main__":
    main()
