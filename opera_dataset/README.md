# 数据目录说明

完整操作步骤、**两阶段试点→全库策略**、流水线说明与赛题映射见项目根目录 **[`README.md`](../README.md)**（§二）。

## 目录约定

```
项目根目录/
├── opera_dataset/              # 【全库】赛题 PDF 根目录（多个合集）
│   ├── 01000000/               # 【试点】《戏考》约 448 部 — 建议先跑通
│   │   └── *.pdf
│   ├── 01001000/               # 其他合集（阶段二）
│   └── …
└── opera_output/
    ├── combined/01000000/      # 试点汇总 all_*.csv
    ├── all_*.csv               # 全库汇总（阶段二完成后）
    └── <合集>/<剧目>/          # 单剧 structured.json + 分层 CSV
```

## 两阶段处理

| 阶段 | 输入 | 汇总表位置 | 脚本 |
|------|------|------------|------|
| 一、试点 | `opera_dataset/01000000` | `opera_output/combined/01000000/` | `run_opera_01000000.ps1` |
| 二、全库 | `opera_dataset/`（递归） | `opera_output/all_*.csv` | `run_opera_full.ps1` |

## 共享与 Git

| 内容 | 建议 |
|------|------|
| `opera_dataset/` 全部 PDF | 网盘共享，勿提交 Git |
| `opera_output/` | 网盘或仅共享 `combined/01000000/` 与根目录 `all_*.csv` |
| 代码仓库 | 仅脚本与文档（见 README §十六） |
