#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""京剧结构化数据可视化入门脚本（赛题分析脚手架）。

读取 opera_output/all_*.csv（或单剧分层 CSV），生成：
  - 全库质量概览
  - 单剧人物关系网络图（任务二）
  - 单剧叙事张力曲线（任务四）
  - 主题构成、角色中心性条形图

依赖：pandas、matplotlib；网络布局可选 networkx。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

try:
    import networkx as nx

    HAS_NETWORKX = True
except ImportError:
    HAS_NETWORKX = False

# 关系类型配色（任务二图例）
RELATION_COLORS = {
    "对抗": "#c0392b",
    "命令": "#8e44ad",
    "汇报": "#2980b9",
    "协助": "#27ae60",
    "评价": "#f39c12",
    "提及": "#7f8c8d",
    "对话": "#95a5a6",
    "同场共现": "#bdc3c7",
    "试探": "#e67e22",
    "劝阻": "#16a085",
    "旁观": "#d5dbdb",
}


def setup_chinese_font() -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def parse_bool_series(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin({"true", "1", "yes"})


def resolve_data_root(path: Path) -> Path:
    path = path.resolve()
    if (path / "all_docs.csv").exists():
        return path
    if (path.parent / "all_docs.csv").exists():
        return path.parent
    raise FileNotFoundError(f"未找到 all_docs.csv，请指定含汇总表的目录（如 opera_output）：{path}")


def pick_doc_row(docs: pd.DataFrame, *, title: str = "", doc_id: str = "") -> pd.Series:
    if doc_id:
        hit = docs[docs["doc_id"] == doc_id]
        if hit.empty:
            raise ValueError(f"未找到 doc_id={doc_id}")
        return hit.iloc[0]
    if title:
        hit = docs[docs["title"].astype(str).str.contains(title, na=False)]
        if hit.empty:
            hit = docs[docs["work_title_hint"].astype(str).str.contains(title, na=False)]
        if hit.empty:
            raise ValueError(f"未找到剧名包含「{title}」的剧目")
        return hit.sort_values("parse_quality_score", ascending=False).iloc[0]
    ready = docs[parse_bool_series(docs.get("analysis_ready", False))]
    pool = ready if len(ready) else docs
    return pool.sort_values("parse_quality_score", ascending=False).iloc[0]


def load_csv(data_root: Path, name: str) -> pd.DataFrame:
    path = data_root / name
    if not path.exists():
        raise FileNotFoundError(f"缺少 {path}，请先运行 combine-only 生成全库表")
    return pd.read_csv(path, encoding="utf-8")


def filter_doc(df: pd.DataFrame, doc_id: str) -> pd.DataFrame:
    if df.empty or "doc_id" not in df.columns:
        return df
    return df[df["doc_id"] == doc_id].copy()


def load_play_from_layers(play_root: Path, doc_id: str) -> dict[str, pd.DataFrame]:
    """从单剧分层目录读取（优先于 all_*）。"""
    layout = {
        "relations_agg": play_root / "04_graph" / "relations_aggregated.csv",
        "narrative": play_root / "06_narrative" / "narrative_curve.csv",
        "roles": play_root / "02_cast" / "roles.csv",
        "themes_agg": play_root / "05_themes" / "themes_aggregated.csv",
        "network": play_root / "04_graph" / "network_metrics.csv",
    }
    out: dict[str, pd.DataFrame] = {}
    for key, p in layout.items():
        if p.exists():
            out[key] = pd.read_csv(p, encoding="utf-8")
        else:
            out[key] = pd.DataFrame()
    return out


def plot_corpus_overview(docs: pd.DataFrame, out_dir: Path) -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    ready = parse_bool_series(docs.get("analysis_ready", False))
    labels = docs["parse_quality_label"].fillna("unknown").value_counts()
    axes[0].bar(labels.index.astype(str), labels.values, color=["#2ecc71", "#f1c40f", "#e74c3c"][: len(labels)])
    axes[0].set_title("解析质量分布")
    axes[0].set_ylabel("剧目数")

    genre = docs["genre_hint"].fillna("未知").str.split("/").str[0].value_counts().head(8)
    axes[1].barh(genre.index[::-1], genre.values[::-1], color="#3498db")
    axes[1].set_title("戏种（前缀）Top8")

    axes[2].pie(
        [ready.sum(), (~ready).sum()],
        labels=[f"可分析 ({ready.sum()})", f"其余 ({(~ready).sum()})"],
        autopct="%1.0f%%",
        colors=["#27ae60", "#ecf0f1"],
    )
    axes[2].set_title("analysis_ready")

    fig.suptitle(f"全库概览（共 {len(docs)} 部）", fontsize=14)
    fig.tight_layout()
    path = out_dir / "00_corpus_overview.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_relation_network(
    edges: pd.DataFrame,
    roles: pd.DataFrame,
    title: str,
    out_dir: Path,
    *,
    min_weight: int = 2,
    max_edges: int = 40,
) -> Path | None:
    if edges.empty:
        print("[WARN] 关系边为空，跳过网络图")
        return None

    e = edges.copy()
    e["weight"] = pd.to_numeric(e["weight"], errors="coerce").fillna(1).astype(int)
    e = e[e["weight"] >= min_weight].sort_values("weight", ascending=False).head(max_edges)
    if e.empty:
        print("[WARN] 过滤后无边，降低 --min-weight 重试")
        return None

    if not HAS_NETWORKX:
        print("[WARN] 未安装 networkx，跳过网络图（pip install networkx）")
        return None

    g = nx.DiGraph()
    centrality = {}
    if not roles.empty and "role_name" in roles.columns:
        for _, r in roles.iterrows():
            centrality[r["role_name"]] = float(r.get("centrality_hint", 0.1) or 0.1)

    for _, row in e.iterrows():
        s, t = str(row["source"]), str(row["target"])
        if not s or not t or s == t:
            continue
        g.add_edge(
            s,
            t,
            weight=int(row["weight"]),
            relation=str(row.get("relation_type", "")),
            label=str(row.get("relation_type", ""))[:4],
        )

    if g.number_of_nodes() == 0:
        return None

    fig, ax = plt.subplots(figsize=(11, 9))
    pos = nx.spring_layout(g, seed=42, k=1.8 / max(len(g.nodes), 1))
    node_sizes = [400 + 2200 * centrality.get(n, 0.15) for n in g.nodes()]
    nx.draw_networkx_nodes(g, pos, node_size=node_sizes, node_color="#ecf0f1", edgecolors="#2c3e50", ax=ax)
    nx.draw_networkx_labels(g, pos, font_size=9, ax=ax)

    for u, v, data in g.edges(data=True):
        rel = data.get("relation", "提及")
        color = RELATION_COLORS.get(rel, "#7f8c8d")
        nx.draw_networkx_edges(
            g,
            pos,
            edgelist=[(u, v)],
            width=0.8 + 0.35 * data.get("weight", 1),
            edge_color=color,
            arrows=True,
            arrowsize=14,
            connectionstyle="arc3,rad=0.12",
            ax=ax,
        )
        mid = ((pos[u][0] + pos[v][0]) / 2, (pos[u][1] + pos[v][1]) / 2)
        ax.text(mid[0], mid[1], data.get("label", ""), fontsize=7, ha="center", color=color)

    ax.set_title(f"《{title}》人物关系网络（任务二）\n边宽≈weight，颜色=关系类型")
    ax.axis("off")
    path = out_dir / "01_relation_network.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_narrative_curve(curve: pd.DataFrame, title: str, out_dir: Path) -> Path | None:
    if curve.empty:
        print("[WARN] narrative_curve 为空")
        return None

    c = curve.sort_values("scene_index")
    x = c["scene_index"].astype(int)
    tension = pd.to_numeric(c.get("tension_norm", c.get("tension_level", 0)), errors="coerce").fillna(0)
    speech = pd.to_numeric(c.get("speech_density", 0), errors="coerce").fillna(0)

    fig, ax1 = plt.subplots(figsize=(11, 5))
    ax1.plot(x, tension, marker="o", color="#c0392b", linewidth=2, label="张力(归一化)")
    climax = c[pd.to_numeric(c.get("is_climax", 0), errors="coerce").fillna(0) > 0]
    if not climax.empty:
        ax1.scatter(
            climax["scene_index"],
            pd.to_numeric(climax.get("tension_norm", climax.get("tension_level", 0)), errors="coerce"),
            s=120,
            c="#f1c40f",
            edgecolors="#c0392b",
            zorder=5,
            label="高潮场",
        )
    ax1.set_xlabel("场次序号")
    ax1.set_ylabel("张力")
    ax1.set_title(f"《{title}》叙事张力曲线（任务四）")
    ax1.grid(alpha=0.3)

    ax2 = ax1.twinx()
    ax2.bar(x, speech, alpha=0.25, color="#3498db", label="话语密度")
    ax2.set_ylabel("话语密度")

    labels = [str(s)[:10] for s in c.get("scene", x)]
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, rotation=35, ha="right", fontsize=8)

    lines1, lab1 = ax1.get_legend_handles_labels()
    lines2, lab2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, lab1 + lab2, loc="upper left")

    fig.tight_layout()
    path = out_dir / "02_narrative_curve.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_themes_and_roles(
    themes: pd.DataFrame,
    network: pd.DataFrame,
    title: str,
    out_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    if not themes.empty and "theme_label" in themes.columns:
        t = themes.sort_values("weight", ascending=True).tail(12)
        fig, ax = plt.subplots(figsize=(8, max(4, 0.35 * len(t))))
        ax.barh(t["theme_label"], pd.to_numeric(t["weight"], errors="coerce").fillna(1), color="#9b59b6")
        ax.set_title(f"《{title}》主题构成（任务三）")
        fig.tight_layout()
        p = out_dir / "03_themes.png"
        fig.savefig(p, dpi=150, bbox_inches="tight")
        plt.close(fig)
        paths.append(p)

    if not network.empty and "character" in network.columns:
        n = network.sort_values("degree_centrality", ascending=True).tail(10)
        fig, ax = plt.subplots(figsize=(8, max(4, 0.35 * len(n))))
        ax.barh(n["character"], pd.to_numeric(n["degree_centrality"], errors="coerce").fillna(0), color="#16a085")
        ax.set_title(f"《{title}》角色度中心性（任务二）")
        ax.set_xlabel("degree_centrality")
        fig.tight_layout()
        p = out_dir / "04_role_centrality.png"
        fig.savefig(p, dpi=150, bbox_inches="tight")
        plt.close(fig)
        paths.append(p)
    return paths


def run_batch_verify(docs: pd.DataFrame, data_root: Path, out_dir: Path, collection: str) -> Path:
    """生成合集批处理质量检验表。"""
    if collection:
        mask = docs["relative_path"].astype(str).str.replace("\\", "/").str.startswith(collection)
        docs = docs[mask].copy()
    rows = []
    for _, doc in docs.iterrows():
        doc_id = str(doc.get("doc_id", ""))
        rel = str(doc.get("relative_path", ""))
        play_name = Path(rel).name if rel else str(doc.get("title", ""))
        play_root = data_root / Path(rel.replace("\\", "/")) if rel else data_root / play_name
        layered_ok = (play_root / "01_meta" / "documents.csv").exists()
        struct_ok = (play_root / "structured.json").exists()
        rows.append({
            "title": doc.get("title", ""),
            "relative_path": rel,
            "doc_id": doc_id,
            "parser_version": doc.get("parser_version", ""),
            "parse_quality_score": doc.get("parse_quality_score", 0),
            "parse_quality_label": doc.get("parse_quality_label", ""),
            "analysis_ready": doc.get("analysis_ready", False),
            "scene_count": doc.get("scene_count", 0),
            "dialogue_count": doc.get("dialogue_count", 0),
            "relation_count": doc.get("relation_count", 0),
            "layered_layout": layered_ok,
            "structured_json": struct_ok,
            "llm_enabled": doc.get("llm_enabled", ""),
        })
    report = pd.DataFrame(rows).sort_values(["analysis_ready", "parse_quality_score"], ascending=[False, False])
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{collection}" if collection else ""
    path = out_dir / f"batch_verify{suffix}.csv"
    report.to_csv(path, index=False, encoding="utf-8-sig")
    ready_n = parse_bool_series(report["analysis_ready"]).sum() if "analysis_ready" in report.columns else 0
    high = (report["parse_quality_label"] == "high").sum() if "parse_quality_label" in report.columns else 0
    low = (report["parse_quality_label"] == "low").sum() if "parse_quality_label" in report.columns else 0
    print(f"[VERIFY] plays={len(report)}  analysis_ready={ready_n}  high={high}  low={low}")
    print(f"[VERIFY] report -> {path}")
    return path


def export_play_summary(
    doc_row: pd.Series,
    edges: pd.DataFrame,
    curve: pd.DataFrame,
    out_dir: Path,
) -> Path:
    summary = {
        "doc_id": doc_row.get("doc_id", ""),
        "title": doc_row.get("title", ""),
        "parse_quality_score": float(doc_row.get("parse_quality_score", 0) or 0),
        "analysis_ready": bool(parse_bool_series(pd.Series([doc_row.get("analysis_ready", False)])).iloc[0]),
        "genre_hint": doc_row.get("genre_hint", ""),
        "synopsis": doc_row.get("synopsis", ""),
        "edge_count": int(len(edges)),
        "scene_count": int(len(curve)),
        "top_relations": edges.head(8)[["source", "target", "relation_type", "weight"]].to_dict(orient="records")
        if not edges.empty
        else [],
    }
    path = out_dir / "play_summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="京剧结构化数据可视化脚手架")
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path("opera_output"),
        help="含 all_*.csv 的目录（默认 opera_output）",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("analysis_figures"),
        help="图表输出目录",
    )
    p.add_argument("--title", type=str, default="", help="剧目名（模糊匹配），默认取质量最高且 analysis_ready")
    p.add_argument("--doc-id", type=str, default="", help="指定 doc_id")
    p.add_argument("--play-dir", type=Path, default=None, help="单剧目录（优先读分层 CSV）")
    p.add_argument("--ready-only", action="store_true", help="全库概览时仅统计 analysis_ready")
    p.add_argument("--min-weight", type=int, default=2, help="网络图最小边权重")
    p.add_argument("--skip-corpus", action="store_true", help="不生成全库概览图")
    p.add_argument(
        "--verify",
        action="store_true",
        help="仅输出批处理质量检验 CSV（不画单剧图）",
    )
    p.add_argument(
        "--collection",
        type=str,
        default="",
        help="按 relative_path 前缀筛选合集，如 01000000",
    )
    return p


def main() -> int:
    args = build_argparser().parse_args()
    setup_chinese_font()

    try:
        data_root = resolve_data_root(args.data_dir)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    docs = load_csv(data_root, "all_docs.csv")
    if args.collection:
        docs = docs[docs["relative_path"].astype(str).str.replace("\\", "/").str.startswith(args.collection)].copy()

    if args.verify:
        run_batch_verify(docs, data_root, out_dir, args.collection)
        return 0

    if args.ready_only:
        docs = docs[parse_bool_series(docs.get("analysis_ready", False))]

    if not args.skip_corpus:
        p = plot_corpus_overview(docs, out_dir)
        print(f"[OK] 全库概览 -> {p}")

    try:
        doc_row = pick_doc_row(docs, title=args.title, doc_id=args.doc_id)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    doc_id = str(doc_row["doc_id"])
    title = str(doc_row.get("title", doc_id))
    play_out = out_dir / title.replace("/", "_")
    play_out.mkdir(parents=True, exist_ok=True)

    if args.play_dir and args.play_dir.exists():
        layered = load_play_from_layers(args.play_dir.resolve(), doc_id)
        edges = layered.get("relations_agg", pd.DataFrame())
        curve = layered.get("narrative", pd.DataFrame())
        roles = layered.get("roles", pd.DataFrame())
        themes = layered.get("themes_agg", pd.DataFrame())
        network = layered.get("network", pd.DataFrame())
    else:
        edges = filter_doc(load_csv(data_root, "all_relations_aggregated.csv"), doc_id)
        curve = filter_doc(load_csv(data_root, "all_narrative_curve.csv"), doc_id)
        roles = filter_doc(load_csv(data_root, "all_roles.csv"), doc_id)
        themes = filter_doc(load_csv(data_root, "all_themes_aggregated.csv"), doc_id)
        network = filter_doc(load_csv(data_root, "all_network_metrics.csv"), doc_id)

    print(f"[PLAY] {title} ({doc_id})  quality={doc_row.get('parse_quality_score')}  edges={len(edges)}")

    for name, fn, extra in [
        ("网络图", plot_relation_network, {"min_weight": args.min_weight}),
        ("叙事曲线", plot_narrative_curve, {}),
    ]:
        if name == "网络图":
            path = fn(edges, roles, title, play_out, **extra)
        else:
            path = fn(curve, title, play_out, **extra)
        if path:
            print(f"[OK] {name} -> {path}")

    for p in plot_themes_and_roles(themes, network, title, play_out):
        print(f"[OK] 附图 -> {p}")

    summary_path = export_play_summary(doc_row, edges, curve, play_out)
    print(f"[OK] 摘要 JSON -> {summary_path}")
    print(f"\n完成。单剧图表目录：{play_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
