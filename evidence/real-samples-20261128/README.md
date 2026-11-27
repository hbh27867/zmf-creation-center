# ZMF 真实样本证据包（50 条）

## AI 先读这些

- `records.jsonl`：50 条主记录，一行一条 JSON，推荐直接读取。
- `records.csv`：同 50 条的表格版，UTF-8 BOM，可用 Excel 打开。
- `previews/`：每条记录一张 384×216 JPEG 缩略图；原图未上传。
- `coverage.json`：抽样方式、字段覆盖、限制、来源文件哈希。
- `model_profile.json`：MOSS Base+SFT、LoRA、采样器、步数、CFG、负面词与工作流证据。
- `source_records/`：原始 ledger、自动视觉验收 JSON、工作流和验收脚本，供核对。

## 记录单位与抽样

一条记录 = 一张 MOSS 输出图。来源为 `tujian_moss_199_20261124` 的 199 条真实生成与自动视觉验收记录。

- 原始总体：170 `pass`，29 `near_reject`。
- 本包：全部 29 条 `near_reject` + 按原顺序均匀抽取的 21 条 `pass` = 50 条。
- 这是失败富集样本，不能用来计算真实通过率。
- `pass` / `near_reject` 是自动视觉验收，不是最终人工图鉴入选结论。

## 每条记录包含

最终发给 MOSS 的完整提示词、提示词 SHA-256、源素材路径与 bvid/scene、模型与采样参数、seed、输出文件名/原图 SHA-256/尺寸、缩略图、自动验收分数与四项分项分、验收描述、具体缺失项。

`style_id`、`color_id`、`layout_id`、`recipe_id` 在来源记录中不存在，保持 `null` 并列入 `missing_fields`，没有推测补值。

## 字段含义

- `selected=true`：自动验收为 `pass`。
- `rejection_reason`：`near_reject` 的具体缺失或错误。
- `verdict_reason`：自动验收对画面的事实描述。
- `hard_constraints_missing`：验收员指出的主要缺失/错误。
- `judged_by=current_assistant_vision`：自动视觉后端，不是人工。
- `sample_stratum`：`all_near_reject` 或 `pass_evenly_spaced`。

## 用户补充的根因观察

> 淘汰理由：一个主题，扩写好几份提示词，重复的很多或者全是不重要的风景，重复的很多

这是人工总体观察，来源记录没有逐条重复标签，因此没有强行贴到单条记录。

为帮助 AI 检查该问题，另附：

- `supplemental_daily440/catalog.json`：2026-11-27 每日 440 组完整提示词与 ID。
- `supplemental_daily440/prompt_reuse_audit.json`：主题/素材复用与同主题提示词字符相似度审计。
- `supplemental_daily440/wallpaper_candidates_technical.json`：技术候选，不是人工入选结论。

440 组中没有完全相同的标准化提示词；但只有 99 个 `theme_zh`、33 个源素材，同主题提示词对的中位字符相似度为 0.831，313 对达到 0.85。该指标只说明文本接近，不直接证明画面重复或淘汰原因。
