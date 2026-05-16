# 京剧剧本数据可视化 · 结构化流水线

赛题 **I：京剧数据可视分析与人文创意** 的数据预处理与可视化脚手架。  
将 PDF 剧本经 [MinerU](https://mineru.net/) 解析 + 规则/LLM 结构化，输出可建图、可回溯的多层 CSV / JSON，供五类分析任务使用。

| 项目 | 说明 |
|------|------|
| 主流水线 | `mineru_batch_convert_structured_llm_final_v8.py` |
| 解析器版本 | `2026-05-16-r6` |
| 数据说明 | [`京剧剧本结构化数据说明.md`](京剧剧本结构化数据说明.md) |
| 分析示例 | `analysis_starter.py` |

---

## 仓库应包含什么（精简清单）

### 必须上传（代码 + 文档，约 1 MB）

```
.
├── README.md                                      # 本文件
├── .gitignore
├── requirements.txt
├── .env.example                                   # 密钥模板（勿提交真实 .env）
├── mineru_batch_convert_structured_llm_final_v8.py
├── analysis_starter.py
├── run_opera_01000000.ps1                         # 《戏考》批处理辅助
├── 京剧剧本结构化数据说明.md
└── opera_dataset/README.md                                 # 大数据获取说明
```


---

## 快速开始

### 1. 环境

```powershell
cd <项目根目录>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. API 密钥

复制 `.env.example` 为 `.env` 并填写：

- `MINERU_TOKEN` — MinerU 批量解析
- `DEEPSEEK_API_KEY` — LLM 增强（`--llm-enabled` 时需要）

或在 PowerShell 中临时设置：

```powershell
$env:MINERU_TOKEN = "..."
$env:DEEPSEEK_API_KEY = "..."
```

### 3. 放置数据

将赛题 PDF 放入：

```
opera_dataset/01000000/*.pdf
```

### 4. 批处理（《戏考》合集）

```powershell
# 查看进度（不耗 API）
python mineru_batch_convert_structured_llm_final_v8.py `
  --status-only `
  --input-dir opera_dataset\01000000 `
  --output-dir opera_output

# 冒烟 2 部
python mineru_batch_convert_structured_llm_final_v8.py `
  --input-dir opera_dataset\01000000 `
  --output-dir opera_output `
  --llm-enabled --limit 2

# 全量（448 部，耗时长）
python mineru_batch_convert_structured_llm_final_v8.py `
  --input-dir opera_dataset\01000000 `
  --output-dir opera_output `
  --llm-enabled --chunk-size 50 `
  --manifest opera_output\mineru_manifest_01000000.csv
```

也可使用：`.\run_opera_01000000.ps1`（内含分步注释）。

中断后仅刷新全库汇总：

```powershell
python mineru_batch_convert_structured_llm_final_v8.py `
  --combine-only --output-dir opera_output --collection-prefix 01000000
```

### 5. 可视化分析

```powershell
pip install pandas matplotlib networkx

python analysis_starter.py --verify --collection 01000000 --data-dir opera_output

python analysis_starter.py --data-dir opera_output --title 空城计 `
  --play-dir opera_output/01000000/01001001_空城计
```

图表输出在 `analysis_figures/`。

---

## 输出结构（r6）

单剧目录示例：

```
opera_output/01000000/01001001_空城计/
├── structured.json
├── 01_meta/documents.csv
├── 02_cast/roles.csv
├── 03_script/{scenes,dialogues,performances}.csv
├── 04_graph/{relations,relations_aggregated,network_metrics,entity_aliases}.csv
├── 05_themes/{themes,themes_aggregated,theme_pairs}.csv
├── 06_narrative/narrative_curve.csv
└── audit/ …
```

全库汇总：`opera_output/all_docs.csv`、`all_relations_aggregated.csv` 等。  
字段与赛题任务映射见 [`京剧剧本结构化数据说明.md`](京剧剧本结构化数据说明.md)。

---

## 队友协作分工建议

| 角色 | 工作内容 |
|------|----------|
| 数据 | 维护网盘 PDF；跑批处理 / `--combine-only`；更新 `all_*.csv` |
| 分析 | 基于 `all_*` + `analysis_ready` 做统计与 NetworkX 图算法 |
| 可视化 | 消费 `structured.json` 或 CSV，前端五视图联动 |
| 文档 | 赛题答卷、数据说明、样例图 |

分析时建议过滤：`analysis_ready == True`（质量分、场次、台词数达标）。

---

## 常用命令速查

| 命令 | 作用 |
|------|------|
| `--status-only` | 对比 PDF 与已完成剧目 |
| `--combine-only` | 从已有 `structured.json` 重建 `all_*.csv` |
| `--repack-only` | 旧扁平目录 → r6 分层 |
| `--limit N` | 只处理前 N 个 PDF（冒烟） |
| `--no-skip-existing` | 强制重跑（慎用全库） |

---

## 推送到 GitHub

```powershell
git init
git add README.md .gitignore requirements.txt .env.example `
  mineru_batch_convert_structured_llm_final_v8.py analysis_starter.py `
  run_opera_01000000.ps1 京剧剧本结构化数据说明.md data/
git commit -m "Initial commit: Jingju structured pipeline r6"
git remote add origin https://github.com/<org>/<repo>.git
git push -u origin main
```

大数据请单独分享链接，写在仓库 **Description** 或 `data/README.md` 中。

---

## 许可与数据

赛题 PDF 仅供课程/竞赛使用，请勿公开传播未授权版权文本。API 密钥仅个人/团队本地使用，禁止写入仓库。
