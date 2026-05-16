# 京剧剧本数据可视化 · 结构化流水线与赛题分析指南

面向 **赛题 I：京剧数据可视分析与人文创意** 的端到端方案：将 PDF 京剧剧本转为**可关联、可建图、可回溯证据**的多层结构化数据，并配套全库汇总表与分析脚手架，直接支撑赛题五项任务（行当、关系网络、主题、叙事结构、综合可视）。

| 项目元信息 | 说明 |
|------------|------|
| 主流水线脚本 | `mineru_batch_convert_structured_llm_final_v8.py` |
| 解析器版本 | `2026-05-16-r6`（常量 `PARSER_VERSION`） |
| 分析脚手架 | `analysis_starter.py` |
| 批处理辅助 | `run_opera_01000000.ps1`（《戏考》合集 `01000000`） |
| 原始输入 | `opera_dataset/<合集>/` 下 PDF |
| 结构化输出 | `opera_output/<合集>/<剧目>/` |
| 全库汇总 | `opera_output/all_*.csv`、`all_structured.jsonl` |

---

## 目录

1. [赛题五任务 ↔ 本项目产出总览](#一赛题五任务--本项目产出总览)
2. [当前项目完成状态（实测）](#二当前项目完成状态实测)
3. [项目目录结构](#三项目目录结构)
4. [环境准备](#四环境准备)
5. [完整操作流程（分步：方式 → 结果 → 赛题用途）](#五完整操作流程分步方式--结果--赛题用途)
6. [数据处理流水线详解](#六数据处理流水线详解)
7. [输出目录与文件说明](#七输出目录与文件说明)
8. [核心字段说明](#八核心字段说明)
9. [赛题五任务数据对照（详细）](#九赛题五任务数据对照详细)
10. [数据质量与使用规范](#十数据质量与使用规范)
11. [分析脚手架 analysis_starter.py](#十一分析脚手架-analysis_starterpy)
12. [中断、重启与维护](#十二中断重启与维护)
13. [命令速查表](#十三命令速查表)
14. [团队协作与 GitHub](#十四团队协作与-github)
15. [赛题满足度评估](#十五赛题满足度评估)
16. [常见问题](#十六常见问题)

---

## 一、赛题五任务 ↔ 本项目产出总览

赛题要求从 **行当与年代、人物关系、主题、叙事结构、综合可视化** 五个维度分析京剧剧本。本仓库**不是**只产出 Markdown，而是产出可 join 的多张表 + 全剧 JSON。

| 赛题任务 | 核心问题 | 本项目主要数据 | 推荐入口 |
|----------|----------|----------------|----------|
| **任务一** | 行当、唱念做打、年代差异 | `all_roles.csv`、`all_performances.csv`、`all_dialogues.csv` + `all_docs.period_hint` | 按 `doc_id` 聚合角色与表演事件 |
| **任务二** | 人物关系网络、中心性、戏种比较 | `all_relations_aggregated.csv`、`all_network_metrics.csv`、分场 `relations.csv` | 全剧网络图；分场子图用 `scene_index` 过滤 |
| **任务三** | 主题构成、组合、主题—角色 | `all_themes_aggregated.csv`、`all_themes.csv`、`all_theme_pairs.csv` | 条形图 / 共现弦图 / `theme_role_links` |
| **任务四** | 叙事阶段、张力、高潮、节奏 | `all_narrative_curve.csv`、`all_scenes.csv`、`all_performances.csv` | 折线/面积图；`is_climax` 标高潮 |
| **任务五** | 综合可视、多视图联动 | `structured.json`（单剧）+ `all_docs.csv`（索引） | 列表页 + 详情五视图；边/场点击回链 `dialogues` |

**分析前必做筛选**：`all_docs.csv` 中 `analysis_ready == True` 的剧目（质量分、场次、台词数达标），避免将 PDF 解析失败剧目纳入全库结论。

---

## 二、当前项目完成状态（实测）

以下数据来自本地 **`--status-only`** 与 **`all_*.csv`**（`--collection-prefix 01000000`），随批处理推进会变化；重启全量前可先自行执行 status 命令刷新。

### 2.1 《戏考》合集 `01000000` 批处理进度

| 指标 | 数值（约） | 含义 |
|------|------------|------|
| PDF 总数 | **448** | `opera_dataset/01000000/*.pdf` |
| **r6 校验通过** | **58** | `structured.json` + 分层 CSV 齐全且 `parser_version=2026-05-16-r6`，重启批处理会 **SKIP** |
| 旧版待升级 | **20** | 有输出但 parser 非 r6，重启后会**自动重跑** |
| 尚未处理 | **370** | 无有效 r6 输出 |
| MinerU 剩余批次 | 约 **8** 批 | 按 `chunk-size 50` 估算 |

### 2.2 全库汇总表 `opera_output/all_*.csv`（01000000）

| 文件 | 规模（约） | 说明 |
|------|------------|------|
| `all_docs.csv` | **78** 部索引行 | 含 structured.json 的剧目（略多于 r6_ok，含待升级/混合状态） |
| `analysis_ready=True` | **58** 部 | **建议全库统计仅用此子集** |
| 质量 high / medium / low | 57 / 1 / 20 | 低质量多为 PDF 表格压成旁白、场次极少 |
| `all_roles.csv` | 347 行 | 任务一 |
| `all_dialogues.csv` | 10 304 行 | 台词语料、证据回链 |
| `all_relations_aggregated.csv` | 1 579 行 | 任务二主表 |
| `all_narrative_curve.csv` | 353 行 | 任务四 |

**结论**：数据**已可用于赛题分析与可视化原型**；全库 448 部尚未跑完时，应用 `analysis_ready` 过滤，并在论文/报告中注明当前样本量。全量完成后执行 `--combine-only` 刷新汇总表。

---

## 三、项目目录结构

### 3.1 仓库内应提交的内容（精简）

```
项目根目录/
├── README.md                          # 本文档（唯一完整说明）
├── 京剧剧本结构化数据说明.md           # 指向 README 的索引
├── requirements.txt
├── .env.example                       # 密钥模板（勿提交 .env）
├── mineru_batch_convert_structured_llm_final_v8.py
├── analysis_starter.py
├── run_opera_01000000.ps1
└── data/README.md                     # 大数据网盘说明
```

### 3.2 本地运行时的数据目录（勿提交 Git）

```
opera_dataset/                         # 输入 PDF（约 616 MB）
└── 01000000/                          # 《戏考》448 部
    └── *.pdf

opera_output/                          # 输出（本地生成或网盘同步）
├── all_docs.csv                       # 全库索引 ★ 分析主入口
├── all_roles.csv … all_structured.jsonl
├── batch_01000000_run.log             # 批处理日志
├── mineru_manifest_01000000.csv       # 处理清单（可选）
└── 01000000/
    └── 01001001_空城计/               # 单剧分层目录（见下文）
```

### 3.3 单剧 r6 分层目录（标准形态）

```
opera_output/01000000/01001001_空城计/
├── README.txt                         # 子目录说明
├── structured.json                    # 全量 JSON（任务五 / 前端主入口）
├── mineru_result.json                 # MinerU 原始返回（审计）
├── 01001001_空城计.md                 # 可选：保留的剧名 Markdown
├── 01_meta/documents.csv              # 文档元数据（1 行/剧）
├── 02_cast/roles.csv                  # 角色画像
├── 03_script/
│   ├── scenes.csv                     # 场次
│   ├── dialogues.csv                  # 台词
│   └── performances.csv               # 唱念做打
├── 04_graph/
│   ├── relations.csv                  # 分场关系（含证据）
│   ├── relations_aggregated.csv       # 全剧聚合边（任务二）
│   ├── network_metrics.csv            # 度中心性等
│   └── entity_aliases.csv             # 别名归一
├── 05_themes/
│   ├── themes.csv
│   ├── themes_aggregated.csv
│   └── theme_pairs.csv
├── 06_narrative/narrative_curve.csv
└── audit/
    ├── cleaned_full.md                # 清洗后全文
    └── structured_raw.json            # 规则解析快照
```

> **注意**：剧目根目录下**不应再出现** `dialogues.csv`、`documents.csv` 等扁平 CSV（r4 遗留）。若存在，运行 `--cleanup-legacy` 或在 r6 写出后自动删除。分析时**只读** `01_meta`～`06_narrative` 子目录。

---

## 四、环境准备

### 4.1 Python 依赖

```powershell
cd D:\University_studies\Junior\data_visualization\experiment\final_self

python -m venv .venv
.\.venv\Scripts\Activate.ps1
# 或：conda activate deepseek_env

pip install -r requirements.txt
```

`requirements.txt`：`requests`、`pandas`、`matplotlib`、`networkx`（网络布局可选）。

### 4.2 API 密钥

| 变量 | 用途 | 获取方式 |
|------|------|----------|
| `MINERU_TOKEN` | PDF → Markdown（MinerU 批量 API） | [MinerU](https://mineru.net/) |
| `DEEPSEEK_API_KEY` | LLM 增强（`--llm-enabled`） | DeepSeek 开放平台 |

**配置方式（二选一）**：

```powershell
# 方式 A：项目根目录 .env（推荐，参考 .env.example）
copy .env.example .env
# 编辑 .env 填入密钥

# 方式 B：当前 PowerShell 会话
$env:MINERU_TOKEN = "你的MinerU密钥"
$env:DEEPSEEK_API_KEY = "你的DeepSeek密钥"
```

脚本启动时会自动 `load_local_env()` 读取 `.env`。

### 4.3 放置 PDF

将赛题 PDF 放入：

```
opera_dataset\01000000\*.pdf
```

`--input-dir opera_dataset\01000000` 时，输出自动写入 `opera_output\01000000\<剧目>\`，且自动推断 `--collection-prefix 01000000`。

---

## 五、完整操作流程（分步：方式 → 结果 → 赛题用途）

以下按**推荐执行顺序**排列；每步标明**命令/方式**、**得到什么**、**可用于赛题哪一部分**。

---

### 步骤 0：查看进度（不消耗 API）

| 项目 | 内容 |
|------|------|
| **方式** | `python mineru_batch_convert_structured_llm_final_v8.py --status-only ...` |
| **命令** | 见 [§十三](#十三命令速查表) |
| **结果** | 终端输出 `total / r6_ok / old_version / pending`；不修改任何文件 |
| **赛题用途** | 项目管理：估算剩余批次、决定是否中断重启 |

---

### 步骤 1：冒烟测试（2 部，验证 r6 + LLM）

| 项目 | 内容 |
|------|------|
| **方式** | `--limit 2 --llm-enabled`；首次验证可用 `--no-skip-existing` 强制重写 |
| **命令** | |
```powershell
python mineru_batch_convert_structured_llm_final_v8.py `
  --input-dir opera_dataset\01000000 `
  --output-dir opera_output `
  --llm-enabled `
  --limit 2 `
  --collection-prefix 01000000
```
| **结果** | `opera_output/01000000/<剧目>/` 下分层 CSV + `structured.json`；`parser_version=2026-05-16-r6`；`analysis_ready`、质量分写入 `01_meta/documents.csv` |
| **赛题用途** | 确认字段是否满足五类任务；案例剧如《空城计》可作任务二～五 demo |

---

### 步骤 2：全量批处理（448 部）

| 项目 | 内容 |
|------|------|
| **方式** | MinerU 每批最多约 50 个 PDF（`--chunk-size 50`）→ 规则解析 → 可选 LLM → 写出单剧包 → **每批结束自动 `--auto-combine`** |
| **命令** | |
```powershell
python mineru_batch_convert_structured_llm_final_v8.py `
  --input-dir opera_dataset\01000000 `
  --output-dir opera_output `
  --llm-enabled `
  --chunk-size 50 `
  --collection-prefix 01000000 `
  --manifest opera_output\mineru_manifest_01000000.csv `
  2>&1 | Tee-Object -FilePath opera_output\batch_01000000_run.log -Append
```
| **结果** | 每部：`structured.json` + `01_meta`～`06_narrative`；每批结束刷新 `opera_output/all_*.csv`；日志写入 `batch_01000000_run.log` |
| **赛题用途** | **全部任务的数据基础**；`all_*` 支撑跨剧统计，单剧目录支撑案例深钻与任务五联动 |

**重要**：

- **不要**加 `--no-skip-existing`（除非故意全库重跑），否则已完成的 r6 会重复消耗 MinerU/LLM。
- 已完成 r6 的剧目日志显示 `[SKIP] ... already valid r6 output`。
- `old_version` 剧目会自动重跑升级到 r6。

---

### 步骤 3：刷新全库汇总（中断后 / 手动同步）

| 项目 | 内容 |
|------|------|
| **方式** | `--combine-only`：扫描磁盘上所有 `structured.json`，重写 `all_*.csv` |
| **命令** | |
```powershell
python mineru_batch_convert_structured_llm_final_v8.py `
  --combine-only `
  --output-dir opera_output `
  --collection-prefix 01000000
```
| **结果** | 更新 `all_docs.csv`、`all_relations_aggregated.csv` 等 16 个汇总文件 |
| **赛题用途** | 队友仅用 `all_*` 即可做全库可视化，无需等 448 部全部跑完 |

---

### 步骤 4：清理遗留扁平文件（可选）

| 项目 | 内容 |
|------|------|
| **方式** | `--cleanup-legacy`（r6 写出后也会自动清理） |
| **命令** | `python mineru_batch_convert_structured_llm_final_v8.py --cleanup-legacy --output-dir opera_output --collection-prefix 01000000` |
| **结果** | 删除剧目根目录下与 `01_meta`～`audit` 重复的 r4 扁平 CSV |
| **赛题用途** | 避免误读旧版 CSV；目录规范利于协作 |

---

### 步骤 5：批处理质量检验

| 项目 | 内容 |
|------|------|
| **方式** | `analysis_starter.py --verify` |
| **命令** | `python analysis_starter.py --verify --collection 01000000 --data-dir opera_output` |
| **结果** | `analysis_figures/batch_verify_01000000.csv`（每剧 `parser_version`、`analysis_ready`、场次/台词数等） |
| **赛题用途** | 写报告时的样本说明、过滤 low 质量剧 |

---

### 步骤 6：生成示例分析图（单剧 + 全库）

| 项目 | 内容 |
|------|------|
| **方式** | `analysis_starter.py` |
| **命令** | 见 [§十一](#十一分析脚手架-analysis_starterpy) |
| **结果** | `analysis_figures/` 下 PNG + `play_summary.json` |
| **赛题用途** | 任务二网络图、任务四曲线、任务三主题条、全库质量概览；可直接放入答辩 PPT |

---

### 步骤 7：自定义分析 / 可视化开发

| 项目 | 内容 |
|------|------|
| **方式** | Python/R/前端读取 `all_*` 或 `structured.json` |
| **结果** | 论文图表、交互系统、统计检验 |
| **赛题用途** | 五项任务的全部深化（介数中心性、社群发现等需在分析端用 NetworkX 等补充） |

---

## 六、数据处理流水线详解

```
PDF
  → [阶段1] MinerU OCR → full.md → 清洗 → audit/cleaned_full.md
  → [阶段2] 规则解析：场次/角色/台词/唱念做打/启发式关系与主题
  → [阶段3] LLM 增强（--llm-enabled）：文档/角色/场次/台词/关系聚合
  → [阶段4] finalize：稳定 ID、别名、质量分、衍生表、证据回链
  → [阶段5] write_play_package：分层 CSV + structured.json
  → [阶段6] auto-combine：all_*.csv 全库汇总
```

### 阶段 1：文档获取与清洗

| 方式 | 结果 | 赛题用途 |
|------|------|----------|
| MinerU 批量上传 PDF，轮询 `batch_id` | `extracted/full.md`、zip 等（可选 `--keep-transient`） | 原始文本来源 |
| `clean_md` 去页眉、页码、网址等 | `audit/cleaned_full.md` | 人工核对、错误排查 |

### 阶段 2：规则解析（始终运行）

| 对象 | 方法 | 落盘 |
|------|------|------|
| 前言元数据 | `extract_preface_metadata` | `documents.csv` / metadata |
| 角色表 | `parse_role_section` | `02_cast/roles.csv` |
| 场次 | `【第×场】` 等模式 | `03_script/scenes.csv` |
| 台词 | 说话人 + 板式；表格拆行 | `03_script/dialogues.csv` |
| 伪说话人修正 | 如「启禀丞相」→ 报子 | 提高关系准确度 |
| 启发式关系 | 邻接对话、提及、同场共现 | `04_graph/relations.csv` |
| 启发式主题 | `THEME_KEYWORDS` | `05_themes/themes.csv` |

### 阶段 3：大语言模型增强（`--llm-enabled`）

| 环节 | 方式 | 落盘字段 | 赛题用途 |
|------|------|----------|----------|
| 文档元数据 | 前言 + 合集信息 + 台词样例 | `synopsis`、`genre_hint`、`doc_tags` | 任务五列表、任务一年代/戏种 |
| 角色画像 | 批量（默认 8 人/次） | `role_type_inferred`、`personality_tags`、`narrative_function` | **任务一** |
| 场次/关系/主题 | 按场调用 | `scenes.*`、`relations(derived_by=llm)` | **任务二、三、四** |
| 台词情感/受话人 | 关键台词批量（默认最多 100 条/剧） | `emotion_tag`、`target`、`speech_act` | 情感分析、关系验证 |
| 关系聚合校正 | 规则合并 + LLM Top 48 边 + 再合并 | `relations_aggregated`、`llm_confidence` | **任务二主网络** |

关闭部分 LLM：`--no-llm-dialogue`、`--no-llm-relation-refine`。

### 阶段 4：分析层（`finalize_structured_package`）

| 产出 | 方式 | 赛题用途 |
|------|------|----------|
| 稳定 ID | `char_id`、`scene_id`、`line_id` | 跨表 join、任务五联动 |
| 别名表 | `entity_aliases.csv` | 孔明→诸葛亮，避免重复节点 |
| 质量标记 | `parse_quality_score`、`parse_quality_label`、**`analysis_ready`** | 全库过滤 |
| 衍生表 | `theme_pairs`、`narrative_curve`、`network_metrics` | **任务三、四、二** |
| 证据回链 | `relations.evidence_line_ids` → `dialogues.line_id` | 论文引用原文 |

### 阶段 5～6：写出与汇总

| 方式 | 结果 | 赛题用途 |
|------|------|----------|
| `write_play_package` | 单剧分层目录 + `structured.json` | 任务五单剧详情 |
| `refresh_combined_exports`（每批 + 结束） | `all_*.csv` | 任务一至四跨剧分析 |

---

## 七、输出目录与文件说明

### 7.1 概念 ↔ 数据表映射

| 概念 | 单剧路径 | 全库汇总 |
|------|----------|----------|
| 文档总表 | `01_meta/documents.csv` | `all_docs.csv` |
| 角色表 | `02_cast/roles.csv` | `all_roles.csv` |
| 场次表 | `03_script/scenes.csv` | `all_scenes.csv` |
| 台词表 | `03_script/dialogues.csv` | `all_dialogues.csv` |
| 唱念做打 | `03_script/performances.csv` | `all_performances.csv` |
| 分场关系 | `04_graph/relations.csv` | `all_relations.csv` |
| 全剧关系网络 | `04_graph/relations_aggregated.csv` | `all_relations_aggregated.csv` |
| 网络指标 | `04_graph/network_metrics.csv` | `all_network_metrics.csv` |
| 别名 | `04_graph/entity_aliases.csv` | `all_entity_aliases.csv` |
| 主题 | `05_themes/themes*.csv` | `all_themes*.csv` |
| 主题共现 | `05_themes/theme_pairs.csv` | `all_theme_pairs.csv` |
| 叙事曲线 | `06_narrative/narrative_curve.csv` | `all_narrative_curve.csv` |
| 一站式 JSON | `structured.json` | `all_structured.jsonl` |

### 7.2 全库汇总文件清单

| 文件 | 用途 |
|------|------|
| `all_docs.csv` | 全库索引、质量筛选、任务五剧目列表 |
| `all_relations_aggregated.csv` | **任务二跨剧主表** |
| `all_narrative_curve.csv` | **任务四跨剧对比** |
| `all_dialogues.csv` / `.jsonl` | 大规模语料（体积大） |
| `all_structured.jsonl` | 每剧一行 JSON，快速原型 |

---

## 八、核心字段说明

路径以 **r6 分层**为准。

### 8.1 `01_meta/documents.csv`

| 字段 | 含义 |
|------|------|
| `doc_id` | 全库唯一 ID |
| `collection_name` / `work_code` | 合集与作品编码 |
| `title`、`aliases`、`period_hint`、`genre_hint` | 剧名与元数据 |
| `synopsis`、`doc_tags` | 简介、主题标签（含 LLM） |
| `parse_quality_score`、`parse_quality_label` | 0–1 与 high/medium/low |
| **`analysis_ready`** | **是否建议纳入全库统计** |
| `scene_count` … `dialogue_count` | 规模指标 |
| `structured_json` | 完整 JSON 路径 |

### 8.2 `02_cast/roles.csv`（任务一）

| 字段 | 含义 |
|------|------|
| `role_type_raw` / `role_type_inferred` | 原文行当 vs 推断 |
| `personality_tags`、`speech_style` | LLM/规则画像 |
| `narrative_function` | 主角/配角/功能性（r6） |
| `line_count`、`centrality_hint` | 戏份与网络提示 |

### 8.3 `03_script/dialogues.csv`（全任务基础）

| 字段 | 含义 |
|------|------|
| `line_id`、`scene_id`、`line_no` | 回溯与关联 |
| `speaker`、`cue`、`text` | 说话人、板式、正文 |
| `target`、`emotion_tag`、`speech_act` | 受话人、情绪、言语行为 |
| `emotion_derived_by` | `rule` 或 `llm` |
| `is_key_line` | 是否关键台词 |

### 8.4 `04_graph/relations_aggregated.csv`（任务二主表）

| 字段 | 含义 |
|------|------|
| `source`、`target` | 角色节点 |
| `relation_type` | 对抗/命令/协助等 |
| `weight` | 边权 |
| `derived_by` | `llm_refined` / 启发式等 |
| `evidence` | 文本证据摘要 |

### 8.5 `06_narrative/narrative_curve.csv`（任务四）

`tension_level`、`tension_norm`、`is_climax`、`speech_density` 等，可直接绘图。

### 8.6 `structured.json`

含 `metadata` 与全部子表数组；**任务五**前端一次加载即可实现多视图联动。

---

## 九、赛题五任务数据对照（详细）

### 任务一：行当与年代变化

| 需求 | 数据 | 操作示例 |
|------|------|----------|
| 行当对比 | `all_roles` | 比较 `role_type_raw` vs `role_type_inferred` |
| 唱念做打统计 | `all_performances` | 按 `perform_type` 分组 |
| 跨年代 | `all_docs.period_hint` | 与 roles join 后聚合 |
| 案例 | 《空城计》`02_cast/roles.csv` | 诸葛亮：`role_type_inferred` 老生 |

### 任务二：关系网络与戏种比较

| 需求 | 数据 | 操作示例 |
|------|------|----------|
| 全剧网络图 | `relations_aggregated` | NetworkX：节点=角色，边=type，宽=weight |
| 中心人物 | `network_metrics` | `degree_centrality` 条形图 |
| 分场子图 | `relations` + `scenes` | `scene_index` 过滤 |
| 戏种对比 | `all_docs.genre_hint` | 分组比较网络密度 |
| 待补充 | — | **介数中心性、社群划分**需在分析端计算 |

### 任务三：主题构成与组合

| 需求 | 数据 |
|------|------|
| 单剧构成 | `themes_aggregated` |
| 场次推进 | `themes` + `scenes` 热力图 |
| 共现 | `theme_pairs` 弦图/网络 |
| 主题—角色 | `themes.theme_role_links` |

### 任务四：叙事结构与节奏

| 需求 | 数据 |
|------|------|
| 张力曲线 | `narrative_curve` |
| 高潮场次 | `is_climax`、`narrative_turning_point` |
| 唱白节奏 | `performances` 按场统计 |

### 任务五：综合可视系统

| 模块 | 接口 |
|------|------|
| 剧目列表 | `all_docs.csv`（`analysis_ready` 筛选） |
| 详情页 | `structured.json` 或分层 CSV |
| 联动逻辑 | 选场次 → 过滤 `relations`/`themes` → 点击边 → `dialogues`（`line_no`+`text`） |

---

## 十、数据质量与使用规范

### 10.1 质量过滤（必做）

```python
import pandas as pd

docs = pd.read_csv("opera_output/all_docs.csv")
# 仅《戏考》合集
docs = docs[docs["relative_path"].astype(str).str.startswith("01000000")]

good = docs[docs["analysis_ready"].astype(str).str.lower().isin(["true", "1", "yes"])]
good_ids = set(good["doc_id"])

# 后续 all_relations 等：
# rel = pd.read_csv("opera_output/all_relations_aggregated.csv")
# rel = rel[rel["doc_id"].isin(good_ids)]
```

**low 质量剧（约 20 部）**：多为 PDF 解析失败（场次极少、关系为空），**勿纳入全库结论**。

### 10.2 关系表选用

| 场景 | 表 |
|------|-----|
| 全剧网络图 | `relations_aggregated` |
| 论文证据 | `relations` + `evidence` / `evidence_line_ids` |
| 中心性 | `network_metrics`（或自算介数） |

### 10.3 `derived_by` 含义

| 值 | 来源 |
|----|------|
| `llm` / `llm_refined` | 大模型 |
| `adjacency` / `mention` / `cooccurrence` | 启发式 |
| `keyword` | 主题关键词 |

### 10.4 目录与版本

- 分析统一使用 **`opera_output`**，勿与 `opera_dataset_md` 混用。
- 仅当 `parser_version` 含 `2026-05-16-r6` 且存在 `01_meta/documents.csv` 时视为 r6 标准输出。

---

## 十一、分析脚手架 analysis_starter.py

### 11.1 安装与全库检验

```powershell
python analysis_starter.py --verify --collection 01000000 --data-dir opera_output --out-dir analysis_figures
```

**结果**：`analysis_figures/batch_verify_01000000.csv`

### 11.2 全库质量概览 + 单剧示例图

```powershell
python analysis_starter.py `
  --data-dir opera_output `
  --collection 01000000 `
  --title 空城计 `
  --play-dir opera_output\01000000\01001001_空城计 `
  --out-dir analysis_figures
```

| 输出文件 | 赛题任务 |
|----------|----------|
| `analysis_figures/00_corpus_overview.png` | 全库质量 / `analysis_ready` 分布 |
| `analysis_figures/<剧名>/01_relation_network.png` | **任务二** 人物关系网络 |
| `analysis_figures/<剧名>/02_narrative_curve.png` | **任务四** 叙事张力曲线 |
| `analysis_figures/<剧名>/03_themes.png` | **任务三** 主题条形图 |
| `analysis_figures/<剧名>/04_role_centrality.png` | **任务二** 度中心性 |
| `analysis_figures/<剧名>/play_summary.json` | 报告 / 前端摘要 |

常用参数：`--doc-id`、`--ready-only`、`--min-weight 3`、`--skip-corpus`。

---

## 十二、中断、重启与维护

### 12.1 安全中断

1. 在运行 `python mineru_batch_convert...` 的终端按 **`Ctrl+C`**。
2. 已写完的 `structured.json` 与分层 CSV **保留**；当前批中未完成的一部可能需下次重跑。

### 12.2 重新启动全量（推荐命令）

```powershell
cd D:\University_studies\Junior\data_visualization\experiment\final_self
# 确保 .env 或环境变量已配置

python mineru_batch_convert_structured_llm_final_v8.py `
  --input-dir opera_dataset\01000000 `
  --output-dir opera_output `
  --llm-enabled `
  --chunk-size 50 `
  --collection-prefix 01000000 `
  --manifest opera_output\mineru_manifest_01000000.csv `
  2>&1 | Tee-Object -FilePath opera_output\batch_01000000_run.log -Append
```

- **不要**加 `--no-skip-existing`（除非故意全库重跑）。
- 每批结束会打印 `[AUTO-COMBINE] N plays, analysis_ready=M`。

### 12.3 中断后同步汇总

```powershell
python mineru_batch_convert_structured_llm_final_v8.py `
  --combine-only --output-dir opera_output --collection-prefix 01000000
```

### 12.4 仅重组旧目录（不耗 API）

```powershell
python mineru_batch_convert_structured_llm_final_v8.py --repack-only --output-dir opera_output
python mineru_batch_convert_structured_llm_final_v8.py --combine-only --output-dir opera_output --collection-prefix 01000000
```

---

## 十三、命令速查表

| 命令 / 参数 | 作用 |
|-------------|------|
| `--status-only` | 对比 PDF 与输出进度 |
| `--combine-only` | 从 `structured.json` 重建 `all_*.csv` |
| `--cleanup-legacy` | 删除 r4 扁平重复文件 |
| `--repack-only` | 旧扁平 → r6 分层（不调用 MinerU/LLM） |
| `--limit N` | 仅处理前 N 个 PDF |
| `--no-skip-existing` | 强制重跑（慎用） |
| `--no-auto-combine` | 关闭每批自动汇总 |
| `--collection-prefix 01000000` | 限定汇总范围 |
| `--llm-enabled` | 开启 LLM 全链路增强 |
| `--no-llm-dialogue` | 关闭台词 LLM |
| `--no-llm-relation-refine` | 关闭关系 LLM 校正 |
| `--llm-max-dialogue-lines 100` | 每剧 LLM 台词上限 |

---

## 十四、团队协作与 GitHub

### 14.1 仓库提交范围

| 提交 | 不提交（网盘共享） |
|------|-------------------|
| 脚本、`README.md`、`requirements.txt`、`.env.example` | `opera_dataset/`（PDF） |
| 可选：`samples/` 单剧示例 | 完整 `opera_output/` 或仅分享 `all_*.csv` zip |
| | `.env`、`*.log` |

详见 `data/README.md`。

### 14.2 分工建议

| 角色 | 工作 |
|------|------|
| 数据 | 维护 PDF；跑批处理；定期 `--combine-only` |
| 分析 | `all_*` + `analysis_ready`；NetworkX 图算法 |
| 可视化 | `structured.json` + 五视图联动 |
| 文档 | 赛题答卷、样本量说明 |

### 14.3 初始化 Git

```powershell
git init
git add README.md .gitignore requirements.txt .env.example `
  mineru_batch_convert_structured_llm_final_v8.py analysis_starter.py `
  run_opera_01000000.ps1 京剧剧本结构化数据说明.md data/
git commit -m "docs: 合并京剧结构化流水线完整说明"
```

---

## 十五、赛题满足度评估

| 维度 | 满足度 | 说明 |
|------|--------|------|
| 数据模型（文档+多表+图谱） | **约 90%** | r6 分层目录已规范 |
| 五任务可直接开做 | **是（当前约 58 部 analysis_ready）** | 需过滤低质量 |
| 任务一 行当/唱念做打 | **高** | roles + performances + LLM 行当 |
| 任务二 关系网络 | **中高** | 聚合边 + 度中心性；介数/社群待分析端 |
| 任务三 主题 | **中高** | theme_pairs + theme_role_links |
| 任务四 叙事 | **中高** | narrative_curve 可直接绘图 |
| 任务五 综合系统 | **是** | structured.json + all_docs |
| 全库 448 部 | **进行中** | 完成后刷新 `all_*` |

**建议在分析/notebook 中补充**：介数中心性、社群划分、跨剧统计检验、低质量 PDF 人工修复或排除说明。

---

## 十六、常见问题

**Q：PowerShell 里 `python : [MinerU]...` 红色报错？**  
A：多为 stderr 进度信息被 PowerShell 当成警告；若随后有 `[OK]`、`[AUTO-COMBINE]` 即正常。可用 `*>&1 | Tee-Object` 或减少 `2>&1`。

**Q：`all_docs` 行数为何大于 `r6_ok`？**  
A：`all_*` 扫描所有含 `structured.json` 的目录；`status` 的 `r6_ok` 还校验 CSV 行数与版本。以 `analysis_ready` 与 `parser_version` 为准。

**Q：根目录还有 `dialogues.csv`？**  
A：r4 遗留，与 `03_script/dialogues.csv` 重复且可能更旧。运行 `--cleanup-legacy`。

**Q：批处理中断后 `all_*` 过旧？**  
A：执行 `--combine-only`；新代码每批结束会自动刷新。

**Q：如何只重跑一部？**  
A：`--files opera_dataset\01000000\xxx.pdf --llm-enabled --no-skip-existing`

---

## 附录：《空城计》示范索引

| 问题 | 查表 | 参考 |
|------|------|------|
| 诸葛亮行当？ | `02_cast/roles.csv` | `role_type_inferred` |
| 对话中心？ | `04_graph/network_metrics.csv` | 度中心性 |
| 核心主题？ | `05_themes/themes_aggregated.csv` | 权谋、战争等 |
| 高潮场次？ | `06_narrative/narrative_curve.csv` | `is_climax` |
| 汇报关系？ | `04_graph/relations_aggregated.csv` | 报子→诸葛亮 |

路径：`opera_output/01000000/01001001_空城计/`。优质样本：`analysis_ready=True`，质量分通常 > 0.9。

---

*文档与 `mineru_batch_convert_structured_llm_final_v8.py`（**2026-05-16-r6**）同步；字段以 `write_play_package` / `write_combined_exports` 为准。批处理进度请用 `--status-only` 获取最新数字。*
