import argparse
import base64
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(r"C:/tmp_zmf/tujian_moss_199_20261124")
CONFIG = Path(r"C:/Users/hello Teng/Documents/Qoder/2026-10-28/5a63e8c8/wz_sol.json")
SYSTEM = (
    "你是严格的图像验收员。只依据生成图和给定提示词判断，不推测画外内容。"
    "重点核对主体与动作、构图与机位、光色、材质氛围。输出一个JSON对象，不要Markdown："
    '{"verdict":"pass|near_reject|fail|error","score":0,"subject_action":0,'
    '"composition":0,"light_color":0,"material_mood":0,"reasons":"中文简述",'
    '"missing":"主要缺失或错误；没有则写无"}。四个分项各0-2分。'
    "主体动作和构图均成立且没有关键错误才pass；主关系成立但有局部偏差为near_reject；"
    "主体、动作或核心构图错误为fail。"
)


def load_ledgers(paths):
    rows = []
    for name in paths:
        data = json.loads(Path(name).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("items", [])
        rows.extend(data)
    return rows


def image_data_url(path):
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def parse_result(content):
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("响应不是JSON对象")
    result = json.loads(content[start:end + 1])
    if result.get("verdict") not in ("pass", "near_reject", "fail"):
        raise ValueError("verdict无效")
    for key in ("score", "subject_action", "composition", "light_color", "material_mood"):
        value = result.get(key)
        if type(value) not in (int, float) or not 0 <= value <= (100 if key == "score" else 2):
            raise ValueError("分数字段无效: " + key)
    result["score"] = int(result["score"])
    return result


def judge(task, cfg):
    started = time.time()
    payload = {
        "model": cfg["model"],
        "temperature": cfg.get("temperature", 0.2),
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "待验收提示词：\n" + task["caption"]},
                {"type": "image_url", "image_url": {"url": image_data_url(task["file"])}},
            ]},
        ],
    }
    effort = cfg.get("reasoning_effort")
    if effort:
        payload["reasoning_effort"] = effort
    response = requests.post(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if response.status_code != 200:
        raise RuntimeError("HTTP {}: {}".format(response.status_code, response.text[:300]))
    body = response.json()
    content = body["choices"][0]["message"].get("content") or ""
    result = parse_result(content)
    result.update(
        id=task["id"], model=task["model"], file=task["file"], prompt_sha256=task["sha256"],
        seconds=round(time.time() - started, 2), judged_at=datetime.now().astimezone().isoformat(),
    )
    return result


def atomic_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if not cfg.get("api_key") or not cfg.get("base_url") or not cfg.get("model"):
        raise SystemExit("视觉接口配置不完整")
    state = {"created": datetime.now().astimezone().isoformat(), "source_ledgers": args.ledger, "items": []}
    if args.output.exists():
        state = json.loads(args.output.read_text(encoding="utf-8"))
    done = {(row["id"], row["model"]) for row in state["items"] if row.get("verdict") != "error"}
    tasks = []
    for row in load_ledgers(args.ledger):
        for file_name in row.get("files", []):
            model = "M1" if "_M1_" in Path(file_name).name else "M2" if "_M2_" in Path(file_name).name else "MOSS"
            if (row["id"], model) in done:
                continue
            path = Path(file_name)
            if not path.exists() or path.stat().st_size == 0:
                continue
            tasks.append({"id": row["id"], "model": model, "file": str(path),
                          "caption": row["caption"], "sha256": row["sha256"]})
    if args.limit is not None:
        tasks = tasks[:args.limit]
    print("pending {} | cached {} | workers {}".format(len(tasks), len(done), args.workers), flush=True)
    if not tasks:
        print(args.output, flush=True)
        return
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(judge, task, cfg): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"id": task["id"], "model": task["model"], "file": task["file"],
                          "prompt_sha256": task["sha256"], "verdict": "error",
                          "reasons": type(exc).__name__ + ": " + str(exc)[:500],
                          "judged_at": datetime.now().astimezone().isoformat()}
            state["items"] = [row for row in state["items"] if (row["id"], row["model"]) != (task["id"], task["model"])]
            state["items"].append(result)
            state["items"].sort(key=lambda row: (row["id"], row["model"]))
            state["updated"] = datetime.now().astimezone().isoformat()
            atomic_json(args.output, state)
            print("{} {} {} {}".format(task["id"], task["model"], result["verdict"], result.get("seconds", "")), flush=True)
    counts = {key: sum(row.get("verdict") == key for row in state["items"])
              for key in ("pass", "near_reject", "fail", "error")}
    print(args.output, flush=True)
    print(json.dumps(counts, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
