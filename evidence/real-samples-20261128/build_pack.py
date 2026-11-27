# -*- coding: utf-8 -*-
import csv
import difflib
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from PIL import Image

PACK = Path(__file__).resolve().parent
ROOT = Path(r"C:\tmp_zmf\tujian_moss_199_20261124")
GRAPH = Path(r"C:\tmp_zmf\gallery\api_graph_moss.json")
DAILY = Path(r"C:\tmp_zmf\zmf_mj45_v2_daily_20261127")
PREVIEWS = PACK / "previews"
RAW = PACK / "source_records"
THUMB_SIZE = (384, 216)
PASS_SAMPLE_COUNT = 21


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso_from_timestamp(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def copy_source(source, name):
    target = RAW / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target.relative_to(PACK).as_posix()


def make_preview(source, record_id):
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    target = PREVIEWS / (record_id + ".jpg")
    with Image.open(source) as im:
        im = im.convert("RGB")
        im.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", THUMB_SIZE, "white")
        canvas.paste(im, ((THUMB_SIZE[0] - im.width) // 2, (THUMB_SIZE[1] - im.height) // 2))
        canvas.save(target, "JPEG", quality=78, optimize=True)
    return target.relative_to(PACK).as_posix()


def output_metadata(path, record_id):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    with Image.open(path) as im:
        width, height = im.size
    return {
        "original_filename": path.name,
        "original_bytes": path.stat().st_size,
        "original_mtime_utc": iso_from_timestamp(path.stat().st_mtime),
        "original_sha256": sha256_file(path),
        "width": width,
        "height": height,
        "preview": make_preview(path, record_id),
        "preview_max_size": list(THUMB_SIZE),
        "preview_note": "Lossy JPEG thumbnail for AI inspection; original PNG is not uploaded.",
    }


def model_profile():
    graph = read_json(GRAPH)
    sampler = graph["16"]["inputs"]
    return {
        "model_mode": "MOSS",
        "label": "MOSS Base + SFT, two LoRAs",
        "base_model": graph["397"]["inputs"]["unet_name"],
        "sft_model": graph["28"]["inputs"]["unet_name"],
        "merge_preset": graph["396"]["inputs"]["preset"],
        "merge_strength": graph["396"]["inputs"]["strength"],
        "lora_chain": [
            {"name": graph["394"]["inputs"]["lora_name"], "strength": graph["394"]["inputs"]["strength_model"]},
            {"name": graph["395"]["inputs"]["lora_name"], "strength": graph["395"]["inputs"]["strength_model"]},
        ],
        "text_encoder": graph["29"]["inputs"]["clip_name"],
        "vae": graph["27"]["inputs"]["vae_name"],
        "sampler": sampler["sampler_name"],
        "scheduler": sampler["scheduler"],
        "steps": graph["356"]["inputs"]["value"],
        "cfg": graph["357"]["inputs"]["value"],
        "denoise": graph["427"]["inputs"]["value"],
        "seed_source": "per-record seed injected into node 16",
        "negative_prompt": graph["26"]["inputs"]["text"],
        "text_to_image": True,
        "reference_image_used": False,
        "resolution_selector": graph["19"]["inputs"],
        "evidence": {
            "graph": "source_records/api_graph_moss.json",
            "graph_sha256": sha256_file(GRAPH),
            "graph_mtime": iso_from_timestamp(GRAPH.stat().st_mtime),
            "injection_code": "zmf-backend/server.py:1293-1301; source_records/run_moss_batch.py:183-200",
            "limitation": "The ledger stores prompt_id and output hash but not a graph hash; the template mtime predates the 2026-11-24 run.",
        },
    }


def evenly_spaced(items, count):
    if count > len(items):
        raise ValueError("Sample count exceeds population")
    if count == 1:
        return [items[len(items) // 2]]
    indexes = {round(i * (len(items) - 1) / (count - 1)) for i in range(count)}
    return [items[i] for i in sorted(indexes)]


def select_sample(ledger, vision_by_key):
    passes = [row for row in ledger if vision_by_key[row["key"]]["verdict"] == "pass"]
    near_rejects = [row for row in ledger if vision_by_key[row["key"]]["verdict"] == "near_reject"]
    other = [row for row in ledger if vision_by_key[row["key"]]["verdict"] not in ("pass", "near_reject")]
    if other:
        raise RuntimeError("Unexpected verdicts in source population: " + repr([r["key"] for r in other]))
    chosen = near_rejects + evenly_spaced(passes, PASS_SAMPLE_COUNT)
    chosen.sort(key=lambda row: row["index"])
    return chosen, {
        "strategy": "all near_reject records plus pass records evenly spaced across the original ordered population",
        "source_population": len(ledger),
        "source_pass": len(passes),
        "source_near_reject": len(near_rejects),
        "sample_pass": PASS_SAMPLE_COUNT,
        "sample_near_reject": len(near_rejects),
        "sample_total": len(chosen),
        "rate_estimation_warning": "Failure-enriched sample; do not use this pack to estimate the population acceptance rate.",
    }


def write_jsonl(path, rows):
    with Path(path).open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_csv(path, rows):
    fields = [
        "record_id", "source_index", "key", "bvid", "scene", "series", "title", "timestamp",
        "final_moss_prompt", "prompt_sha256", "prompt_char_count", "source_asset", "source_material_id",
        "style_id", "color_id", "layout_id", "recipe_id", "model_mode", "model", "sampler",
        "scheduler", "steps", "cfg", "seed", "selected", "selection_status", "verdict", "score",
        "subject_action", "composition", "light_color", "material_mood", "selection_reason",
        "rejection_reason", "verdict_reason", "hard_constraints_missing", "judged_by", "reviewer_type",
        "sample_stratum", "original_filename", "original_sha256", "original_bytes", "width", "height",
        "preview", "missing_fields",
    ]
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            flat = {key: row.get(key) for key in fields}
            for key in ("original_filename", "original_sha256", "original_bytes", "width", "height", "preview"):
                flat[key] = row["output"][key]
            flat["missing_fields"] = "|".join(row["missing_fields"])
            writer.writerow(flat)


def build_daily_audit():
    catalog = read_json(DAILY / "catalog.json")
    normalize = lambda text: re.sub(r"\s+", " ", str(text).strip().lower())
    exact_groups = defaultdict(list)
    theme_groups = defaultdict(list)
    source_groups = defaultdict(list)
    for row in catalog:
        exact_groups[normalize(row["prompt"])].append(row["tag"])
        theme_groups[row["theme_zh"]].append(row)
        source_groups[row["source"]].append(row)
    pairs = []
    for theme, rows in theme_groups.items():
        for i, left in enumerate(rows):
            for right in rows[i + 1:]:
                ratio = difflib.SequenceMatcher(None, left["prompt"], right["prompt"], autojunk=False).ratio()
                pairs.append({
                    "theme_zh": theme,
                    "left_tag": left["tag"],
                    "right_tag": right["tag"],
                    "left_key": left["key"],
                    "right_key": right["key"],
                    "left_source": left["source"],
                    "right_source": right["source"],
                    "character_similarity": round(ratio, 6),
                })
    pairs.sort(key=lambda row: row["character_similarity"], reverse=True)
    audit = {
        "source": "zmf_mj45_v2_daily_20261127/catalog.json",
        "record_count": len(catalog),
        "unique_prompt_sha256": len({row["sha256"] for row in catalog}),
        "unique_normalized_prompt_text": len(exact_groups),
        "exact_normalized_duplicate_groups": [tags for tags in exact_groups.values() if len(tags) > 1],
        "unique_theme_zh": len(theme_groups),
        "unique_source_assets": len(source_groups),
        "theme_reuse": {
            "groups_with_multiple_prompts": sum(len(rows) > 1 for rows in theme_groups.values()),
            "max_prompts_per_theme": max(len(rows) for rows in theme_groups.values()),
            "top_groups": [
                {"theme_zh": theme, "prompt_count": len(rows), "tags": [row["tag"] for row in rows]}
                for theme, rows in sorted(theme_groups.items(), key=lambda item: len(item[1]), reverse=True)[:20]
            ],
        },
        "source_reuse": {
            "max_prompts_per_source": max(len(rows) for rows in source_groups.values()),
            "top_groups": [
                {"source": source, "prompt_count": len(rows), "tags": [row["tag"] for row in rows]}
                for source, rows in sorted(source_groups.items(), key=lambda item: len(item[1]), reverse=True)[:20]
            ],
        },
        "within_theme_character_similarity": {
            "method": "Python difflib.SequenceMatcher on full prompt text, autojunk=False; character similarity, not semantic identity",
            "pair_count": len(pairs),
            "median": round(median(row["character_similarity"] for row in pairs), 6),
            "max": pairs[0]["character_similarity"],
            "threshold_counts": {str(t): sum(row["character_similarity"] >= t for row in pairs) for t in (0.95, 0.90, 0.85, 0.80, 0.75)},
            "top_100_pairs": pairs[:100],
        },
        "prompt_length_characters": {
            "min": min(len(row["prompt"]) for row in catalog),
            "median": median(len(row["prompt"]) for row in catalog),
            "max": max(len(row["prompt"]) for row in catalog),
        },
        "selection_labels_present": False,
        "interpretation_limit": "This audit measures prompt reuse only. It does not prove visual duplication, scenery-only output, or why any individual image was rejected.",
    }
    supplement = PACK / "supplemental_daily440"
    supplement.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DAILY / "catalog.json", supplement / "catalog.json")
    shutil.copy2(DAILY / "wallpaper_candidates_technical.json", supplement / "wallpaper_candidates_technical.json")
    write_json(supplement / "prompt_reuse_audit.json", audit)
    return audit


def main():
    ledger = read_json(ROOT / "ledger.json")
    vision_state = read_json(ROOT / "auto_vision.json")
    vision_items = vision_state["items"]
    vision_by_key = {row["key"]: row for row in vision_items}
    if len(ledger) != 199 or len(vision_items) != 199 or len(vision_by_key) != 199:
        raise RuntimeError("Expected 199 unique ledger and vision records")
    profile = model_profile()
    chosen, sampling = select_sample(ledger, vision_by_key)
    if len(chosen) != 50:
        raise RuntimeError("Expected 50 sampled records")

    records = []
    for row in chosen:
        vision = vision_by_key[row["key"]]
        prompt = row["prompt"]
        if sha256_text(prompt) != row["sha256"] or vision["prompt_sha256"] != row["sha256"]:
            raise RuntimeError("Prompt hash mismatch: " + row["key"])
        if len(row["files"]) != 1 or Path(row["files"][0]).name != vision["file"].split("\\")[-1]:
            raise RuntimeError("Output file mismatch: " + row["key"])
        selected = vision["verdict"] == "pass"
        record_id = "MOSS199_" + row["key"].replace("/", "_")
        missing = [name for name, value in (
            ("style_id", None), ("color_id", None), ("layout_id", None), ("recipe_id", None),
        ) if value is None]
        record = {
            "record_id": record_id,
            "source_index": row["index"],
            "key": row["key"],
            "bvid": row["bvid"],
            "scene": row["scene"],
            "series": row["series"],
            "title": row["title"],
            "timestamp": row.get("completed_at") or row.get("submitted_at"),
            "final_moss_prompt": prompt,
            "prompt_sha256": row["sha256"],
            "prompt_char_count": row["char_count"],
            "source_asset": row["reference"],
            "source_material_id": row["key"],
            "style_id": None,
            "color_id": None,
            "layout_id": None,
            "recipe_id": None,
            "model_mode": "MOSS",
            "model": profile["label"],
            "sampler": profile["sampler"],
            "scheduler": profile["scheduler"],
            "steps": profile["steps"],
            "cfg": profile["cfg"],
            "seed": row["seed"],
            "model_profile": profile,
            "workflow_graph_sha256": profile["evidence"]["graph_sha256"],
            "selected": selected,
            "selection_status": "auto_pass" if selected else "auto_near_reject",
            "verdict": vision["verdict"],
            "score": vision["score"],
            "subject_action": vision["subject_action"],
            "composition": vision["composition"],
            "light_color": vision["light_color"],
            "material_mood": vision["material_mood"],
            "selection_reason": vision["reasons"] if selected else None,
            "rejection_reason": vision["missing"] if not selected else None,
            "verdict_reason": vision["reasons"],
            "hard_constraints_missing": vision["missing"],
            "judged_by": vision["judged_by"],
            "reviewer_type": "automatic_vision",
            "rubric": vision_state["rubric"],
            "judged_at": vision_state["updated"],
            "sample_stratum": "pass_evenly_spaced" if selected else "all_near_reject",
            "raw_review_source": "source_records/auto_vision.json",
            "raw_generation_source": "source_records/ledger.json",
            "output": output_metadata(row["files"][0], record_id),
            "missing_fields": missing,
            "evidence_origin": {
                "prompt": "ledger.json prompt; sha256 verified against catalog/ledger/vision record",
                "parameters": "api_graph_moss.json template plus per-record seed; server.py injects prompt and seed",
                "selection": "auto_vision.json automatic visual verdict, score, subscores, reasons, missing",
            },
        }
        if row.get("output_sha256") and row["output_sha256"] != record["output"]["original_sha256"]:
            raise RuntimeError("Output hash mismatch: " + row["key"])
        records.append(record)

    write_jsonl(PACK / "records.jsonl", records)
    write_csv(PACK / "records.csv", records)
    write_json(PACK / "model_profile.json", profile)

    source_files = [
        (ROOT / "ledger.json", "ledger.json"),
        (ROOT / "auto_vision.json", "auto_vision.json"),
        (ROOT / "catalog.json", "catalog.json"),
        (ROOT / "run_moss_batch.py", "run_moss_batch.py"),
        (ROOT / "auto_accept.py", "auto_accept.py"),
        (GRAPH, "api_graph_moss.json"),
    ]
    source_manifest = []
    for source, name in source_files:
        copied = copy_source(source, name)
        source_manifest.append({"file": copied, "sha256": sha256_file(source), "bytes": source.stat().st_size})

    daily_audit = build_daily_audit()
    coverage = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "record_unit": "one MOSS output image",
        "record_count": len(records),
        "preview_count": len(list(PREVIEWS.glob("*.jpg"))),
        "source_population": {
            "path": "tujian_moss_199_20261124",
            "ledger_records": len(ledger),
            "vision_records": len(vision_items),
            "verdict_counts": dict(Counter(row["verdict"] for row in vision_items)),
            "score_counts": dict(Counter(str(row["score"]) for row in vision_items)),
            "series_counts": dict(Counter(row["series"] for row in ledger)),
            "unique_bvids": len({row["bvid"] for row in ledger}),
        },
        "sampling": sampling,
        "sample_verdict_counts": dict(Counter(row["verdict"] for row in records)),
        "sample_series_counts": dict(Counter(row["series"] for row in records)),
        "missing_field_counts": dict(Counter(name for row in records for name in row["missing_fields"])),
        "reviewer": {
            "type": "automatic_vision",
            "backend": vision_state["backend"],
            "rubric": vision_state["rubric"],
            "note": "These are automatic visual-acceptance verdicts, not human final gallery selections.",
        },
        "user_supplied_root_cause_observation": {
            "text": "淘汰理由：一个主题，扩写好几份提示词，重复的很多或者全是不重要的风景，重复的很多",
            "scope": "General human observation supplied with this export; not a per-record label and not automatically assigned to individual records.",
            "supplemental_daily440_audit": "supplemental_daily440/prompt_reuse_audit.json",
        },
        "supplemental_daily440": {
            "catalog_records": daily_audit["record_count"],
            "unique_theme_zh": daily_audit["unique_theme_zh"],
            "unique_source_assets": daily_audit["unique_source_assets"],
            "exact_normalized_duplicate_groups": len(daily_audit["exact_normalized_duplicate_groups"]),
            "within_theme_pairs": daily_audit["within_theme_character_similarity"]["pair_count"],
            "within_theme_median_character_similarity": daily_audit["within_theme_character_similarity"]["median"],
            "pairs_at_least_0_85": daily_audit["within_theme_character_similarity"]["threshold_counts"]["0.85"],
            "selection_labels_present": False,
        },
        "limitations": [
            "This 50-record pack is failure-enriched and cannot estimate the population acceptance rate.",
            "The source population has 170 pass and 29 near_reject automatic verdicts; this pack includes all 29 near_reject and 21 evenly spaced pass records.",
            "Style/color/layout/recipe IDs are absent from the source ledger and remain null.",
            "Automatic near_reject is not the same as final human rejection; use it as evidence for root-cause analysis, not as a final gallery label.",
            "The 440-group 2026-11-27 daily batch is not included as selected/rejected evidence because no visual shortlist was found for it.",
            "The user-supplied duplication/scenery observation is stored separately because the source records do not contain a per-record duplication label.",
        ],
        "source_files": source_manifest,
    }
    write_json(PACK / "coverage.json", coverage)
    print(json.dumps({"records": len(records), "pass": sum(r["selected"] for r in records), "near_reject": sum(not r["selected"] for r in records), "pack": str(PACK)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
