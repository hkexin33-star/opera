# 数据目录说明

完整操作步骤、流水线阶段说明与赛题映射见项目根目录 **[`README.md`](../README.md)**。

本仓库 **不包含** 大体量 PDF 与完整结构化输出，请按下列方式与队友同步。

## 目录约定（克隆后本地自建）

```
项目根目录/
├── opera_dataset/          # 输入：赛题 PDF
│   └── 01000000/           # 《戏考》合集（约 448 部）
│       └── *.pdf
└── opera_output/           # 输出：流水线生成
    ├── all_*.csv           # 全库汇总（分析主入口）
    └── 01000000/<剧目>/    # 单剧分层目录
```

## 推荐共享方式

| 内容 | 体积（约） | 建议 |
|------|-----------|------|
| `opera_dataset/` 全部 PDF | ~616 MB | 百度网盘 / 校内盘 / Release 附件，**勿提交 Git** |
| `opera_output/` 全量结构化 | ~120 MB+ | 同上；或只共享 `all_*.csv`（~47 MB） |
| 示例单剧 | ~2 MB | 可放 `samples/` 进仓库（见根目录 README） |

## 队友本地最小可运行

1. 克隆本仓库并 `pip install -r requirements.txt`
2. 配置 `.env`（复制 `.env.example`）
3. 将 PDF 放入 `opera_dataset/01000000/`
4. 运行 `run_opera_01000000.ps1` 或 README 中的批处理命令
5. 若已有队友分享的 `opera_output/`，解压到项目根目录即可直接跑 `analysis_starter.py`
