# ZMF 创作中心源码与真实样本证据

本仓库包含 ZMF 创作中心源码、后端实现和用于根因分析的真实样本。

## AI 请优先读取

1. [records.jsonl](records.jsonl)：50 条真实 MOSS 样本。每条包含最终提示词、模型参数、seed、输出文件哈希、自动验收分数、具体缺失/淘汰原因。
2. [prompt_reuse_audit.json](prompt_reuse_audit.json)：2026-11-27 每日 440 组提示词的同主题复用、素材复用和字符相似度审计。
3. [证据说明](evidence/real-samples-20261128/README.md)：字段定义、抽样方式与限制。
4. [ZMF 创作中心页面](handraw-skill/gallery/zmf.html)：创作中心前端源码。
5. [后端服务](zmf-backend/server.py)：`/api/assemble`、`/api/generate_dual` 等接口实现。

## 重要限制

- 50 条样本为失败富集样本：21 条自动验收通过、29 条自动验收近淘汰，不能据此计算真实通过率。
- 自动视觉验收不是最终人工图鉴入选结论。
- 用户补充的根因观察是“同主题扩写重复、无效风景过多”；该观察与逐条自动验收标签分开保存，没有强行归类到单条记录。
