#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch convert PDFs with MinerU, then structure Jingju scripts for analysis.

This refactor keeps the pipeline focused on the赛题真正需要的结构层：
1) 文档总表 documents.csv
2) 角色表 roles.csv
3) 场次表 scenes.csv
4) 台词表 dialogues.csv
5) 关系表 relations.csv
6) 主题表 themes.csv
7) structured.json / structured.jsonl

Compared with the original script, this version:
- outputs competition-ready layered tables (documents / roles / scenes / dialogues /
  performances / relations / themes + derived analytics tables);
- adds stable IDs, entity alias normalization, parse quality scoring, theme co-occurrence,
  narrative curves, and network centrality metrics;
- adds optional LLM enrichment for roles / scenes / relations / themes;
- deletes transient MinerU zip/extract artifacts after parsing;
- optionally keeps the cleaned Markdown for auditability.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import os
import re
import shutil
import sys
import time
import zipfile
from copy import deepcopy
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://mineru.net"
UPLOAD_ENDPOINT = f"{API_BASE}/api/v4/file-urls/batch"
RESULT_ENDPOINT = f"{API_BASE}/api/v4/extract-results/batch"

PARSER_VERSION = "2026-05-16-r6"

ROLE_SECTION_MARKERS = ("主要角色", "角色表", "剧中人")
ROLE_SECTION_END_MARKERS = ("情节", "注释", "第一场", "【第一场】")
SCENE_MARKER_RE = re.compile(r"^\s*【?\s*第\s*[一二三四五六七八九十百零〇0-9]+\s*场\s*】?\s*$")
SCENE_BODY_RE = re.compile(r"第\s*[一二三四五六七八九十百零〇0-9]+\s*场")
PAGE_HEADER_RE = re.compile(r"^\s*中国京剧戏考\b.*$")
PAGE_FOOTER_URL_RE = re.compile(r"^\s*https?://scripts\.xikao\.com/play/\d+\s*$")
TCPDF_RE = re.compile(r"^\s*Powered by TCPDF \(www\.tcpdf\.org\)\s*$")
STANDALONE_PAGE_NO_RE = re.compile(r"^\s*\d+\s*$")
BOILERPLATE_RE = re.compile(r"^\s*根据《戏考》第一册整理\s*$")
MD_HEADING_PREFIX_RE = re.compile(r"^\s*#{1,6}\s*")
HTML_TAG_RE = re.compile(r"<[^>]+>")

SPEAKER_LINE_RE = re.compile(
    r"^\s*(?P<speaker>[\u4e00-\u9fffA-Za-z0-9·]{1,12})"
    r"(?:\s*(?:：|:)\s*|\s*[（(](?P<cue>[^）)]{1,32})[）)])"
    r"\s*(?P<text>.*)$"
)
PAREN_ONLY_RE = re.compile(r"^\s*[（(](?P<cue>[^）)]{1,80})[）)]\s*$")
PAREN_FRONT_RE = re.compile(r"^\s*[（(](?P<cue>[^）)]{1,80})[）)]\s*(?P<text>.*)$")

COMMON_STAGE_SPEAKERS = {
    "童儿", "旗牌", "报子", "龙套", "众人", "老军甲", "老军乙",
    "二老军", "二童儿", "四上手", "四龙套", "四白龙套", "四上手引赵云",
    "四上手引赵云急急风过场", "军士", "差役", "家人", "院子", "随从", "侍从",
}

# Lines like「启禀丞相：……」are report speech, not character names.
PSEUDO_SPEAKER_PREFIX_RE = re.compile(r"^(启禀|回禀|禀报|报与|参见|谢)(.+)?$")
REPORT_SPEAKER_LINE_RE = re.compile(
    r"^(?P<prefix>启禀|回禀|禀报)(?P<title>丞相|将军|元帅|陛下|主公|大人|千岁)?[：:]\s*(?P<text>.+)$"
)
PLACEHOLDER_DIALOGUE_RE = re.compile(
    r"^[\u4e00-\u9fffA-Za-z0-9·]{1,12}\s*[（(][^）)]{1,20}[）)]\s*$"
)
HTML_TABLE_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
HTML_TABLE_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)

ROLE_GUESS_KEYWORDS = {
    "生": ["男", "将", "官", "王", "帝", "帅", "相", "爷", "公", "老", "大臣", "书生", "武生", "净", "丑"],
    "旦": ["女", "夫人", "小姐", "娘", "妻", "妃", "婆", "尼姑", "姑娘", "花旦", "青衣", "老旦"],
    "净": ["勇", "猛", "粗", "刚", "烈", "威", "霸", "豪", "奸", "恶", "武", "将", "黑"],
    "丑": ["滑", "笑", "诙", "小", "仆", "童", "差", "杂", "老军", "报子", "旗牌", "家人"],
}

THEME_KEYWORDS = {
    "忠义": ["忠", "义", "报国", "扶汉", "尽忠"],
    "权谋": ["计", "谋", "诈", "权", "虚实", "兵法"],
    "战争": ["兵", "战", "军", "阵", "攻", "守", "城"],
    "家国": ["国", "家", "朝", "社稷", "天下"],
    "亲情": ["父", "母", "子", "女", "兄", "弟", "妻", "夫"],
    "公案": ["案", "审", "官", "犯", "冤", "狱"],
    "喜剧": ["笑", "逗", "滑", "闹", "趣", "诙"],
    "情爱": ["爱", "情", "婚", "媒", "姻", "恋"],
    "离散": ["别", "离", "散", "流亡", "逃", "走"],
}

# 用户提供的数据集集合编码与名称映射：
# 目录中通常会出现诸如 opera_dataset/01000000/01001001_空城计.pdf 这样的结构。
# 下面的映射会将第一层数字目录归入对应“集合”名称，方便统计、分组、可视化和导出。
COLLECTION_NAME_MAP = {
    "1000000": "《戏考》",
    "2000000": "《国剧大成》",
    "3000000": "《京剧汇编》",
    "4000000": "《京剧丛刊》",
    "5000000": "《传统剧目汇编》",
    "7000000": "《中国传统戏曲剧本选集》",
    "8000000": "《京剧集成》",
    "9000000": "《京剧流派剧目荟萃》",
    "10000000": "《戏考大全》",
    "11000000": "《传统戏曲剧目资料汇编》",
    "13000000": "《剧学月刊》",
    "14000000": "《戏典》",
    "15000000": "《大众戏曲丛书》",
    "70001000": "《周信芳演出剧本选集》",
    "70002000": "《马连良演出剧本选集》",
    "70003000": "《关羽戏集：李洪春演出本》",
    "70004000": "《唐韵笙舞台艺术集》",
    "70005000": "《汪笑侬戏曲集》",
    "70006000": "《余派戏词钱氏辑粹》",
    "70201000": "《梅兰芳演出剧本选集》",
    "70202000": "《程砚秋演出剧本选集》",
    "70203000": "《荀慧生演出剧本选集》",
    "70204000": "《欧阳予倩文集》",
    "70401000": "《郝寿臣演出剧本选集》",
    "70402000": "《方荣翔戏剧集》",
    "70601000": "《萧长华演出剧本选集》",
    "70801000": "《田汉全集》",
    "70802000": "《老舍剧作全集》",
    "70803000": "《范钧宏戏曲选》",
    "70804000": "《范钧宏、吕瑞明戏曲选》",
    "70805000": "《翁偶虹剧作选》",
    "70901000": "《侯玉山昆曲谱》",
    "70902000": "《振飞曲谱》",
    "70903000": "《大武生：侯少奎昆曲五十年》",
    "70904000": "《马祥麟演出剧目集》",
    "80000000": "根据录音记录本",
    "90000000": "单行本",
    "94000000": "院团改编本、演出本",
}


def normalize_collection_code(code: str) -> str:
    digits = re.sub(r"\D", "", code or "")
    digits = digits.lstrip("0")
    return digits or "0"


def infer_collection_info(rel_path: Path | str) -> dict:
    path = Path(rel_path)
    parts = list(path.parts)
    collection_dir = ""
    for part in parts[:-1]:
        if re.fullmatch(r"\d{6,8}", part):
            collection_dir = part
            break
    if not collection_dir:
        for part in parts:
            m = re.search(r"(\d{6,8})", part)
            if m:
                collection_dir = m.group(1)
                break

    collection_code = normalize_collection_code(collection_dir)
    collection_name = COLLECTION_NAME_MAP.get(collection_code, collection_dir or "未知集合")
    work_stem = path.stem
    work_code = ""
    work_title_hint = ""
    m = re.match(r"^(?P<code>\d{6,8})_(?P<title>.+)$", work_stem)
    if m:
        work_code = m.group("code")
        work_title_hint = m.group("title").strip()
    else:
        m = re.match(r"^(?P<code>\d{6,8})(?P<title>.+)$", work_stem)
        if m:
            work_code = m.group("code")
            work_title_hint = m.group("title").strip(" _-")
        else:
            work_title_hint = work_stem

    return {
        "collection_dir": collection_dir,
        "collection_code": collection_code,
        "collection_name": collection_name,
        "collection_label": f"{collection_name}（{collection_code}）" if collection_name else collection_code,
        "work_code": work_code,
        "work_title_hint": work_title_hint,
    }


@dataclass
class MinerUConfig:
    token: str
    model_version: str = "vlm"
    language: str = "ch"
    wait_seconds: int = 15
    timeout_minutes: int = 120
    chunk_size: int = 50
    recursive: bool = True
    clean_md: bool = True
    skip_existing: bool = True
    trust_env: bool = False
    keep_md: bool = False
    keep_transient: bool = False
    structured_only: bool = False


@dataclass
class LLMConfig:
    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    timeout_seconds: int = 90
    max_tokens: int = 2000
    max_input_chars: int = 8000
    max_scene_lines: int = 120
    max_role_evidence: int = 14
    enrich_dialogues: bool = True
    enrich_relations: bool = True
    max_dialogue_llm_lines: int = 100
    dialogue_batch_size: int = 18
    max_relation_refine_pairs: int = 48
    roles_batch_size: int = 8


# -------------------------
# Generic helpers
# -------------------------


def log_info(msg: str) -> None:
    """Progress messages to stdout (avoid PowerShell treating stderr as errors)."""
    print(msg, flush=True)


def log_warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_local_env(env_path: Path | None = None) -> bool:
    """Load KEY=VALUE lines from .env into os.environ (only if not already set)."""
    if env_path is None:
        env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return False
    loaded = 0
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    if loaded:
        print(f"[ENV] loaded {loaded} keys from {env_path}")
    return loaded > 0


def iter_files(input_dir: Path | None, files: Sequence[Path] | None, recursive: bool = True) -> list[Path]:
    if files:
        return [p.resolve() for p in files]
    if not input_dir:
        raise ValueError("Either input_dir or files must be provided.")
    input_dir = input_dir.resolve()
    if recursive:
        return sorted(p.resolve() for p in input_dir.rglob("*.pdf"))
    return sorted(p.resolve() for p in input_dir.iterdir() if p.suffix.lower() == ".pdf")


def chunked(seq: Sequence, size: int) -> Iterable[Sequence]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def build_session(trust_env: bool = False) -> requests.Session:
    session = requests.Session()
    session.trust_env = trust_env
    retries = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PUT"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: dict,
    json_payload: dict | None = None,
    timeout: int = 60,
) -> dict:
    last_err = None
    for _ in range(2):
        try:
            resp = session.request(method, url, headers=headers, json=json_payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            last_err = e
            time.sleep(2)
    raise last_err  # type: ignore[misc]


def common_input_root(paths: Sequence[Path]) -> Path:
    if not paths:
        raise ValueError("No PDF files found.")
    parents = [str(p.parent) for p in paths]
    return Path(os.path.commonpath(parents))


def relative_pdf_path(pdf_path: Path, input_root: Path | None) -> Path:
    if input_root is not None:
        try:
            rel = pdf_path.relative_to(input_root)
            # 当 --input-dir 指向合集目录（如 opera_dataset/01000000）时，
            # 输出仍写入 opera_output/01000000/<剧目>/，与全库目录结构一致。
            if re.fullmatch(r"\d{6,8}", input_root.name) and str(rel.parent) in {".", ""}:
                return Path(input_root.name) / rel.name
            return rel
        except ValueError:
            pass
    return Path(pdf_path.name)


def play_relative_path(rel_path: Path) -> str:
    """目录式相对路径（无 .pdf 后缀），用于 documents.relative_path。"""
    if rel_path.suffix.lower() == ".pdf":
        return (rel_path.parent / rel_path.stem).as_posix()
    return rel_path.as_posix()


def stable_data_id(rel_path: Path) -> str:
    stem = rel_path.with_suffix("").as_posix()
    stem = re.sub(r"[^0-9A-Za-z._/-]+", "_", stem)
    digest = hashlib.sha1(rel_path.as_posix().encode("utf-8")).hexdigest()[:10]
    return f"pdf_{stem.replace('/', '__')}_{digest}"[:120]


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def merge_collection_row(row: dict, collection_meta: dict) -> dict:
    """Merge per-row data with document-level metadata without duplicate keys."""
    merged = dict(collection_meta)
    merged.update(row)
    return merged


def normalize_genre_hint(raw: str) -> str:
    text = normalize_text(raw)
    if not text:
        return ""
    for canonical, keys in GENRE_CANONICAL_RULES:
        if text == canonical or any(k in text for k in keys):
            return canonical
    return text[:16]


def attach_evidence_line_ids(relations: list[dict], dialogues: list[dict]) -> None:
    """Link relation evidence snippets back to dialogue line_id when possible."""
    index: dict[str, list[str]] = defaultdict(list)
    for row in dialogues:
        line_id = row.get("line_id", "")
        text = strip_html_markup(row.get("text", ""))
        if not line_id or not text:
            continue
        for length in (24, 16, 12):
            index[text[:length]].append(line_id)
    for rel in relations:
        evidence = strip_html_markup(rel.get("evidence", ""))
        if not evidence:
            continue
        matched: list[str] = []
        for length in (40, 24, 16):
            key = evidence[:length]
            if key in index:
                matched = index[key]
                break
        if not matched:
            for row in dialogues:
                text = strip_html_markup(row.get("text", ""))
                if text and (evidence in text or text in evidence):
                    lid = row.get("line_id", "")
                    if lid:
                        matched.append(lid)
                    break
        rel["evidence_line_ids"] = "；".join(dict.fromkeys(matched))


def enrich_theme_role_links(theme_rows: list[dict], scenes: list[dict]) -> None:
    scene_chars: dict[int, set[str]] = {}
    for scene in scenes:
        idx = int(scene.get("scene_index", 0) or 0)
        chars = set(_llm_items(scene.get("key_characters", ""))) | set(
            _llm_items(scene.get("characters_present", ""))
        )
        scene_chars[idx] = {c for c in chars if c}
    for theme in theme_rows:
        idx = int(theme.get("scene_index", 0) or 0)
        label = normalize_theme_label(theme.get("theme_label", ""))
        linked = sorted(scene_chars.get(idx, set()))
        theme["theme_role_links"] = "；".join(linked)
        theme["theme_stage"] = normalize_text(theme.get("scene", "")) or f"scene_{idx}"


def is_analysis_ready(metadata: dict) -> bool:
    try:
        score = float(metadata.get("parse_quality_score", 0) or 0)
    except Exception:
        score = 0.0
    scene_count = int(metadata.get("scene_count", 0) or 0)
    dialogue_count = int(metadata.get("dialogue_count", 0) or 0)
    return score >= 0.45 and scene_count >= 2 and dialogue_count >= 15


# -------------------------
# MinerU operations
# -------------------------


def request_upload_urls(
    session: requests.Session,
    cfg: MinerUConfig,
    batch_files: Sequence[Path],
    rel_paths: Sequence[Path],
) -> tuple[str, list[str]]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.token}",
    }
    payload = {
        "files": [{"name": p.name, "data_id": stable_data_id(rel)} for p, rel in zip(batch_files, rel_paths)],
        "model_version": cfg.model_version,
        "language": cfg.language,
    }
    data = request_json(session, "POST", UPLOAD_ENDPOINT, headers=headers, json_payload=payload, timeout=60)
    if data.get("code") != 0:
        raise RuntimeError(f"MinerU upload-url request failed: {data}")
    batch_id = data["data"]["batch_id"]
    urls = data["data"]["file_urls"]
    if len(urls) != len(batch_files):
        raise RuntimeError(f"upload url count mismatch: {len(urls)} != {len(batch_files)}")
    return batch_id, urls


def upload_files(session: requests.Session, upload_urls: Sequence[str], batch_files: Sequence[Path]) -> None:
    for url, path in zip(upload_urls, batch_files):
        last_err = None
        for attempt in range(3):
            try:
                with path.open("rb") as f:
                    r = session.put(url, data=f, timeout=600)
                r.raise_for_status()
                last_err = None
                break
            except requests.RequestException as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        if last_err is not None:
            raise last_err


def poll_results(session: requests.Session, cfg: MinerUConfig, batch_id: str) -> list[dict]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.token}",
    }
    deadline = time.time() + cfg.timeout_minutes * 60
    last_state = None
    while True:
        if time.time() > deadline:
            raise TimeoutError(f"Timed out waiting for batch {batch_id}")
        data = request_json(session, "GET", f"{RESULT_ENDPOINT}/{batch_id}", headers=headers, timeout=60)
        if data.get("code") != 0:
            raise RuntimeError(f"MinerU result request failed: {data}")
        extract = data["data"].get("extract_result")
        if not extract:
            raise RuntimeError(f"MinerU response missing extract_result: {data}")

        if isinstance(extract, dict):
            results = [extract]
            state = extract.get("state")
        else:
            results = list(extract)
            states = {item.get("state") for item in results if isinstance(item, dict)}
            state = ",".join(sorted(states)) if states else None

        if state != last_state:
            log_info(f"[MinerU] batch={batch_id} state={state}")
            last_state = state

        if isinstance(extract, dict) and extract.get("state") == "done":
            return results
        if isinstance(extract, dict) and extract.get("state") == "failed":
            raise RuntimeError(f"MinerU extraction failed: {extract.get('err_msg') or extract}")

        if isinstance(extract, list) and all(item.get("state") == "done" for item in results):
            return results
        if isinstance(extract, list) and any(item.get("state") == "failed" for item in results):
            failed = [x for x in results if x.get("state") == "failed"]
            raise RuntimeError(f"MinerU extraction failed: {failed}")

        time.sleep(cfg.wait_seconds)


def download_and_extract_zip(session: requests.Session, zip_url: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / "result.zip"
    with session.get(zip_url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with zip_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    extract_dir = out_dir / "extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)
    return extract_dir


def locate_full_md(extract_dir: Path) -> Path:
    candidates = list(extract_dir.rglob("full.md"))
    if not candidates:
        raise FileNotFoundError(f"full.md not found under {extract_dir}")
    return candidates[0]


def clean_markdown_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if PAGE_HEADER_RE.match(stripped):
            continue
        if PAGE_FOOTER_URL_RE.match(stripped):
            continue
        if TCPDF_RE.match(stripped):
            continue
        if STANDALONE_PAGE_NO_RE.match(stripped):
            continue
        if BOILERPLATE_RE.match(stripped):
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + "\n"


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\u3000", " ")).strip()


def strip_html_markup(text: str) -> str:
    return normalize_text(HTML_TAG_RE.sub(" ", text or ""))


def is_pseudo_speaker_name(name: str) -> bool:
    name = normalize_text(name)
    if not name:
        return False
    return bool(PSEUDO_SPEAKER_PREFIX_RE.match(name))


def resolve_pseudo_speaker(speaker: str, text: str, last_speaker: str, role_names: set[str]) -> tuple[str, str]:
    """Turn mis-parsed report lines (e.g. 启禀丞相：…) into 报子/童儿 + real dialogue text."""
    speaker = normalize_text(speaker)
    text = normalize_text(text)
    combined = normalize_text(f"{speaker}：{text}") if speaker and text else speaker or text
    m = REPORT_SPEAKER_LINE_RE.match(combined) or REPORT_SPEAKER_LINE_RE.match(speaker)
    if m:
        body = normalize_text(m.group("text"))
        for fallback in (last_speaker, "报子", "童儿", "旗牌"):
            if fallback and (fallback in role_names or fallback in COMMON_STAGE_SPEAKERS):
                return fallback, body
        return "报子", body
    if is_pseudo_speaker_name(speaker):
        body = text or speaker
        for fallback in (last_speaker, "报子", "童儿"):
            if fallback and (fallback in role_names or fallback in COMMON_STAGE_SPEAKERS):
                return fallback, body
        return "报子", body
    return speaker, text


def is_placeholder_dialogue_row(row: dict) -> bool:
    text = normalize_text(row.get("text", ""))
    if text and not text.startswith("<table"):
        return False
    raw = normalize_text(row.get("raw_line", ""))
    if not raw:
        return True
    if PLACEHOLDER_DIALOGUE_RE.match(raw):
        return True
    if row.get("row_type") == "dialogue" and not text and len(raw) <= 24:
        return True
    return False


def is_relation_speaker(name: str, role_names: set[str]) -> bool:
    name = normalize_text(name)
    if not name or is_pseudo_speaker_name(name):
        return False
    if name in role_names or name in COMMON_STAGE_SPEAKERS:
        return True
    return len(name) <= 6 and bool(re.search(r"^[\u4e00-\u9fff·]{1,6}$", name))


def parse_html_table_dialogue_rows(html: str) -> list[tuple[str, str, str]]:
    """Parse MinerU HTML tables into (speaker, cue, text) tuples."""
    out: list[tuple[str, str, str]] = []
    for tr in HTML_TABLE_TR_RE.findall(html):
        cells = [strip_html_markup(c) for c in HTML_TABLE_CELL_RE.findall(tr)]
        cells = [c for c in cells if c]
        if not cells:
            continue
        if len(cells) >= 3:
            speaker = cells[0]
            cue = re.sub(r"^[（(]|[）)]$", "", cells[1])
            text = cells[-1]
        elif len(cells) == 2:
            speaker, cue, text = "", cells[0], cells[1]
        else:
            speaker, cue, text = "", "", cells[0]
        if speaker and "(" in speaker:
            speaker = speaker.split("(")[0].strip()
        if not text and not speaker:
            continue
        if not speaker and text:
            m = REPORT_SPEAKER_LINE_RE.match(text)
            if m:
                speaker, text = "报子", normalize_text(m.group("text"))
        if text.startswith("(") and text.endswith(")"):
            continue
        out.append((speaker, cue, text))
    return out


def expand_table_narration_rows(dialogues: list[dict], role_names: set[str]) -> list[dict]:
    """Replace HTML-table narration blobs with per-line dialogue rows."""
    expanded: list[dict] = []
    for row in dialogues:
        text = normalize_text(row.get("text", ""))
        if row.get("row_type") != "narration" or "<table" not in text.lower():
            expanded.append(row)
            continue
        table_rows = parse_html_table_dialogue_rows(text)
        if len(table_rows) < 2:
            expanded.append(row)
            continue
        last_speaker = ""
        for speaker, cue, body in table_rows:
            if not body and not speaker:
                continue
            if body.startswith("(") and body.endswith(")"):
                expanded.append({
                    **row,
                    "row_type": "stage_direction",
                    "speaker": "",
                    "cue": "",
                    "text": body,
                    "raw_line": body,
                })
                continue
            speaker, body = resolve_pseudo_speaker(speaker, body, last_speaker, role_names)
            if not speaker:
                continue
            row_type = classify_row_type(cue, body)
            expanded.append({
                **{k: v for k, v in row.items() if k not in {"row_type", "speaker", "cue", "text", "raw_line"}},
                "row_type": row_type,
                "speaker": speaker,
                "cue": cue,
                "text": body,
                "raw_line": f"{speaker}（{cue}）{body}" if cue else f"{speaker}：{body}",
            })
            last_speaker = speaker
    return expanded


def normalize_markup_label(text: str) -> str:
    """Normalize section/marker lines by removing markdown heading prefixes and simple HTML wrappers."""
    text = normalize_text(text)
    text = HTML_TAG_RE.sub("", text)
    text = MD_HEADING_PREFIX_RE.sub("", text)
    return normalize_text(text)


def is_noise_for_analysis(text: str) -> bool:
    t = normalize_text(text)
    if not t:
        return True
    if t.startswith(("#", "<", "</", "http://", "https://")):
        return True
    if t in {"中国京剧戏考", "主要角色", "情节", "注释"}:
        return True
    if re.fullmatch(r"\d+", t):
        return True
    if any(pat.match(t) for pat in (PAGE_HEADER_RE, PAGE_FOOTER_URL_RE, TCPDF_RE, STANDALONE_PAGE_NO_RE, BOILERPLATE_RE)):
        return True
    if re.fullmatch(r"[\s\W]+", t):
        return True
    return False


def analysis_payload(row: dict) -> str:
    speaker = normalize_text(row.get("speaker", ""))
    cue = normalize_text(row.get("cue", ""))
    text = normalize_text(row.get("text", ""))
    parts = [p for p in [speaker, f"（{cue}）" if cue else "", text] if p]
    payload = normalize_text("".join(parts))
    return payload



def split_scene_marker(line: str) -> str | None:
    text = normalize_markup_label(line)
    if SCENE_MARKER_RE.match(text):
        return re.sub(r"\s+", "", text)
    return None


def is_role_section_marker(line: str) -> bool:
    text = normalize_markup_label(line)
    return any(marker in text for marker in ROLE_SECTION_MARKERS)


def extract_title(lines: list[str]) -> str:
    for line in lines[:20]:
        m = re.search(r"《([^》]{1,80})》", line)
        if m:
            return m.group(1).strip()
    for line in lines[:10]:
        text = normalize_text(line)
        if text:
            return text[:80]
    return ""


def parse_role_section(lines: list[str]) -> tuple[list[dict], set[str]]:
    roles: list[dict] = []
    start_idx: int | None = None
    end_idx = len(lines)

    for i, raw in enumerate(lines):
        text = normalize_markup_label(raw)
        if not text:
            continue
        if is_role_section_marker(text):
            start_idx = i + 1
            break

    if start_idx is None:
        return roles, set()

    for i in range(start_idx, len(lines)):
        text = normalize_markup_label(lines[i])
        if not text:
            continue
        if text in ROLE_SECTION_END_MARKERS or text.startswith("【") or SCENE_BODY_RE.search(text):
            end_idx = i
            break

    for line_no in range(start_idx, end_idx):
        raw = lines[line_no]
        text = normalize_text(raw)
        if not text:
            continue

        m = re.match(r"^(?P<name>[^：:]{1,18})[：:](?P<role>.+)$", text)
        if m:
            name = normalize_text(m.group("name"))
            role = normalize_text(m.group("role"))
            if name and role:
                roles.append({
                    "role_name": name,
                    "role_type_raw": role,
                    "role_note": "",
                    "source_line": line_no + 1,
                    "raw": raw,
                })
            continue

        if re.search(r"\s{2,}|\t+", text):
            parts = [p for p in re.split(r"\s{2,}|\t+", text) if p]
            if len(parts) == 2 and len(parts[0]) <= 18:
                roles.append({
                    "role_name": parts[0].strip(),
                    "role_type_raw": parts[1].strip(),
                    "role_note": "",
                    "source_line": line_no + 1,
                    "raw": raw,
                })

    role_names = {r["role_name"] for r in roles}
    return roles, role_names


def classify_row_type(cue: str, text: str) -> str:
    cue_norm = normalize_text(cue)
    text_norm = normalize_text(text)
    if cue_norm.startswith("西皮") or cue_norm in {"快板", "导板", "原板", "慢板", "摇板", "散板", "二六板", "流水板"}:
        return "lyric"
    if cue_norm in {"白", "念", "同白", "内白", "道白", "白口", "旁白", "叫头", "笑", "唱"}:
        return "dialogue"
    if cue_norm and not text_norm:
        return "stage_direction"
    if text_norm and not cue_norm:
        return "narration"
    return "dialogue"


def _append_continuation(row: dict, extra_text: str, raw_line: str) -> None:
    if not extra_text:
        return
    row["text"] = f"{row.get('text', '')} {extra_text}".strip() if row.get("text") else extra_text
    row["raw_line"] = f"{row.get('raw_line', '')}\n{raw_line}".strip()


def parse_scenes_and_dialogues(lines: list[str], source_meta: dict, role_names: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    scenes: list[dict] = []
    dialogues: list[dict] = []
    role_names = role_names or set()

    current_scene: dict | None = None
    scene_index = -1
    last_row: dict | None = None
    last_speaker = ""
    in_role_section = False
    role_start = None
    role_end = None

    for i, raw in enumerate(lines):
        text = normalize_text(raw)
        if not text:
            continue
        if role_start is None and is_role_section_marker(text):
            role_start = i
            continue
        if role_start is not None and (text in ROLE_SECTION_END_MARKERS or text.startswith("【") or SCENE_BODY_RE.search(text)):
            role_end = i
            break
    if role_start is None:
        role_start = -1
        role_end = -1

    def close_scene(end_line: int) -> None:
        nonlocal current_scene
        if current_scene is None:
            return
        current_scene["end_line"] = end_line
        for k in ("line_count", "dialogue_count", "lyric_count", "stage_direction_count", "narration_count"):
            current_scene.setdefault(k, 0)
        scenes.append(current_scene)
        current_scene = None

    def open_scene(title: str, line_no: int) -> None:
        nonlocal current_scene, scene_index, last_row
        if current_scene is not None:
            close_scene(line_no - 1)
        scene_index += 1
        current_scene = {
            "scene_index": scene_index,
            "scene": title,
            "line_no": line_no,
            "start_line": line_no,
            "end_line": 0,
            "characters_present": "",
            "line_count": 0,
            "dialogue_count": 0,
            "lyric_count": 0,
            "stage_direction_count": 0,
            "narration_count": 0,
        }
        last_row = None

    def ensure_preface(line_no: int) -> None:
        nonlocal current_scene, scene_index
        if current_scene is None:
            scene_index = 0
            current_scene = {
                "scene_index": 0,
                "scene": "序幕/前置内容",
                "line_no": line_no,
                "start_line": line_no,
                "end_line": 0,
                "characters_present": "",
                "line_count": 0,
                "dialogue_count": 0,
                "lyric_count": 0,
                "stage_direction_count": 0,
                "narration_count": 0,
            }

    for idx, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if PAGE_HEADER_RE.match(stripped) or PAGE_FOOTER_URL_RE.match(stripped) or TCPDF_RE.match(stripped) or STANDALONE_PAGE_NO_RE.match(stripped):
            continue

        if role_start != -1 and (idx - 1) == role_start:
            in_role_section = True
            continue
        if in_role_section and role_end is not None and (idx - 1) >= role_end:
            in_role_section = False
        if in_role_section:
            continue

        scene_marker = split_scene_marker(stripped)
        if scene_marker:
            open_scene(scene_marker, idx)
            continue

        if stripped in {"主要角色", "情节", "注释"}:
            continue

        ensure_preface(idx)
        assert current_scene is not None

        current_scene["line_count"] += 1

        m_paren_only = PAREN_ONLY_RE.match(stripped)
        if m_paren_only:
            row = {
                **source_meta,
                "scene_index": current_scene["scene_index"],
                "scene": current_scene["scene"],
                "line_no": idx,
                "row_type": "stage_direction",
                "speaker": "",
                "cue": normalize_text(m_paren_only.group("cue")),
                "text": "",
                "raw_line": raw,
            }
            dialogues.append(row)
            current_scene["stage_direction_count"] += 1
            last_row = row
            current_scene["end_line"] = idx
            continue

        m = SPEAKER_LINE_RE.match(stripped)
        if m:
            speaker = normalize_text(m.group("speaker"))
            cue = normalize_text(m.group("cue") or "")
            text = normalize_text(m.group("text") or "")
            if is_pseudo_speaker_name(speaker) or REPORT_SPEAKER_LINE_RE.match(stripped):
                speaker, text = resolve_pseudo_speaker(speaker, text, last_speaker, role_names)
            if speaker and (speaker in role_names or speaker in COMMON_STAGE_SPEAKERS or len(speaker) <= 4):
                if not cue and text in {"白", "念", "叫头", "笑", "唱"}:
                    cue, text = text, ""
                row_type = classify_row_type(cue, text)
                row = {
                    **source_meta,
                    "scene_index": current_scene["scene_index"],
                    "scene": current_scene["scene"],
                    "line_no": idx,
                    "row_type": row_type,
                    "speaker": speaker,
                    "cue": cue,
                    "text": text,
                    "raw_line": raw,
                }
                dialogues.append(row)
                if row_type == "lyric":
                    current_scene["lyric_count"] += 1
                elif row_type == "stage_direction":
                    current_scene["stage_direction_count"] += 1
                elif row_type == "narration":
                    current_scene["narration_count"] += 1
                else:
                    current_scene["dialogue_count"] += 1
                if speaker:
                    last_speaker = speaker
                last_row = row
                current_scene["end_line"] = idx
                continue

        m_front = PAREN_FRONT_RE.match(stripped)
        if m_front and len(stripped) <= 160:
            cue = normalize_text(m_front.group("cue"))
            text = normalize_text(m_front.group("text") or "")
            row_type = classify_row_type(cue, text)
            row = {
                **source_meta,
                "scene_index": current_scene["scene_index"],
                "scene": current_scene["scene"],
                "line_no": idx,
                "row_type": row_type,
                "speaker": "",
                "cue": cue,
                "text": text,
                "raw_line": raw,
            }
            dialogues.append(row)
            if row_type == "lyric":
                current_scene["lyric_count"] += 1
            elif row_type == "stage_direction":
                current_scene["stage_direction_count"] += 1
            elif row_type == "narration":
                current_scene["narration_count"] += 1
            else:
                current_scene["dialogue_count"] += 1
            last_row = row
            current_scene["end_line"] = idx
            continue

        if raw.startswith((" ", "\u3000", "\t")) and last_row is not None:
            _append_continuation(last_row, normalize_text(stripped), raw)
            current_scene["end_line"] = idx
            continue

        row = {
            **source_meta,
            "scene_index": current_scene["scene_index"],
            "scene": current_scene["scene"],
            "line_no": idx,
            "row_type": "narration",
            "speaker": "",
            "cue": "",
            "text": normalize_text(stripped),
            "raw_line": raw,
        }
        dialogues.append(row)
        current_scene["narration_count"] += 1
        last_row = row
        current_scene["end_line"] = idx

    if current_scene is not None:
        if current_scene["end_line"] == 0:
            current_scene["end_line"] = len(lines)
        scenes.append(current_scene)

    # Characters present = unique speakers per scene; later enriched by LLM if enabled.
    for scene in scenes:
        idx = int(scene["scene_index"])
        speakers = []
        seen = set()
        for row in dialogues:
            if int(row["scene_index"]) != idx:
                continue
            sp = normalize_text(row.get("speaker", ""))
            if sp and sp not in seen:
                seen.add(sp)
                speakers.append(sp)
        scene["characters_present"] = "；".join(speakers)

    return scenes, dialogues


# -------------------------
# LLM helpers
# -------------------------


def build_llm_session() -> requests.Session:
    return build_session(trust_env=False)


def extract_json_object(text: str):
    text = text.strip()
    if not text:
        raise ValueError("Empty LLM response")
    try:
        return json.loads(text)
    except Exception:
        pass
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = None
    end = None
    stack = []
    for i, ch in enumerate(text):
        if ch in "[{":
            if start is None:
                start = i
            stack.append(ch)
        elif ch in "]}":
            if stack:
                stack.pop()
                if not stack:
                    end = i
                    break
    if start is None or end is None:
        raise ValueError("No JSON object found in LLM response")
    return json.loads(text[start:end + 1])


def llm_chat_json(session: requests.Session, cfg: LLMConfig, system_prompt: str, user_prompt: str) -> dict | list:
    if not cfg.enabled:
        raise RuntimeError("LLM is disabled")
    if not cfg.api_key:
        raise RuntimeError("LLM API key is missing")
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.api_key}",
    }
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": cfg.max_tokens,
    }
    resp = session.post(url, headers=headers, json=payload, timeout=cfg.timeout_seconds)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "choices" in data:
        return extract_json_object(data["choices"][0]["message"]["content"])
    return data


def collect_role_evidence(dialogues: list[dict], role_name: str, max_lines: int = 12) -> str:
    hits: list[str] = []
    for row in dialogues:
        speaker = normalize_text(row.get("speaker", ""))
        cue = normalize_text(row.get("cue", ""))
        text = normalize_text(row.get("text", ""))
        row_text = f"{speaker}（{cue}）{text}" if cue else f"{speaker}{text}"
        if speaker == role_name or role_name in text or role_name in cue:
            hits.append(row_text.strip())
        if len(hits) >= max_lines:
            break
    if not hits:
        for row in dialogues[:max_lines]:
            speaker = normalize_text(row.get("speaker", ""))
            cue = normalize_text(row.get("cue", ""))
            text = normalize_text(row.get("text", ""))
            hits.append((f"{speaker}（{cue}）{text}" if cue else f"{speaker}{text}").strip())
    return "\n".join(hits[:max_lines])


def compact_scene_transcript(rows: list[dict], max_lines: int = 100, max_chars: int = 7000) -> str:
    chunks: list[str] = []
    total = 0
    for row in rows:
        if normalize_text(row.get("row_type", "")) not in {"dialogue", "lyric", "stage_direction", "narration"}:
            continue
        payload = analysis_payload(row)
        if is_noise_for_analysis(payload):
            continue
        if payload in {"", "（）"}:
            continue
        if len(chunks) >= max_lines:
            break
        if total + len(payload) > max_chars:
            break
        chunks.append(payload)
        total += len(payload)
    return "\n".join(chunks)


def infer_role_from_heuristics(role_name: str, role_type_raw: str, evidence: str) -> dict:
    """Heuristic role inference with subtype preservation."""
    raw = normalize_text(role_type_raw)
    text = normalize_text(f"{role_name} {raw} {evidence}")

    subtype_patterns = [
        "老生", "武生", "小生", "娃娃生", "文生", "生",
        "青衣", "花旦", "老旦", "武旦", "刀马旦", "闺门旦", "正旦", "旦",
        "净", "花脸", "大花脸", "架子花脸",
        "丑", "武丑", "文丑", "丑角",
        "外", "末", "贴",
    ]
    inferred = ""
    if raw:
        for label in subtype_patterns:
            if raw == label or raw.startswith(label) or label in raw:
                inferred = raw
                break

    if not inferred:
        if any(k in text for k in ROLE_GUESS_KEYWORDS["旦"]):
            inferred = "旦"
        elif any(k in text for k in ROLE_GUESS_KEYWORDS["净"]):
            inferred = "净"
        elif any(k in text for k in ROLE_GUESS_KEYWORDS["丑"]):
            inferred = "丑"
        else:
            inferred = "生"

    if any(k in text for k in ["女", "夫人", "小姐", "妃", "娘", "妻", "母", "姑", "老太", "老娘"]):
        gender = "女"
    elif any(k in text for k in ["童儿", "小", "公子", "将军", "先生", "男", "爷", "老爷", "元帅", "少爷"]):
        gender = "男"
    else:
        gender = "未知"

    if any(k in text for k in ["老", "年", "父", "母", "老军", "太君", "老太", "老爷"]):
        age = "偏老/中老年"
    elif any(k in text for k in ["童", "少", "小", "儿", "娃"]):
        age = "偏少/少年"
    else:
        age = "未知"

    if any(k in text for k in ["相", "将", "官", "王", "帝", "帅", "丞相", "元帅", "太君", "夫人"]):
        status = "高位/官员"
    elif any(k in text for k in ["军", "兵", "卒", "差", "仆", "童", "家人", "家院", "报子", "旗牌", "鬼卒"]):
        status = "基层/随从"
    else:
        status = "未知"

    personality = []
    if any(k in text for k in ["忠", "义", "孝"]):
        personality.append("忠义/孝义")
    if any(k in text for k in ["谨慎", "稳", "沉着", "冷静", "机"]):
        personality.append("谨慎沉着")
    if any(k in text for k in ["勇", "武", "猛", "烈", "刚"]):
        personality.append("勇武刚烈")
    if any(k in text for k in ["奸", "诈", "狡", "滑"]):
        personality.append("机诈狡黠")
    if any(k in text for k in ["笑", "诙谐", "滑稽", "趣"]):
        personality.append("诙谐滑稽")

    return {
        "role_type_inferred": inferred,
        "gender_inferred": gender,
        "age_inferred": age,
        "status_identity": status,
        "personality_tags": personality,
        "behavior_tags": [],
        "speech_style": [],
        "evidence": evidence[:120],
        "confidence": 0.3 if inferred else 0.1,
    }


def llm_enrich_roles(
    session: requests.Session,
    llm: LLMConfig,
    roles: list[dict],
    dialogues: list[dict],
    doc_title: str,
) -> list[dict]:
    if not llm.enabled or not roles:
        return roles

    system = (
        "你是中国京剧剧本分析助手。请根据角色表和文本证据，输出严格JSON，只返回JSON对象，不要解释。"
    )
    enriched: list[dict] = []
    for role in roles:
        role_name = role.get("role_name", "")
        evidence = collect_role_evidence(dialogues, role_name, max_lines=llm.max_role_evidence)
        user = f"""
剧名：{doc_title}
角色名：{role_name}
原始行当标注：{role.get('role_type_raw', '')}

请根据下列证据补全角色画像：
{evidence}

请输出JSON对象，字段固定为：
{{
  "role_type_inferred": "",
  "gender_inferred": "",
  "age_inferred": "",
  "status_identity": "",
  "personality_tags": ["", ""],
  "behavior_tags": ["", ""],
  "speech_style": ["", ""],
  "evidence": "一句话概括证据依据",
  "confidence": 0.0
}}
要求：
- 仅基于证据推断，不要编造。
- personality_tags / behavior_tags / speech_style 用数组。
"""
        row = dict(role)
        try:
            result = llm_chat_json(session, llm, system, user)
            if isinstance(result, dict):
                row["role_type_inferred"] = result.get("role_type_inferred", "")
                row["gender_inferred"] = result.get("gender_inferred", "")
                row["age_inferred"] = result.get("age_inferred", "")
                row["status_identity"] = result.get("status_identity", "")
                row["personality_tags"] = "；".join(result.get("personality_tags", []) or [])
                row["behavior_tags"] = "；".join(result.get("behavior_tags", []) or [])
                row["speech_style"] = "；".join(result.get("speech_style", []) or [])
                row["evidence"] = result.get("evidence", "")
                row["confidence"] = result.get("confidence", "")
        except Exception as e:
            row["llm_error"] = str(e)
            fallback = infer_role_from_heuristics(role_name, role.get("role_type_raw", ""), evidence)
            for k, v in fallback.items():
                row.setdefault(k, v)
        enriched.append(row)
    return enriched



def llm_enrich_documents(
    session: requests.Session,
    llm: LLMConfig,
    preface_meta: dict,
    doc_context: dict,
    dialogues: list[dict],
) -> dict:
    """Use LLM to refine document-level metadata from title, preface and context.

    This is intentionally conservative: if the LLM fails, the original heuristic
    metadata is returned unchanged.
    """
    if not llm.enabled:
        return preface_meta

    system = (
        "你是中国京剧剧本元数据抽取助手。请根据剧名、集合信息、前言和少量正文，"
        "输出严格 JSON，只返回 JSON 对象，不要解释。"
    )

    sample_lines: list[str] = []
    for row in dialogues[: min(len(dialogues), 60)]:
        speaker = normalize_text(row.get("speaker", ""))
        cue = normalize_text(row.get("cue", ""))
        text = normalize_text(row.get("text", ""))
        if speaker or cue or text:
            sample_lines.append(f"{speaker}（{cue}）{text}".strip())
    sample_text = "\n".join(sample_lines[:60])

    user = f"""
剧名：{doc_context.get('play_title', '')}
集合名称：{doc_context.get('collection_name', '')}
集合编码：{doc_context.get('collection_code', '')}
作品编码：{doc_context.get('work_code', '')}
作品标题提示：{doc_context.get('work_title_hint', '')}

前言结构化信息（规则抽取）：
{json.dumps(preface_meta, ensure_ascii=False, indent=2)}

正文样例：
{sample_text}

请输出 JSON 对象，字段固定为：
{{
  "title": "剧名",
  "aliases": ["别名1", "别名2"],
  "period_hint": "时代/历史背景",
  "genre_hint": "历史戏|家庭戏|公案戏|战争戏|其他",
  "synopsis": "40-120字剧情摘要",
  "note_text": "注释要点摘要，如无则为空",
  "keywords": ["主题词1", "主题词2"],
  "confidence": 0.0
}}
要求：
- 只依据前言和正文样例推断，不要编造。
- 如果无法确定某项，保留为空或空数组。
- aliases 不要重复剧名本身。
"""
    try:
        result = llm_chat_json(session, llm, system, user)
        if not isinstance(result, dict):
            return preface_meta
        merged = dict(preface_meta)
        for key in ["title", "period_hint", "genre_hint", "synopsis", "note_text", "confidence"]:
            if result.get(key) not in (None, "", [], {}):
                merged[key] = result.get(key)
        if result.get("aliases"):
            merged["aliases"] = [a for a in result.get("aliases", []) if a]
        if result.get("keywords"):
            merged["keywords"] = [k for k in result.get("keywords", []) if k]
        return merged
    except Exception:
        return preface_meta

def llm_enrich_scenes_and_relations(
    session: requests.Session,
    llm: LLMConfig,
    scenes: list[dict],
    dialogues: list[dict],
    doc_title: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    if not llm.enabled or not scenes:
        return scenes, [], []

    by_scene: dict[int, list[dict]] = defaultdict(list)
    for row in dialogues:
        by_scene[int(row["scene_index"])].append(row)

    system = (
        "你是中国京剧剧本结构化分析助手。请根据场次文本输出严格JSON，用于后续人物关系、主题、叙事结构分析。"
        "只返回JSON对象，不要输出解释文字。"
    )

    enriched_scenes: list[dict] = []
    relation_rows: list[dict] = []
    theme_rows: list[dict] = []
    llm_failed = False

    for scene in scenes:
        idx = int(scene["scene_index"])
        rows = by_scene.get(idx, [])
        transcript = compact_scene_transcript(rows, max_lines=llm.max_scene_lines, max_chars=llm.max_input_chars)
        characters = sorted({normalize_text(r.get("speaker", "")) for r in rows if normalize_text(r.get("speaker", ""))})
        row = dict(scene)

        if not transcript.strip() or llm_failed:
            enriched_scenes.append(row)
            continue

        user = f"""
剧名：{doc_title}
场次：{scene.get('scene', '')}
本场角色：{ '、'.join(characters) if characters else '未知' }

本场文本：
{transcript}

请输出JSON对象，字段固定为：
{{
  "summary": "本场简要摘要，40-80字",
  "conflict_stage": "铺垫|发展|冲突|高潮|收束|转折|过渡",
  "tension_level": 1,
  "scene_function": "引子|铺垫|推进|对峙|高潮|反转|收束|过渡",
  "key_characters": ["角色A", "角色B"],
  "theme_labels": ["主题1", "主题2"],
  "relations": [
    {{
      "source": "角色A",
      "target": "角色B",
      "relation_type": "对抗|命令|协助|汇报|评价|提及|同盟|旁观|劝阻|试探",
      "weight": 1,
      "evidence": "对应证据短句"
    }}
  ]
}}
要求：
- summary、theme_labels、key_characters 要简洁准确。
- relations 保留最重要的 0-8 条。
- weight 为 1-5 整数。
- 关系方向尽量表达从发话/施动者到受动者。
"""
        try:
            result = llm_chat_json(session, llm, system, user)
            if isinstance(result, dict):
                row["summary"] = result.get("summary", "")
                row["conflict_stage"] = result.get("conflict_stage", "")
                row["tension_level"] = result.get("tension_level", "")
                row["scene_function"] = result.get("scene_function", "")
                row["key_characters"] = "；".join(result.get("key_characters", []) or [])
                row["theme_labels"] = "；".join(result.get("theme_labels", []) or [])
                for rel in result.get("relations", []) or []:
                    relation_rows.append({
                        "doc_title": doc_title,
                        "scene_index": idx,
                        "scene": scene.get("scene", ""),
                        "source": rel.get("source", ""),
                        "target": rel.get("target", ""),
                        "relation_type": rel.get("relation_type", ""),
                        "weight": rel.get("weight", ""),
                        "evidence": rel.get("evidence", ""),
                        "derived_by": "llm",
                    })
                for theme in result.get("theme_labels", []) or []:
                    theme_rows.append({
                        "doc_title": doc_title,
                        "scene_index": idx,
                        "scene": scene.get("scene", ""),
                        "theme_label": theme,
                        "evidence": "",
                        "derived_by": "llm",
                    })
        except Exception as e:
            row["llm_error"] = str(e)
            if any(code in str(e) for code in ["402", "401", "403", "Payment Required"]):
                llm_failed = True
        enriched_scenes.append(row)

    max_scene_index = max((int(s.get("scene_index", 0)) for s in scenes), default=0)
    for scene in enriched_scenes:
        idx = int(scene.get("scene_index", 0))
        rows = by_scene.get(idx, [])
        payloads = [analysis_payload(r) for r in rows]
        fallback_themes = simple_theme_fallback([p for p in payloads if not is_noise_for_analysis(p)])
        if not normalize_text(scene.get("summary", "")):
            scene["summary"] = fallback_scene_summary(rows)
        if not normalize_text(scene.get("conflict_stage", "")):
            scene["conflict_stage"] = "铺垫" if idx == 0 else ("收束" if idx == max_scene_index else "发展")
        if not normalize_text(scene.get("scene_function", "")):
            scene["scene_function"] = "过渡" if idx == 0 else "推进"
        if not scene.get("tension_level") or str(scene.get("tension_level")).strip() == "":
            scene["tension_level"] = 1 + min(4, sum(1 for r in rows if any(k in normalize_text(r.get("text", "")) for k in ["死", "自刎", "劈", "哭", "惊", "病", "吐"])) )
        if not normalize_text(scene.get("key_characters", "")):
            speakers = []
            for r in rows:
                sp = normalize_text(r.get("speaker", ""))
                if sp and sp not in speakers:
                    speakers.append(sp)
            scene["key_characters"] = "；".join(speakers[:6])
        if not normalize_text(scene.get("theme_labels", "")) and fallback_themes:
            scene["theme_labels"] = "；".join(fallback_themes)
        if scene.get("llm_error") is None:
            scene["llm_error"] = ""

    return enriched_scenes, relation_rows, theme_rows


# -------------------------
# Semantic helpers
# -------------------------


def extract_entity_mentions(text: str, candidates: set[str]) -> set[str]:
    hits = set()
    for name in candidates:
        if name and name in text:
            hits.add(name)
    return hits


def fallback_relations_from_scenes(scenes: list[dict], dialogues: list[dict]) -> list[dict]:
    by_scene: dict[int, list[dict]] = defaultdict(list)
    for row in dialogues:
        by_scene[int(row["scene_index"])].append(row)
    relation_rows: list[dict] = []
    for scene in scenes:
        idx = int(scene["scene_index"])
        rows = by_scene.get(idx, [])
        speakers = []
        seen = set()
        for r in rows:
            sp = normalize_text(r.get("speaker", ""))
            if sp and sp not in seen:
                seen.add(sp)
                speakers.append(sp)
        if len(speakers) < 2:
            continue
        for a, b in itertools.combinations(speakers, 2):
            relation_rows.append({
                "doc_title": "",
                "scene_index": idx,
                "scene": scene.get("scene", ""),
                "source": a,
                "target": b,
                "relation_type": "同场共现",
                "weight": 1,
                "evidence": "",
                "derived_by": "cooccurrence",
            })
    return relation_rows


def aggregate_relations(rows: list[dict]) -> list[dict]:
    bucket = Counter()
    evidence_map: dict[tuple[str, str, str, str], str] = {}
    doc_title = ""
    for r in rows:
        doc_title = doc_title or r.get("doc_title", "")
        key = (
            normalize_text(r.get("doc_title", "")),
            normalize_text(r.get("source", "")),
            normalize_text(r.get("target", "")),
            normalize_text(r.get("relation_type", "")),
        )
        bucket[key] += int(r.get("weight") or 1)
        if key not in evidence_map and r.get("evidence"):
            evidence_map[key] = r.get("evidence", "")
    out = []
    for (doc, src, tgt, rel_type), weight in bucket.items():
        out.append({
            "doc_title": doc,
            "source": src,
            "target": tgt,
            "relation_type": rel_type,
            "weight": weight,
            "evidence": evidence_map.get((doc, src, tgt, rel_type), ""),
            "derived_by": "aggregated",
        })
    return sorted(out, key=lambda x: (-int(x.get("weight", 0)), x.get("source", ""), x.get("target", "")))


def simple_theme_fallback(texts: list[str]) -> list[str]:
    joined = "\n".join(texts)
    found = []
    for theme, keys in THEME_KEYWORDS.items():
        if any(k in joined for k in keys):
            found.append(theme)
    return found


def split_tags(text: str) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    parts = re.split(r"[；;、,，/\s]+", text)
    return [p for p in (normalize_text(x) for x in parts) if p]


def cue_to_perform_type(cue: str, row_type: str) -> str:
    cue = normalize_text(cue)
    if row_type == "stage_direction":
        return "做"
    if row_type == "narration":
        return "说明"
    if cue.startswith("西皮") or cue.startswith("二黄") or cue in {"唱", "原板", "慢板", "摇板", "快板", "导板", "散板", "二六板", "流水板"}:
        return "唱"
    if cue in {"白", "内白", "同白", "道白", "白口"}:
        return "白"
    if cue in {"念"}:
        return "念"
    if cue in {"叫头"}:
        return "叫头"
    if cue in {"哭", "笑"}:
        return "做"
    return "对白"


ACTION_KEYWORDS = [
    "上", "下", "进", "退", "走", "来", "去", "看", "问", "答", "请", "传", "报", "禀", "唤",
    "命", "令", "叫", "道", "拿", "抓", "打", "劈", "杀", "刺", "追", "拦", "扶", "搀",
    "跪", "拜", "坐", "立", "哭", "笑", "叹", "惊", "吐", "自刎", "掩埋", "收", "射"
]


def extract_action_tags(text: str, cue: str = "") -> list[str]:
    payload = f"{normalize_text(cue)} {normalize_text(text)}"
    tags = []
    for kw in ACTION_KEYWORDS:
        if kw in payload and kw not in tags:
            tags.append(kw)
    if normalize_text(cue).startswith("西皮"):
        tags.append("唱段")
    if normalize_text(cue) in {"白", "内白", "同白", "道白", "白口"}:
        tags.append("念白")
    return tags


def _clean_stage_token(token: str) -> str:
    token = normalize_text(token)
    token = re.sub(r"[。．.!！？?]+$", "", token)
    token = re.sub(r"(同上|同下|同进|同退|上|下|进|退|走|来|去|过场)$", "", token)
    token = re.sub(r"(急急风|慢板|摇板|快板)$", "", token)
    return normalize_text(token)


def extract_stage_participants(raw_line: str, candidate_names: set[str]) -> list[str]:
    text = normalize_text(raw_line)
    if not text:
        return []
    # Remove outer brackets if present.
    text = text.strip("（）()")
    text = text.replace("同上", "、").replace("同下", "、").replace("同进", "、").replace("同退", "、")
    text = text.replace("上", "、").replace("下", "、")
    hits = []
    search_pool = set(candidate_names) | COMMON_STAGE_SPEAKERS | {"杨继业", "杨延昭", "杨宗保", "佘太君", "柴夫人", "赵德芳", "孟良", "焦赞", "程宣"}
    for name in sorted(search_pool, key=len, reverse=True):
        if name and name in text and name not in hits:
            hits.append(name)
    if hits:
        return hits
    # Fallback: split by punctuation and keep likely stage tokens.
    parts = re.split(r"[、,，；;\s]+", text)
    for part in parts:
        part = _clean_stage_token(part)
        if not part:
            continue
        if len(part) <= 12 and re.search(r"[一-鿿]", part):
            if part not in hits:
                hits.append(part)
    return hits


def infer_relation_type_from_text(text: str) -> str:
    text = normalize_text(text)
    if any(k in text for k in ["命", "令", "传", "唤", "请", "吩咐"]):
        return "命令/请求"
    if any(k in text for k in ["报", "禀", "启禀", "回禀"]):
        return "汇报"
    if any(k in text for k in ["问", "可曾", "怎讲", "为何", "如何", "哪里", "哪有"]):
        return "问答"
    if any(k in text for k in ["劈", "打", "杀", "刺", "射", "拿奸细", "拿", "抓"]):
        return "对抗"
    if any(k in text for k in ["拜", "谢", "请坐", "赐坐", "参见", "见礼"]):
        return "礼节/隶属"
    return "提及"


def fallback_roles_from_dialogues(dialogues: list[dict]) -> list[dict]:
    seen = set()
    roles = []
    for row in dialogues:
        speaker = normalize_text(row.get("speaker", ""))
        if not speaker or speaker in seen:
            continue
        seen.add(speaker)
        roles.append({
            "role_name": speaker,
            "role_type_raw": "",
            "role_type_inferred": "",
            "gender_inferred": "",
            "age_inferred": "",
            "status_identity": "",
            "personality_tags": "",
            "behavior_tags": "",
            "speech_style": "",
            "evidence": "",
            "source_line": row.get("line_no", ""),
            "raw": row.get("raw_line", ""),
            "confidence": "",
            "llm_error": "",
            "role_note": "fallback_from_dialogues",
        })
    return roles


def fallback_scene_summary(rows: list[dict], max_chars: int = 80) -> str:
    snippets = []
    for r in rows:
        speaker = normalize_text(r.get("speaker", ""))
        cue = normalize_text(r.get("cue", ""))
        text = normalize_text(r.get("text", ""))
        payload = " ".join(x for x in [speaker, cue, text] if x)
        if payload:
            snippets.append(payload)
        if len(snippets) >= 3:
            break
    if not snippets:
        return ""
    joined = "；".join(snippets)
    return joined[:max_chars]


def build_performance_rows(
    dialogues: list[dict],
    role_names: set[str],
    doc_id: str,
    doc_title: str,
) -> list[dict]:
    rows = []
    for r in dialogues:
        row_type = normalize_text(r.get("row_type", ""))
        cue = normalize_text(r.get("cue", ""))
        text = normalize_text(r.get("text", ""))
        raw_line = r.get("raw_line", "")
        speaker = normalize_text(r.get("speaker", ""))
        participants = extract_stage_participants(raw_line or text, role_names | {speaker} if speaker else role_names)
        rows.append({
            "doc_id": doc_id,
            "doc_title": doc_title,
            "source_file": r.get("source_file", ""),
            "source_path": r.get("source_path", ""),
            "relative_path": r.get("relative_path", ""),
            "batch_id": r.get("batch_id", ""),
            "scene_index": r.get("scene_index", ""),
            "scene": r.get("scene", ""),
            "line_no": r.get("line_no", ""),
            "row_type": row_type,
            "speaker": speaker,
            "cue": cue,
            "perform_type": cue_to_perform_type(cue, row_type),
            "perform_subtype": cue if cue else "",
            "action_tags": "；".join(extract_action_tags(text, cue)),
            "participants": "；".join(participants),
            "text": text,
            "raw_line": raw_line,
        })
    return rows


def heuristic_relations_from_dialogues(
    dialogues: list[dict],
    role_names: set[str],
    doc_title: str,
) -> list[dict]:
    by_scene_all: dict[int, list[dict]] = defaultdict(list)
    by_scene_dialogue: dict[int, list[dict]] = defaultdict(list)
    for row in dialogues:
        by_scene_all[int(row["scene_index"])].append(row)
        if row.get("row_type") not in {"dialogue", "lyric"}:
            continue
        if is_placeholder_dialogue_row(row):
            continue
        by_scene_dialogue[int(row["scene_index"])].append(row)

    relations: list[dict] = []
    candidate_roles = {n for n in role_names if is_relation_speaker(n, role_names)} | COMMON_STAGE_SPEAKERS
    for idx, rows in by_scene_dialogue.items():
        # 1) speaker adjacency relations
        prev_speaker = ""
        prev_text = ""
        for row in rows:
            speaker = normalize_text(row.get("speaker", ""))
            text = strip_html_markup(row.get("text", ""))
            if not is_relation_speaker(speaker, role_names):
                continue
            if speaker and prev_speaker and speaker != prev_speaker and text:
                relations.append({
                    "doc_title": doc_title,
                    "scene_index": idx,
                    "scene": row.get("scene", ""),
                    "source": prev_speaker,
                    "target": speaker,
                    "relation_type": "对话/应答",
                    "weight": 1,
                    "evidence": strip_html_markup(prev_text)[:80],
                    "derived_by": "adjacency",
                })
            if speaker:
                prev_speaker = speaker
            if text:
                prev_text = text

        # 2) explicit mentions and imperative relations
        for row in rows:
            speaker = normalize_text(row.get("speaker", ""))
            text = strip_html_markup(row.get("text", ""))
            if not is_relation_speaker(speaker, role_names) or not text:
                continue
            targets = extract_entity_mentions(text, candidate_roles - {speaker})
            for target in targets:
                relations.append({
                    "doc_title": doc_title,
                    "scene_index": idx,
                    "scene": row.get("scene", ""),
                    "source": speaker,
                    "target": target,
                    "relation_type": infer_relation_type_from_text(text),
                    "weight": 1,
                    "evidence": text[:80],
                    "derived_by": "mention",
                })

    for idx, rows in by_scene_all.items():
        # 3) stage-direction co-presence
        stage_rows = [r for r in rows if normalize_text(r.get("row_type", "")) == "stage_direction"]
        for row in stage_rows:
            participants = extract_stage_participants(row.get("raw_line", "") or row.get("text", ""), candidate_roles)
            if len(participants) < 2:
                continue
            for a, b in itertools.combinations(dict.fromkeys(participants), 2):
                relations.append({
                    "doc_title": doc_title,
                    "scene_index": idx,
                    "scene": row.get("scene", ""),
                    "source": a,
                    "target": b,
                    "relation_type": "同场共现",
                    "weight": 1,
                    "evidence": normalize_text(row.get("raw_line", ""))[:80],
                    "derived_by": "stage_direction",
                })
    return relations


def dedupe_rows(rows: list[dict], key_fields: list[str], weight_field: str = "weight") -> list[dict]:
    bucket: dict[tuple, dict] = {}
    for r in rows:
        key = tuple(normalize_text(str(r.get(k, ""))) for k in key_fields)
        if key not in bucket:
            bucket[key] = dict(r)
        else:
            try:
                bucket[key][weight_field] = int(bucket[key].get(weight_field, 1) or 1) + int(r.get(weight_field, 1) or 1)
            except Exception:
                bucket[key][weight_field] = bucket[key].get(weight_field, 1)
            if not bucket[key].get("evidence") and r.get("evidence"):
                bucket[key]["evidence"] = r.get("evidence")
    return list(bucket.values())


# -------------------------
# Main parsing / writing
# -------------------------


def extract_preface_metadata(lines: list[str]) -> dict:
    """Extract front-matter style metadata from the preface section."""
    title = extract_title(lines)
    alias_line = ""
    synopsis_lines: list[str] = []
    note_lines: list[str] = []
    in_synopsis = False
    in_note = False
    for raw in lines[:120]:
        text = normalize_markup_label(raw)
        if not text:
            continue
        if title and text.startswith(f"《{title}》"):
            alias_line = text
            continue
        if text == "情节":
            in_synopsis = True
            in_note = False
            continue
        if text == "注释":
            in_synopsis = False
            in_note = True
            continue
        if text.startswith("根据《"):
            break
        if in_synopsis:
            synopsis_lines.append(text)
        elif in_note:
            note_lines.append(text)

    aliases = []
    if alias_line:
        found = re.findall(r"《([^》]+)》", alias_line)
        if found:
            aliases = [x for x in found if x and x != title]

    synopsis = normalize_text(" ".join(synopsis_lines))
    note_text = normalize_text(" ".join(note_lines))

    period_hint = ""
    for kw in ["先秦", "秦", "汉", "东汉", "三国", "魏", "晋", "南北朝", "隋", "唐", "五代", "宋", "元", "明", "清", "民国"]:
        if kw in synopsis:
            period_hint = kw
            break

    genre_hint = ""
    if any(k in synopsis for k in ["公案", "案"]):
        genre_hint = "公案戏"
    elif any(k in synopsis for k in ["家", "母", "夫人", "娘", "亲"]):
        genre_hint = "家庭戏"
    elif any(k in synopsis for k in ["战", "兵", "营", "将", "帅", "番"]):
        genre_hint = "历史/战争戏"

    return {
        "title": title,
        "aliases": aliases,
        "alias_text": alias_line,
        "synopsis": synopsis,
        "note_text": note_text,
        "period_hint": period_hint,
        "genre_hint": genre_hint,
    }


# =========================
# v5 final refinements
# =========================

# Canonical labels for cross-play comparison.
RELATION_CANONICAL_MAP = {
    "对话": "对话",
    "对话/应答": "对话",
    "问答": "对话",
    "应答": "对话",
    "命令": "命令",
    "命令/请求": "命令",
    "请求": "命令",
    "汇报": "汇报",
    "报告": "汇报",
    "提报": "汇报",
    "对抗": "对抗",
    "冲突": "对抗",
    "战争": "对抗",
    "协助": "协助",
    "同盟": "协助",
    "帮助": "协助",
    "救助": "协助",
    "评价": "评价",
    "评论": "评价",
    "提及": "提及",
    "同场共现": "同场共现",
    "旁观": "旁观",
    "劝阻": "劝阻",
    "试探": "试探",
    "礼节/隶属": "礼节/隶属",
}

THEME_CANONICAL_RULES = [
    ("忠义", ["忠义", "忠", "义", "报国", "扶汉", "尽忠", "忠孝"]),
    ("亲情", ["亲情", "父子", "母子", "兄弟", "夫妻", "家庭", "思念"]),
    ("战争", ["战争", "征战", "兵", "战", "军", "阵", "攻", "守", "城"]),
    ("家国", ["家国", "国", "朝", "社稷", "天下", "君臣"]),
    ("权谋", ["权谋", "计", "谋", "诈", "虚实", "兵法", "空城"]),
    ("宫廷", ["宫廷", "宫中", "朝廷", "帝王", "太监", "千岁", "王府"]),
    ("幽冥", ["幽冥", "鬼", "魂", "阴间", "地府", "鬼卒", "冥"]),
    ("悲情", ["悲", "哀", "病", "死", "亡", "泪", "伤", "痛", "忧", "疾"]),
    ("思念", ["思念", "盼", "想", "牵挂", "念"]),
    ("离散", ["离散", "别", "散", "走", "逃", "流亡"]),
    ("喜剧", ["笑", "闹", "滑", "趣", "诙", "逗"]),
    ("公案", ["公案", "审", "冤", "狱", "案"]),
    ("情爱", ["情爱", "爱", "情", "婚", "姻", "媒", "恋"]),
    ("使命", ["使命", "命", "令", "嘱", "任"]),
    ("疾病与死亡", ["病", "疾", "死", "亡", "危"]),
]

COMMON_LOCATION_TERMS = [
    "西城", "城楼", "西门", "汉中", "祁山", "街亭", "白帝城", "卧龙岗", "番营",
    "郡马府", "病房", "宫中", "宫门", "皇宫", "朝堂", "大殿", "寒窑", "庙中", "寺中",
    "地府", "阴间", "营中", "山头", "山上", "山下", "府前", "后堂", "前堂", "庭院",
    "帐中", "帐外", "门外", "门内", "厅上", "厅下", "府中", "城外", "河边", "路上",
]

SCENE_FUNCTION_ORDER = ["引子", "铺垫", "推进", "对峙", "高潮", "反转", "收束", "过渡"]

def _llm_items(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    elif isinstance(value, str):
        items = re.split(r"[；;、,，/\s]+", value)
    else:
        items = [str(value)]
    out: list[str] = []
    for item in items:
        item = normalize_text(str(item))
        if item and item not in out:
            out.append(item)
    return out


def _llm_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, (list, tuple, set)):
        return "；".join(_llm_items(value))
    return normalize_text(str(value))


def _scene_excerpt(rows: list[dict], max_lines: int, max_chars: int) -> str:
    return compact_scene_transcript(rows, max_lines=max_lines, max_chars=max_chars)


def extract_json_object(text: str):
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty LLM response")
    try:
        return json.loads(text)
    except Exception:
        pass
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = None
    stack = []
    end = None
    for i, ch in enumerate(text):
        if ch in "[{":
            if start is None:
                start = i
            stack.append(ch)
        elif ch in "]}":
            if stack:
                stack.pop()
                if not stack:
                    end = i
                    break
    if start is None or end is None:
        raise ValueError("No JSON object found in LLM response")
    return json.loads(text[start:end + 1])


def llm_chat_json(session: requests.Session, cfg: LLMConfig, system_prompt: str, user_prompt: str) -> dict | list:
    if not cfg.enabled:
        raise RuntimeError("LLM is disabled")
    if not cfg.api_key:
        raise RuntimeError("LLM API key is missing")

    url = cfg.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.api_key}",
    }
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": cfg.max_tokens,
    }

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            resp = session.post(url, headers=headers, json=payload, timeout=cfg.timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "choices" in data:
                content = data["choices"][0]["message"]["content"]
                return extract_json_object(content)
            return data
        except Exception as e:
            last_err = e
            time.sleep(min(2 ** attempt, 6))
    assert last_err is not None
    raise last_err


def normalize_relation_type(raw: str) -> str:
    t = normalize_text(raw)
    if not t:
        return "提及"
    if t in RELATION_CANONICAL_MAP:
        return RELATION_CANONICAL_MAP[t]
    if any(k in t for k in ["对话", "问答", "应答"]):
        return "对话"
    if any(k in t for k in ["命令", "请求", "吩咐", "传令"]):
        return "命令"
    if any(k in t for k in ["汇报", "报告", "启禀", "回禀"]):
        return "汇报"
    if any(k in t for k in ["对抗", "冲突", "斗争", "战", "杀", "斩", "退兵"]):
        return "对抗"
    if any(k in t for k in ["协助", "同盟", "帮助", "救", "扶", "护送"]):
        return "协助"
    if any(k in t for k in ["评价", "评论"]):
        return "评价"
    if any(k in t for k in ["旁观"]):
        return "旁观"
    if any(k in t for k in ["劝阻"]):
        return "劝阻"
    if any(k in t for k in ["试探"]):
        return "试探"
    if any(k in t for k in ["同场", "共现"]):
        return "同场共现"
    return t if len(t) <= 12 else "提及"


def normalize_theme_label(raw: str) -> str:
    t = normalize_text(raw)
    if not t:
        return ""
    for canonical, keywords in THEME_CANONICAL_RULES:
        if t == canonical or t in keywords or any(k and k in t for k in keywords):
            return canonical
    # fallback: if the LLM already gave a meaningful short label, preserve it
    return t[:12]


def aggregate_relations(rows: list[dict]) -> list[dict]:
    bucket: dict[tuple, dict] = {}
    for r in rows:
        doc_title = normalize_text(r.get("doc_title", ""))
        src = normalize_text(r.get("source", ""))
        tgt = normalize_text(r.get("target", ""))
        rel = normalize_relation_type(r.get("relation_type", r.get("relation_type_raw", "")))
        key = (doc_title, src, tgt, rel)
        weight = r.get("weight", 1)
        try:
            weight = int(weight)
        except Exception:
            weight = 1
        if key not in bucket:
            bucket[key] = {
                "doc_title": doc_title,
                "source": src,
                "target": tgt,
                "relation_type": rel,
                "weight": weight,
                "evidence": normalize_text(r.get("evidence", "")),
                "derived_by": r.get("derived_by", ""),
            }
        else:
            bucket[key]["weight"] += weight
            if not bucket[key]["evidence"] and r.get("evidence"):
                bucket[key]["evidence"] = normalize_text(r.get("evidence", ""))
            if bucket[key]["derived_by"] != "llm" and r.get("derived_by") == "llm":
                bucket[key]["derived_by"] = "llm"
    return sorted(bucket.values(), key=lambda x: (-int(x.get("weight", 0)), x.get("source", ""), x.get("target", ""), x.get("relation_type", "")))


def aggregate_themes(rows: list[dict]) -> list[dict]:
    bucket: dict[tuple, dict] = {}
    for r in rows:
        doc_title = normalize_text(r.get("doc_title", ""))
        scene = normalize_text(r.get("scene", ""))
        theme = normalize_theme_label(r.get("theme_label", r.get("theme_label_raw", "")))
        if not theme:
            continue
        key = (doc_title, theme)
        weight = r.get("weight", 1)
        try:
            weight = int(weight)
        except Exception:
            weight = 1
        if key not in bucket:
            bucket[key] = {
                "doc_title": doc_title,
                "theme_label": theme,
                "weight": weight,
                "evidence": normalize_text(r.get("evidence", "")),
                "derived_by": r.get("derived_by", ""),
            }
        else:
            bucket[key]["weight"] += weight
            if not bucket[key]["evidence"] and r.get("evidence"):
                bucket[key]["evidence"] = normalize_text(r.get("evidence", ""))
            if bucket[key]["derived_by"] != "llm" and r.get("derived_by") == "llm":
                bucket[key]["derived_by"] = "llm"
    return sorted(bucket.values(), key=lambda x: (-int(x.get("weight", 0)), x.get("theme_label", "")))


# -------------------------
# Competition analytics layer (IDs, aliases, quality, derived tables)
# -------------------------

STATIC_CHARACTER_ALIASES: list[tuple[str, str]] = [
    ("孔明", "诸葛亮"), ("诸葛孔明", "诸葛亮"), ("诸葛", "诸葛亮"),
    ("司马", "司马懿"), ("司马仲达", "司马懿"),
    ("子龙", "赵云"), ("赵子龙", "赵云"),
    ("关公", "关羽"), ("云长", "关羽"),
    ("翼德", "张飞"), ("皇叔", "刘备"),
    ("孟起", "马超"), ("汉升", "黄忠"), ("魏延", "魏延"),
    ("奉先", "吕布"), ("孟德", "曹操"), ("玄德", "刘备"),
]

STAGE_EXTRAS_NORMALIZE = {
    "四龙套": "龙套", "四白龙套": "龙套", "四上手": "龙套",
    "二童儿": "童儿", "二老军": "老军",
}

GENRE_CANONICAL_RULES = [
    ("历史戏", ["历史", "战争", "三国", "汉", "唐", "宋", "明", "清", "逐鹿", "征战"]),
    ("公案戏", ["公案", "审", "狱", "告状", "冤", "执法"]),
    ("家庭戏", ["家庭", "婆媳", "夫妻", "婆媳", "伦理", "闺门"]),
    ("神话戏", ["神", "仙", "妖", "怪", "天庭"]),
    ("喜剧", ["喜剧", "诙谐", "滑稽", "玩笑"]),
]

EMOTION_KEYWORDS = {
    "紧张": ["急", "慌", "惧", "怕", "惊", "紧", "危"],
    "愤怒": ["怒", "恨", "呸", "骂", "恼", "气"],
    "沉稳": ["静", "稳", "缓缓", "从容", "镇定"],
    "悲伤": ["哭", "叹", "悲", "泪", "哀", "痛"],
    "得意": ["哈哈", "喜", "笑", "得意", "胜"],
    "焦虑": ["忧", "虑", "恐", "只恐", "奈何"],
}


def make_char_id(doc_id: str, name: str) -> str:
    name = normalize_text(name)
    if not name:
        return ""
    digest = hashlib.sha1(f"{doc_id}|{name}".encode("utf-8")).hexdigest()[:10]
    return f"{doc_id}__char__{digest}"


def make_scene_id(doc_id: str, scene_index: int) -> str:
    return f"{doc_id}__scene__{int(scene_index):03d}"


def make_line_id(doc_id: str, line_no: int) -> str:
    return f"{doc_id}__line__{int(line_no):06d}"


def build_entity_alias_map(role_names: set[str], dialogues: list[dict]) -> dict[str, str]:
    """alias -> canonical role name within one play."""
    canon_names = {normalize_text(n) for n in role_names if normalize_text(n)}
    alias_map: dict[str, str] = {}
    for alias, canon in STATIC_CHARACTER_ALIASES:
        if canon in canon_names:
            alias_map[alias] = canon
    for name in sorted(canon_names, key=len, reverse=True):
        if len(name) >= 2 and name not in alias_map:
            alias_map[name] = name
    for row in dialogues:
        text = strip_html_markup(row.get("text", ""))
        for alias, canon in STATIC_CHARACTER_ALIASES:
            if alias in text and canon in canon_names:
                alias_map[alias] = canon
    return alias_map


def resolve_character_name(name: str, alias_map: dict[str, str], role_names: set[str]) -> str:
    name = normalize_text(name)
    if not name:
        return ""
    if name in alias_map:
        return alias_map[name]
    if name in STAGE_EXTRAS_NORMALIZE:
        return STAGE_EXTRAS_NORMALIZE[name]
    if name in role_names or name in COMMON_STAGE_SPEAKERS:
        return name
    return name


def build_entity_aliases_table(doc_id: str, doc_title: str, alias_map: dict[str, str], role_names: set[str]) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for alias, canon in sorted(alias_map.items()):
        if alias == canon:
            continue
        if canon not in role_names and canon not in COMMON_STAGE_SPEAKERS:
            continue
        key = (alias, canon)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "doc_id": doc_id,
            "doc_title": doc_title,
            "alias_name": alias,
            "canonical_name": canon,
            "char_id": make_char_id(doc_id, canon),
            "alias_char_id": make_char_id(doc_id, alias),
            "derived_by": "rule",
        })
    return rows


def infer_emotion_tag(text: str, cue: str = "") -> str:
    payload = f"{normalize_text(cue)} {strip_html_markup(text)}"
    if not payload.strip():
        return ""
    scores: dict[str, int] = {}
    for label, keys in EMOTION_KEYWORDS.items():
        scores[label] = sum(payload.count(k) for k in keys)
    if not scores or max(scores.values()) <= 0:
        return ""
    return max(scores.items(), key=lambda kv: kv[1])[0]


def infer_addressee(
    speaker: str,
    text: str,
    role_names: set[str],
    prev_speaker: str,
    alias_map: dict[str, str],
) -> str:
    text = strip_html_markup(text)
    if not text:
        return ""
    candidates = set(role_names) | COMMON_STAGE_SPEAKERS
    mentions = extract_entity_mentions(text, candidates - {speaker})
    resolved = [resolve_character_name(m, alias_map, role_names) for m in mentions]
    resolved = [m for m in resolved if m and m != speaker]
    if resolved:
        return resolved[0]
    if any(k in text for k in ["启禀", "报与", "参见", "禀"]):
        for title in ("丞相", "将军", "元帅", "陛下", "主公", "大人"):
            if title in text:
                for name in role_names:
                    if title in normalize_text(name) or name in {"诸葛亮", "司马懿", "赵云"}:
                        return resolve_character_name(name, alias_map, role_names)
        if "丞相" in text and "诸葛亮" in role_names:
            return "诸葛亮"
    if prev_speaker and prev_speaker != speaker:
        return prev_speaker
    return ""


def is_key_line_row(row: dict) -> bool:
    text = strip_html_markup(row.get("text", ""))
    cue = normalize_text(row.get("cue", ""))
    if not text:
        return False
    if row.get("row_type") == "lyric":
        return True
    if any(k in cue for k in ["西皮", "二黄", "慢板", "摇板", "二六", "原板"]):
        return True
    if any(k in text for k in ["启禀", "命", "斩", "杀", "退兵", "收兵", "如何", "为何", "敢尔"]):
        return True
    return len(text) >= 18


def enrich_dialogue_rows(
    dialogues: list[dict],
    doc_id: str,
    play_title: str,
    role_names: set[str],
    alias_map: dict[str, str],
) -> list[dict]:
    prev_speaker = ""
    by_scene_prev: dict[int, str] = {}
    for row in dialogues:
        idx = int(row.get("scene_index", 0) or 0)
        row["doc_id"] = doc_id
        row["doc_title"] = play_title
        row["line_id"] = make_line_id(doc_id, int(row.get("line_no", 0) or 0))
        row["scene_id"] = make_scene_id(doc_id, idx)
        speaker_raw = normalize_text(row.get("speaker", ""))
        speaker = resolve_character_name(speaker_raw, alias_map, role_names)
        row["speaker"] = speaker
        row["speaker_char_id"] = make_char_id(doc_id, speaker) if speaker else ""
        text = strip_html_markup(row.get("text", ""))
        row["text"] = text
        cue = normalize_text(row.get("cue", ""))
        row["entity_tags"] = "；".join(
            sorted(
                resolve_character_name(m, alias_map, role_names)
                for m in extract_entity_mentions(text, role_names | COMMON_STAGE_SPEAKERS - {speaker})
                if resolve_character_name(m, alias_map, role_names)
            )
        )
        row["emotion_tag"] = infer_emotion_tag(text, cue)
        row["emotion_derived_by"] = "rule" if row.get("emotion_tag") else ""
        prev = by_scene_prev.get(idx, prev_speaker)
        row["target"] = infer_addressee(speaker, text, role_names, prev, alias_map)
        row["target_derived_by"] = "rule" if row.get("target") else ""
        row["target_char_id"] = make_char_id(doc_id, row["target"]) if row.get("target") else ""
        row["speech_act"] = row.get("speech_act", "")
        row["action_tags"] = "；".join(extract_action_tags(text, cue))
        row["is_key_line"] = 1 if is_key_line_row(row) else 0
        row.setdefault("llm_confidence", "")
        if speaker and row.get("row_type") in {"dialogue", "lyric"}:
            by_scene_prev[idx] = speaker
            prev_speaker = speaker
    return dialogues


def enrich_scene_rows(scenes: list[dict], doc_id: str, play_title: str) -> list[dict]:
    for scene in scenes:
        idx = int(scene.get("scene_index", 0) or 0)
        scene["doc_id"] = doc_id
        scene["doc_title"] = play_title
        scene["scene_id"] = make_scene_id(doc_id, idx)
        line_count = max(int(scene.get("line_count", 0) or 0), 1)
        dialogue_count = int(scene.get("dialogue_count", 0) or 0)
        lyric_count = int(scene.get("lyric_count", 0) or 0)
        span = max(int(scene.get("end_line", 0) or 0) - int(scene.get("start_line", 0) or 0) + 1, 1)
        scene["dialogue_density"] = round(dialogue_count / span, 4)
        scene["lyric_density"] = round(lyric_count / span, 4)
        scene["speech_density"] = round((dialogue_count + lyric_count) / span, 4)
        try:
            scene["tension_level"] = int(scene.get("tension_level", 0) or 0)
        except Exception:
            scene["tension_level"] = simple_tension_score([])
    return scenes


def enrich_role_rows(
    roles: list[dict],
    dialogues: list[dict],
    relations_aggregated: list[dict],
    doc_id: str,
    play_title: str,
    alias_map: dict[str, str],
) -> list[dict]:
    line_counter: Counter[str] = Counter()
    scene_counter: dict[str, set[int]] = defaultdict(set)
    degree: Counter[str] = Counter()
    for row in dialogues:
        sp = resolve_character_name(row.get("speaker", ""), alias_map, {r.get("role_name", "") for r in roles})
        if not sp:
            continue
        if row.get("row_type") in {"dialogue", "lyric"} and normalize_text(row.get("text", "")):
            line_counter[sp] += 1
            scene_counter[sp].add(int(row.get("scene_index", 0) or 0))
    for rel in relations_aggregated:
        for end in ("source", "target"):
            name = normalize_text(rel.get(end, ""))
            if name:
                degree[name] += int(rel.get("weight", 1) or 1)
    max_degree = max(degree.values()) if degree else 1
    for role in roles:
        name = normalize_text(role.get("role_name", ""))
        canon = resolve_character_name(name, alias_map, {name})
        role["doc_id"] = doc_id
        role["doc_title"] = play_title
        role["char_id"] = make_char_id(doc_id, canon)
        role["alias_names"] = "；".join(
            sorted(a for a, c in alias_map.items() if c == canon and a != canon)
        )
        role["line_count"] = int(line_counter.get(canon, 0))
        scenes_present = sorted(scene_counter.get(canon, set()))
        role["scene_coverage"] = "；".join(str(s) for s in scenes_present)
        role["scene_count_present"] = len(scenes_present)
        role["centrality_hint"] = round(degree.get(canon, 0) / max_degree, 4) if canon else 0.0
    return roles


def normalize_relation_endpoints(rows: list[dict], alias_map: dict[str, str], role_names: set[str]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        row = dict(row)
        row["source"] = resolve_character_name(row.get("source", ""), alias_map, role_names)
        row["target"] = resolve_character_name(row.get("target", ""), alias_map, role_names)
        if row["source"] and row["target"] and row["source"] != row["target"]:
            out.append(row)
    return out


def refine_relations_aggregated_semantic(rows: list[dict]) -> list[dict]:
    """Prefer汇报/命令/对抗 over低信息量对话 when sharing an endpoint pair."""
    priority = {"汇报": 5, "命令": 4, "对抗": 4, "评价": 3, "提及": 2, "同场共现": 2, "协助": 3, "对话": 1}
    bucket: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        key = (
            normalize_text(row.get("doc_title", "")),
            normalize_text(row.get("source", "")),
            normalize_text(row.get("target", "")),
        )
        bucket[key].append(row)
    merged: list[dict] = []
    for _, group in bucket.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        group = sorted(
            group,
            key=lambda r: (
                priority.get(normalize_text(r.get("relation_type", "")), 0),
                int(r.get("weight", 0) or 0),
            ),
            reverse=True,
        )
        top = dict(group[0])
        top["weight"] = sum(int(g.get("weight", 1) or 1) for g in group)
        top["merged_relation_types"] = "；".join(
            sorted({normalize_text(g.get("relation_type", "")) for g in group if normalize_text(g.get("relation_type", ""))})
        )
        merged.append(top)
    return sorted(merged, key=lambda x: (-int(x.get("weight", 0)), x.get("source", ""), x.get("target", "")))


def select_dialogue_llm_candidates(dialogues: list[dict], max_lines: int) -> list[dict]:
    ranked: list[tuple[int, dict]] = []
    for row in dialogues:
        if normalize_text(row.get("row_type", "")) not in {"dialogue", "lyric"}:
            continue
        text = strip_html_markup(row.get("text", ""))
        if len(text) < 4:
            continue
        score = 0
        if is_key_line_row(row):
            score += 3
        if row.get("row_type") == "lyric":
            score += 2
        if len(text) >= 16:
            score += 1
        if any(k in text for k in ["启禀", "参见", "如何", "为何", "命", "斩"]):
            score += 2
        ranked.append((score, row))
    ranked.sort(key=lambda x: (-x[0], int(x[1].get("line_no", 0) or 0)))
    return [row for _, row in ranked[:max_lines]]


def llm_enrich_dialogues(
    session: requests.Session,
    llm: LLMConfig,
    dialogues: list[dict],
    role_names: set[str],
    alias_map: dict[str, str],
    doc_title: str,
) -> list[dict]:
    if not llm.enabled or not llm.enrich_dialogues:
        return dialogues
    candidates = select_dialogue_llm_candidates(dialogues, llm.max_dialogue_llm_lines)
    if not candidates:
        return dialogues
    by_line = {int(r.get("line_no", 0) or 0): r for r in dialogues}
    system = (
        "你是京剧台词语用分析助手。根据说话人、唱腔提示与台词，推断受话人、情绪与言语行为。"
        "只返回严格JSON对象，不要解释。"
    )
    role_list = "、".join(sorted(role_names)[:24])
    for start in range(0, len(candidates), llm.dialogue_batch_size):
        batch = candidates[start : start + llm.dialogue_batch_size]
        lines_payload = []
        for row in batch:
            lines_payload.append({
                "line_no": int(row.get("line_no", 0) or 0),
                "scene_index": int(row.get("scene_index", 0) or 0),
                "speaker": normalize_text(row.get("speaker", "")),
                "cue": normalize_text(row.get("cue", "")),
                "text": strip_html_markup(row.get("text", ""))[:220],
            })
        user = f"""
剧名：{doc_title}
可选角色：{role_list}

请分析下列台词，输出 JSON：
{{
  "lines": [
    {{
      "line_no": 1,
      "target": "受话角色名，无法判断则空字符串",
      "emotion_tag": "怒|喜|悲|惊|疑|惧|平|厉|哀|其他",
      "speech_act": "命令|汇报|质问|陈述|请求|劝阻|威胁|安慰|调侃|其他",
      "confidence": 0.0
    }}
  ]
}}
台词列表：
{json.dumps(lines_payload, ensure_ascii=False, indent=2)}
要求：
- target 必须从可选角色中选，或为空；不要把「丞相」等头衔当作角色名。
- 仅依据台词推断，不要编造剧情。
"""
        try:
            result = llm_chat_json(session, llm, system, user)
            if not isinstance(result, dict):
                continue
            for item in result.get("lines", []) or []:
                if not isinstance(item, dict):
                    continue
                line_no = int(item.get("line_no", 0) or 0)
                row = by_line.get(line_no)
                if not row:
                    continue
                target = resolve_character_name(_llm_text(item.get("target", "")), alias_map, role_names)
                emotion = _llm_text(item.get("emotion_tag", ""))
                speech_act = _llm_text(item.get("speech_act", ""))
                conf = item.get("confidence", "")
                if target:
                    row["target"] = target
                    row["target_char_id"] = make_char_id(row.get("doc_id", ""), target)
                    row["target_derived_by"] = "llm"
                if emotion:
                    row["emotion_tag"] = emotion
                    row["emotion_derived_by"] = "llm"
                if speech_act:
                    row["speech_act"] = speech_act
                if conf not in ("", None):
                    row["llm_confidence"] = conf
        except Exception:
            continue
    return dialogues


def llm_refine_relations_aggregated(
    session: requests.Session,
    llm: LLMConfig,
    rows: list[dict],
    role_names: set[str],
    doc_title: str,
) -> list[dict]:
    if not llm.enabled or not llm.enrich_relations or not rows:
        return rows
    ranked = sorted(rows, key=lambda r: (-int(r.get("weight", 0) or 0), r.get("source", ""), r.get("target", "")))
    subset = ranked[: llm.max_relation_refine_pairs]
    if not subset:
        return rows
    system = (
        "你是京剧人物关系图谱专家。请对聚合后的人物关系边进行语义校正："
        "统一关系类型、合并重复语义、提高证据表述质量。只返回严格JSON。"
    )
    payload = [
        {
            "source": r.get("source", ""),
            "target": r.get("target", ""),
            "relation_type": r.get("relation_type", ""),
            "merged_relation_types": r.get("merged_relation_types", ""),
            "weight": int(r.get("weight", 1) or 1),
            "evidence": strip_html_markup(r.get("evidence", ""))[:120],
        }
        for r in subset
    ]
    user = f"""
剧名：{doc_title}
主要角色：{"、".join(sorted(role_names)[:20])}

输入关系边：
{json.dumps(payload, ensure_ascii=False, indent=2)}

请输出 JSON：
{{
  "relations": [
    {{
      "source": "",
      "target": "",
      "relation_type": "对抗|命令|协助|汇报|评价|提及|同盟|旁观|劝阻|试探|对话|同场共现",
      "merged_relation_types": "；分隔的原始类型",
      "weight": 1,
      "evidence": "一句证据",
      "confidence": 0.0
    }}
  ]
}}
要求：
- 保留 source/target 与输入一致（仅做别名归一，不要改人）。
- relation_type 选最能代表主导互动的一种。
- weight 可微调为 1-10 整数。
"""
    try:
        result = llm_chat_json(session, llm, system, user)
        if not isinstance(result, dict):
            return rows
        refined_list = result.get("relations", []) or []
        refined_map: dict[tuple[str, str], dict] = {}
        for item in refined_list:
            if not isinstance(item, dict):
                continue
            src = normalize_text(item.get("source", ""))
            tgt = normalize_text(item.get("target", ""))
            if not (src and tgt and src != tgt):
                continue
            key = (src, tgt)
            refined_map[key] = {
                "relation_type": normalize_relation_type(item.get("relation_type", "")),
                "merged_relation_types": _llm_text(item.get("merged_relation_types", "")),
                "weight": int(item.get("weight", 1) or 1),
                "evidence": _llm_text(item.get("evidence", "")),
                "derived_by": "llm_refined",
                "llm_confidence": item.get("confidence", ""),
            }
        if not refined_map:
            return rows
        out: list[dict] = []
        for row in rows:
            key = (normalize_text(row.get("source", "")), normalize_text(row.get("target", "")))
            patch = refined_map.get(key)
            if patch:
                merged_row = dict(row)
                merged_row.update(patch)
                out.append(merged_row)
            else:
                out.append(row)
        return out
    except Exception:
        return rows


def llm_enrich_roles_batched(
    session: requests.Session,
    llm: LLMConfig,
    roles: list[dict],
    dialogues: list[dict],
    doc_title: str,
) -> list[dict]:
    if not llm.enabled or not roles:
        return roles
    system = (
        "你是京剧角色分析专家。请根据角色表与台词证据，批量补全角色画像。"
        "只返回严格JSON对象，字段 roles 为数组。"
    )
    enriched_map: dict[str, dict] = {}
    for start in range(0, len(roles), llm.roles_batch_size):
        chunk = roles[start : start + llm.roles_batch_size]
        role_blocks = []
        for role in chunk:
            name = role.get("role_name", "")
            evidence = collect_role_evidence(dialogues, name, max_lines=llm.max_role_evidence)
            role_blocks.append({
                "role_name": name,
                "role_type_raw": role.get("role_type_raw", ""),
                "evidence": evidence[:500],
            })
        user = f"""
剧名：{doc_title}

角色证据块：
{json.dumps(role_blocks, ensure_ascii=False, indent=2)}

请输出 JSON：
{{
  "roles": [
    {{
      "role_name": "",
      "role_type_inferred": "",
      "gender_inferred": "",
      "age_inferred": "",
      "status_identity": "",
      "personality_tags": [""],
      "behavior_tags": [""],
      "speech_style": [""],
      "narrative_function": "主角|配角|功能性角色",
      "evidence": "",
      "confidence": 0.0
    }}
  ]
}}
"""
        try:
            result = llm_chat_json(session, llm, system, user)
            if not isinstance(result, dict):
                continue
            for item in result.get("roles", []) or []:
                if not isinstance(item, dict):
                    continue
                name = normalize_text(item.get("role_name", ""))
                if name:
                    enriched_map[name] = item
        except Exception as e:
            for role in chunk:
                role.setdefault("llm_error", str(e))
    out: list[dict] = []
    for role in roles:
        row = dict(role)
        name = normalize_text(role.get("role_name", ""))
        item = enriched_map.get(name)
        if not item:
            out.append(row)
            continue
        row["role_type_inferred"] = _llm_text(item.get("role_type_inferred", row.get("role_type_inferred", "")))
        row["gender_inferred"] = _llm_text(item.get("gender_inferred", row.get("gender_inferred", "")))
        row["age_inferred"] = _llm_text(item.get("age_inferred", row.get("age_inferred", "")))
        row["status_identity"] = _llm_text(item.get("status_identity", row.get("status_identity", "")))
        row["personality_tags"] = "；".join(_llm_items(item.get("personality_tags", [])))
        row["behavior_tags"] = "；".join(_llm_items(item.get("behavior_tags", [])))
        row["speech_style"] = "；".join(_llm_items(item.get("speech_style", [])))
        row["narrative_function"] = _llm_text(item.get("narrative_function", ""))
        row["evidence"] = _llm_text(item.get("evidence", row.get("evidence", "")))
        row["confidence"] = item.get("confidence", row.get("confidence", ""))
        out.append(row)
    return out


def build_theme_pairs(
    doc_id: str,
    doc_title: str,
    themes: list[dict],
    scenes: list[dict],
) -> list[dict]:
    scene_themes: dict[int, set[str]] = defaultdict(set)
    for theme in themes:
        label = normalize_theme_label(theme.get("theme_label", theme.get("theme_label_raw", "")))
        if not label:
            continue
        scene_themes[int(theme.get("scene_index", 0) or 0)].add(label)
    for scene in scenes:
        idx = int(scene.get("scene_index", 0) or 0)
        for label in _llm_items(scene.get("theme_labels", "")):
            norm = normalize_theme_label(label)
            if norm:
                scene_themes[idx].add(norm)
    pair_counter: Counter[tuple[str, str]] = Counter()
    evidence_map: dict[tuple[str, str], str] = {}
    for idx, labels in scene_themes.items():
        ordered = sorted(labels)
        if len(ordered) < 2:
            continue
        scene_name = next((s.get("scene", "") for s in scenes if int(s.get("scene_index", 0)) == idx), "")
        for a, b in itertools.combinations(ordered, 2):
            key = (a, b)
            pair_counter[key] += 1
            if key not in evidence_map:
                evidence_map[key] = f"{scene_name or idx}"
    rows: list[dict] = []
    for (a, b), weight in pair_counter.items():
        rows.append({
            "doc_id": doc_id,
            "doc_title": doc_title,
            "theme_a": a,
            "theme_b": b,
            "cooccurrence_weight": weight,
            "evidence": evidence_map.get((a, b), ""),
            "derived_by": "scene_cooccurrence",
        })
    return sorted(rows, key=lambda r: (-int(r["cooccurrence_weight"]), r["theme_a"], r["theme_b"]))


def build_narrative_curve(doc_id: str, doc_title: str, scenes: list[dict]) -> list[dict]:
    rows: list[dict] = []
    max_tension = max((int(s.get("tension_level", 0) or 0) for s in scenes), default=1) or 1
    for scene in sorted(scenes, key=lambda s: int(s.get("scene_index", 0) or 0)):
        idx = int(scene.get("scene_index", 0) or 0)
        tension = int(scene.get("tension_level", 0) or 0)
        rows.append({
            "doc_id": doc_id,
            "doc_title": doc_title,
            "scene_id": scene.get("scene_id", make_scene_id(doc_id, idx)),
            "scene_index": idx,
            "scene": scene.get("scene", ""),
            "conflict_stage": scene.get("conflict_stage", ""),
            "scene_function": scene.get("scene_function", ""),
            "tension_level": tension,
            "tension_norm": round(tension / max_tension, 4),
            "dialogue_density": scene.get("dialogue_density", 0),
            "lyric_density": scene.get("lyric_density", 0),
            "speech_density": scene.get("speech_density", 0),
            "narrative_turning_point": scene.get("narrative_turning_point", ""),
            "is_climax": 1 if tension >= max_tension - 1 and idx > 0 else 0,
            "derived_by": "scene_metrics",
        })
    return rows


def compute_network_metrics(doc_id: str, doc_title: str, relations_aggregated: list[dict]) -> list[dict]:
    degree: Counter[str] = Counter()
    weighted: Counter[str] = Counter()
    for rel in relations_aggregated:
        w = int(rel.get("weight", 1) or 1)
        for end in ("source", "target"):
            name = normalize_text(rel.get(end, ""))
            if not name:
                continue
            degree[name] += 1
            weighted[name] += w
    if not degree:
        return []
    max_deg = max(degree.values())
    max_w = max(weighted.values()) or 1
    rows = []
    for name, deg in degree.most_common():
        rows.append({
            "doc_id": doc_id,
            "doc_title": doc_title,
            "character": name,
            "char_id": make_char_id(doc_id, name),
            "degree": deg,
            "weighted_degree": weighted[name],
            "degree_centrality": round(deg / max_deg, 4),
            "strength_centrality": round(weighted[name] / max_w, 4),
            "derived_by": "relations_aggregated",
        })
    return rows


def compute_parse_quality_score(
    md_text: str,
    scenes: list[dict],
    dialogues: list[dict],
    roles: list[dict],
    relations: list[dict],
) -> float:
    score = 0.0
    body_scenes = [s for s in scenes if int(s.get("scene_index", 0) or 0) > 0]
    effective_lines = sum(
        1 for d in dialogues
        if d.get("row_type") in {"dialogue", "lyric"}
        and normalize_text(d.get("text", ""))
        and not is_placeholder_dialogue_row(d)
    )
    if len(body_scenes) >= 2:
        score += 0.22
    elif len(body_scenes) == 1:
        score += 0.08
    if effective_lines >= 40:
        score += 0.28
    elif effective_lines >= 15:
        score += 0.18
    elif effective_lines >= 5:
        score += 0.08
    if len(roles) >= 3:
        score += 0.12
    elif len(roles) >= 1:
        score += 0.05
    if len(relations) >= 8:
        score += 0.13
    elif len(relations) >= 2:
        score += 0.07
    total = max(len(dialogues), 1)
    html_rows = sum(1 for d in dialogues if "<table" in normalize_text(d.get("text", "")).lower())
    placeholder_rows = sum(1 for d in dialogues if is_placeholder_dialogue_row(d))
    penalty = min(0.25, (html_rows / total) * 0.12 + (placeholder_rows / total) * 0.18)
    score += max(0.0, 0.25 - penalty)
    return round(min(1.0, max(0.0, score)), 3)


def finalize_structured_package(
    structured: dict,
    md_text: str,
    play_title: str,
    doc_id: str,
    llm_cfg: LLMConfig | None = None,
    llm_session: requests.Session | None = None,
) -> dict:
    roles = structured.get("roles", [])
    scenes = structured.get("scenes", [])
    dialogues = structured.get("dialogues", [])
    performances = structured.get("performances", [])
    relations = structured.get("relations", [])
    relations_aggregated = structured.get("relations_aggregated", [])
    themes = structured.get("themes", [])
    themes_aggregated = structured.get("themes_aggregated", [])

    role_names = {normalize_text(r.get("role_name", "")) for r in roles if normalize_text(r.get("role_name", ""))}
    alias_map = build_entity_alias_map(role_names, dialogues)

    dialogues = enrich_dialogue_rows(dialogues, doc_id, play_title, role_names, alias_map)
    if llm_cfg and llm_cfg.enabled and llm_session is not None:
        dialogues = llm_enrich_dialogues(
            llm_session, llm_cfg, dialogues, role_names, alias_map, play_title
        )
    scenes = enrich_scene_rows(scenes, doc_id, play_title)
    roles = enrich_role_rows(roles, dialogues, relations_aggregated, doc_id, play_title, alias_map)
    relations = normalize_relation_endpoints(relations, alias_map, role_names)
    relations = dedupe_rows(
        relations,
        ["doc_title", "scene_index", "scene", "source", "target", "relation_type", "derived_by"],
    )
    relations_aggregated = normalize_relation_endpoints(relations_aggregated, alias_map, role_names)
    relations_aggregated = refine_relations_aggregated_semantic(
        aggregate_relations(relations) if relations else relations_aggregated
    )
    if llm_cfg and llm_cfg.enabled and llm_session is not None:
        relations_aggregated = llm_refine_relations_aggregated(
            llm_session, llm_cfg, relations_aggregated, role_names, play_title
        )
        relations_aggregated = refine_relations_aggregated_semantic(relations_aggregated)

    entity_aliases = build_entity_aliases_table(doc_id, play_title, alias_map, role_names)
    enrich_theme_role_links(themes, scenes)
    theme_pairs = build_theme_pairs(doc_id, play_title, themes, scenes)
    narrative_curve = build_narrative_curve(doc_id, play_title, scenes)
    network_metrics = compute_network_metrics(doc_id, play_title, relations_aggregated)
    attach_evidence_line_ids(relations, dialogues)

    for perf in performances:
        sp = resolve_character_name(perf.get("speaker", ""), alias_map, role_names)
        perf["speaker"] = sp
        perf["speaker_char_id"] = make_char_id(doc_id, sp) if sp else ""
        perf["line_id"] = make_line_id(doc_id, int(perf.get("line_no", 0) or 0))
        perf["scene_id"] = make_scene_id(doc_id, int(perf.get("scene_index", 0) or 0))

    parse_quality = compute_parse_quality_score(md_text, scenes, dialogues, roles, relations)
    metadata = dict(structured.get("metadata", {}))
    genre_norm = normalize_genre_hint(metadata.get("genre_hint", ""))
    metadata.update({
        "doc_id": doc_id,
        "play_title": play_title,
        "text_length": len(md_text),
        "genre_hint": genre_norm or metadata.get("genre_hint", ""),
        "parse_quality_score": parse_quality,
        "parse_quality_label": "high" if parse_quality >= 0.75 else ("medium" if parse_quality >= 0.45 else "low"),
        "entity_alias_count": len(entity_aliases),
        "theme_pair_count": len(theme_pairs),
        "relation_count": len(relations),
        "aggregated_relation_count": len(relations_aggregated),
        "role_count": len(roles),
        "scene_count": len(scenes),
        "dialogue_count": len(dialogues),
        "performance_count": len(performances),
        "theme_count": len(themes),
        "aggregated_theme_count": len(themes_aggregated),
    })
    metadata["analysis_ready"] = is_analysis_ready(metadata)

    structured.update({
        "metadata": metadata,
        "roles": roles,
        "scenes": scenes,
        "dialogues": dialogues,
        "performances": performances,
        "relations": relations,
        "relations_aggregated": relations_aggregated,
        "themes": themes,
        "themes_aggregated": themes_aggregated,
        "entity_aliases": entity_aliases,
        "theme_pairs": theme_pairs,
        "narrative_curve": narrative_curve,
        "network_metrics": network_metrics,
    })
    return structured


def detect_location(texts: list[str]) -> str:
    joined = "；".join(normalize_text(t) for t in texts if normalize_text(t))
    for term in COMMON_LOCATION_TERMS:
        if term in joined:
            return term
    return ""


def detect_time_hint(texts: list[str]) -> str:
    joined = "；".join(normalize_text(t) for t in texts if normalize_text(t))
    for term in ["二更", "三更", "四更", "夜", "夜晚", "当晚", "次日", "清晨", "黎明", "午后", "黄昏", "更深"]:
        if term in joined:
            return term
    return ""


def detect_dominant_action(rows: list[dict]) -> str:
    text = " ".join(normalize_text(r.get("text", "")) for r in rows)
    cue = " ".join(normalize_text(r.get("cue", "")) for r in rows)
    score = {
        "命令": sum(text.count(k) + cue.count(k) for k in ["命", "令", "传", "快去", "唤", "着"]),
        "汇报": sum(text.count(k) for k in ["启禀", "报", "回禀", "禀"]),
        "对话": sum(text.count(k) for k in ["问", "答", "说", "道"]),
        "对峙": sum(text.count(k) for k in ["兵", "杀", "战", "退兵", "对敌", "夺"]),
        "抒情": sum(text.count(k) for k in ["哭", "叹", "思", "念", "忧", "病"]),
        "唱段": sum(cue.count(k) for k in ["西皮", "二黄", "慢板", "摇板", "二六板", "快板"]),
    }
    if not score:
        return ""
    return max(score.items(), key=lambda kv: kv[1])[0]


def detect_narrative_turning_point(rows: list[dict]) -> str:
    text = " ".join(normalize_text(r.get("text", "")) for r in rows)
    for term in ["转折", "误认", "发现", "决断", "冲突升级", "退兵", "失守", "救", "死", "病重", "空城", "认作", "惊疑"]:
        if term in text:
            return term
    return ""


def detect_main_event(summary: str, rows: list[dict]) -> str:
    s = normalize_text(summary)
    if s:
        return s[:80]
    for row in rows:
        txt = normalize_text(row.get("text", ""))
        if txt:
            return txt[:80]
    return ""


def normalize_scene_function(value: str, tension: int, idx: int, max_idx: int) -> str:
    v = normalize_text(value)
    if v:
        for term in SCENE_FUNCTION_ORDER:
            if term in v:
                return term
    if idx == 0:
        return "引子"
    if idx == max_idx:
        return "收束" if tension < 6 else "高潮"
    if tension >= 7:
        return "高潮"
    if tension >= 5:
        return "对峙"
    if tension >= 3:
        return "推进"
    return "过渡"


def normalize_conflict_stage(value: str, tension: int, idx: int, max_idx: int) -> str:
    v = normalize_text(value)
    if v in {"铺垫", "发展", "冲突", "高潮", "收束", "转折", "过渡"}:
        return v
    if idx == 0:
        return "铺垫"
    if idx == max_idx:
        return "收束" if tension < 6 else "高潮"
    if tension >= 7:
        return "高潮"
    if tension >= 5:
        return "冲突"
    if tension >= 3:
        return "发展"
    return "过渡"


def simple_tension_score(rows: list[dict]) -> int:
    text = " ".join(normalize_text(r.get("text", "")) for r in rows)
    score = 0
    for term in ["兵", "杀", "战", "惊", "病", "哭", "叹", "夺", "退兵", "失守", "空城", "认作", "误认", "危", "险"]:
        score += text.count(term)
    return min(10, score)


def fallback_scene_summary(rows: list[dict], max_chars: int = 120) -> str:
    snippets = []
    for r in rows:
        payload = analysis_payload(r)
        if payload and payload not in snippets:
            snippets.append(payload)
        if len(snippets) >= 3:
            break
    joined = "；".join(snippets)
    return joined[:max_chars]


def build_scene_enrichment_rows(
    scene_row: dict,
    rows: list[dict],
    max_scene_index: int,
    preface_meta: dict | None = None,
) -> dict:
    preface_meta = preface_meta or {}
    summary = normalize_text(scene_row.get("summary", "")) or fallback_scene_summary(rows)
    tension = scene_row.get("tension_level", "")
    try:
        tension = int(tension)
    except Exception:
        tension = simple_tension_score(rows)
    chars = []
    seen = set()
    for r in rows:
        sp = normalize_text(r.get("speaker", ""))
        if sp and sp not in seen:
            seen.add(sp)
            chars.append(sp)

    scene_row["characters_present"] = "；".join(chars)
    if not normalize_text(scene_row.get("key_characters", "")):
        scene_row["key_characters"] = "；".join(chars[:6])
    scene_row["line_count"] = len(rows)
    scene_row["dialogue_count"] = sum(1 for r in rows if r.get("row_type") == "dialogue")
    scene_row["lyric_count"] = sum(1 for r in rows if r.get("row_type") == "lyric")
    scene_row["stage_direction_count"] = sum(1 for r in rows if r.get("row_type") == "stage_direction")
    scene_row["narration_count"] = sum(1 for r in rows if r.get("row_type") == "narration")

    location = scene_row.get("location", "") or detect_location([summary] + [analysis_payload(r) for r in rows])
    time_hint = scene_row.get("time_hint", "") or detect_time_hint([summary] + [analysis_payload(r) for r in rows])
    dominant_action = scene_row.get("dominant_action", "") or detect_dominant_action(rows)
    main_event = scene_row.get("main_event", "") or detect_main_event(summary, rows)
    turning = scene_row.get("narrative_turning_point", "") or detect_narrative_turning_point(rows)
    scene_func = normalize_scene_function(scene_row.get("scene_function", ""), tension, int(scene_row.get("scene_index", 0) or 0), max_scene_index)
    conflict_stage = normalize_conflict_stage(scene_row.get("conflict_stage", ""), tension, int(scene_row.get("scene_index", 0) or 0), max_scene_index)

    scene_row["summary"] = summary
    scene_row["location"] = location
    scene_row["time_hint"] = time_hint
    scene_row["dominant_action"] = dominant_action
    scene_row["main_event"] = main_event
    scene_row["narrative_turning_point"] = turning
    scene_row["scene_function"] = scene_func
    scene_row["conflict_stage"] = conflict_stage
    scene_row["tension_level"] = tension

    if not normalize_text(scene_row.get("theme_labels", "")):
        scene_row["theme_labels"] = "；".join(simple_theme_fallback([summary, preface_meta.get("synopsis", ""), preface_meta.get("note_text", "")]))
    if not normalize_text(scene_row.get("key_characters", "")):
        scene_row["key_characters"] = "；".join(chars[:6])

    return scene_row


def llm_enrich_document_meta(
    session: requests.Session,
    llm: LLMConfig,
    preface_meta: dict,
    doc_title: str,
    doc_context: dict | None = None,
    dialogues: list[dict] | None = None,
) -> dict:
    if not llm.enabled:
        return preface_meta
    doc_context = doc_context or {}
    sample_lines: list[str] = []
    for row in (dialogues or [])[:80]:
        speaker = normalize_text(row.get("speaker", ""))
        cue = normalize_text(row.get("cue", ""))
        text = strip_html_markup(row.get("text", ""))
        if speaker or text:
            sample_lines.append(f"{speaker}（{cue}）{text}".strip())
    sample_text = "\n".join(sample_lines[:80])
    system = "你是中国京剧剧本信息抽取助手。请只返回严格JSON，不要输出解释。"
    user = f"""
剧名：{doc_title}
集合：{doc_context.get('collection_name', '')}（{doc_context.get('collection_code', '')}）
作品编码：{doc_context.get('work_code', '')}
原标题/别名行：{preface_meta.get('alias_text', '')}
剧情简介（规则抽取）：{preface_meta.get('synopsis', '')}
注释（规则抽取）：{preface_meta.get('note_text', '')}
已有年代线索：{preface_meta.get('period_hint', '')}
已有戏种线索：{preface_meta.get('genre_hint', '')}

正文台词样例（前80条）：
{sample_text}

请输出JSON对象，字段固定为：
{{
  "aliases": [""],
  "alias_text": "",
  "period_hint": "",
  "genre_hint": "历史戏|家庭戏|公案戏|战争戏|神话戏|喜剧|其他",
  "synopsis": "",
  "note_text": "",
  "doc_tags": [""],
  "main_conflict": "一句话概括核心冲突",
  "confidence": 0.0
}}
要求：
- aliases 只放与剧名确实有关的别名。
- period_hint 与 genre_hint 优先使用已有线索并校正。
- synopsis 40-120字；note_text 概括版本/表演注释要点。
- doc_tags 2-6个，如忠义、权谋、战争、家庭、公案、离散、情爱、喜剧。
- 仅依据前言与样例推断，不要编造。
"""
    try:
        result = llm_chat_json(session, llm, system, user)
        if isinstance(result, dict):
            merged = dict(preface_meta)
            aliases = _llm_items(result.get("aliases", merged.get("aliases", [])))
            merged["aliases"] = aliases or merged.get("aliases", [])
            if result.get("alias_text"):
                merged["alias_text"] = _llm_text(result.get("alias_text"))
            if result.get("period_hint"):
                merged["period_hint"] = _llm_text(result.get("period_hint"))
            if result.get("genre_hint"):
                merged["genre_hint"] = _llm_text(result.get("genre_hint"))
            if result.get("synopsis"):
                merged["synopsis"] = _llm_text(result.get("synopsis"))
            if result.get("note_text"):
                merged["note_text"] = _llm_text(result.get("note_text"))
            merged["doc_tags"] = _llm_items(result.get("doc_tags", []))
            if result.get("main_conflict"):
                merged["main_conflict"] = _llm_text(result.get("main_conflict"))
            merged["doc_llm_confidence"] = result.get("confidence", "")
            merged["genre_hint"] = normalize_genre_hint(merged.get("genre_hint", ""))
            return merged
    except Exception:
        pass
    return preface_meta


def llm_enrich_scenes_and_relations(
    session: requests.Session,
    llm: LLMConfig,
    scenes: list[dict],
    dialogues: list[dict],
    doc_title: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    if not llm.enabled or not scenes:
        return scenes, [], []

    by_scene: dict[int, list[dict]] = defaultdict(list)
    for row in dialogues:
        by_scene[int(row["scene_index"])].append(row)

    system = (
        "你是中国京剧剧本结构化分析助手。请根据场次文本输出严格JSON，用于人物关系、主题、叙事结构分析。"
        "只返回JSON对象，不要输出解释文字。"
    )

    enriched_scenes: list[dict] = []
    relation_rows: list[dict] = []
    theme_rows: list[dict] = []
    llm_failed = False

    for scene in scenes:
        idx = int(scene["scene_index"])
        rows = by_scene.get(idx, [])
        transcript = _scene_excerpt(rows, max_lines=llm.max_scene_lines, max_chars=llm.max_input_chars)
        characters = sorted({normalize_text(r.get("speaker", "")) for r in rows if normalize_text(r.get("speaker", ""))})
        row = dict(scene)

        if not transcript.strip() or llm_failed:
            enriched_scenes.append(row)
            continue

        user = f"""
剧名：{doc_title}
场次：{scene.get('scene', '')}
本场角色：{ '、'.join(characters) if characters else '未知' }

本场文本：
{transcript}

请输出JSON对象，字段固定为：
{{
  "summary": "本场简要摘要，40-80字",
  "conflict_stage": "铺垫|发展|冲突|高潮|收束|转折|过渡",
  "tension_level": 1,
  "scene_function": "引子|铺垫|推进|对峙|高潮|反转|收束|过渡",
  "location": "",
  "time_hint": "",
  "main_event": "",
  "dominant_action": "",
  "narrative_turning_point": "",
  "key_characters": ["角色A", "角色B"],
  "theme_labels": ["主题1", "主题2"],
  "relations": [
    {{
      "source": "角色A",
      "target": "角色B",
      "relation_type": "对抗|命令|协助|汇报|评价|提及|同盟|旁观|劝阻|试探",
      "weight": 1,
      "evidence": "对应证据短句"
    }}
  ]
}}
要求：
- summary 需概括本场主要冲突或推进。
- key_characters 只保留真正参与推进剧情的角色。
- theme_labels 选 1-4 个最核心主题。
- narrative_turning_point 简述本场是否包含转折、发现、决断、冲突升级等关键点。
- relations 尽量给出显式互动，不要把无关共现都列出来。
"""
        try:
            result = llm_chat_json(session, llm, system, user)
            if isinstance(result, dict):
                row["summary"] = _llm_text(result.get("summary", row.get("summary", "")))
                row["conflict_stage"] = _llm_text(result.get("conflict_stage", row.get("conflict_stage", "")))
                row["tension_level"] = result.get("tension_level", row.get("tension_level", 0))
                row["scene_function"] = _llm_text(result.get("scene_function", row.get("scene_function", "")))
                row["location"] = _llm_text(result.get("location", row.get("location", "")))
                row["time_hint"] = _llm_text(result.get("time_hint", row.get("time_hint", "")))
                row["main_event"] = _llm_text(result.get("main_event", row.get("main_event", "")))
                row["dominant_action"] = _llm_text(result.get("dominant_action", row.get("dominant_action", "")))
                if result.get("narrative_turning_point"):
                    row["narrative_turning_point"] = _llm_text(result.get("narrative_turning_point"))
                row["key_characters"] = "；".join(_llm_items(result.get("key_characters", [])))
                row["theme_labels"] = "；".join(_llm_items(result.get("theme_labels", [])))
                for rel in result.get("relations", []) or []:
                    if not isinstance(rel, dict):
                        continue
                    source = _llm_text(rel.get("source", ""))
                    target = _llm_text(rel.get("target", ""))
                    relation_type = normalize_relation_type(rel.get("relation_type", ""))
                    if not (source and target and relation_type):
                        continue
                    relation_rows.append({
                        "doc_title": doc_title,
                        "scene_index": idx,
                        "scene": scene.get("scene", ""),
                        "source": source,
                        "target": target,
                        "relation_type_raw": _llm_text(rel.get("relation_type", "")),
                        "relation_type": relation_type,
                        "weight": rel.get("weight", 1),
                        "evidence": _llm_text(rel.get("evidence", "")),
                        "derived_by": "llm",
                    })
                for theme in _llm_items(result.get("theme_labels", [])):
                    norm_theme = normalize_theme_label(theme)
                    if not norm_theme:
                        continue
                    theme_rows.append({
                        "doc_title": doc_title,
                        "scene_index": idx,
                        "scene": scene.get("scene", ""),
                        "theme_label_raw": theme,
                        "theme_label": norm_theme,
                        "theme_strength": 1,
                        "evidence": row.get("summary", ""),
                        "derived_by": "llm",
                    })
        except Exception as e:
            row["llm_error"] = str(e)
            if any(code in str(e) for code in ["402", "401", "403", "Payment Required"]):
                llm_failed = True

        enriched_scenes.append(row)

    return enriched_scenes, relation_rows, theme_rows


def parse_markdown_structure(
    md_text: str,
    source_meta: dict,
    llm_cfg: LLMConfig | None = None,
    llm_session: requests.Session | None = None,
) -> dict:
    lines = md_text.splitlines()
    play_title = extract_title(lines)
    rel_path = Path(source_meta.get("relative_path", source_meta.get("source_file", play_title or "doc")))
    doc_id = stable_data_id(rel_path)
    preface_meta = extract_preface_metadata(lines)
    collection_meta = infer_collection_info(rel_path)
    doc_context = {"play_title": play_title, **collection_meta}

    roles, role_names = parse_role_section(lines)
    scenes, dialogues = parse_scenes_and_dialogues(lines, source_meta, role_names=role_names)
    dialogues = expand_table_narration_rows(dialogues, role_names)
    if not roles:
        roles = fallback_roles_from_dialogues(dialogues)
        role_names = {normalize_text(r.get("role_name", "")) for r in roles if normalize_text(r.get("role_name", ""))}

    enriched_roles: list[dict] = []
    for role in roles:
        row = dict(role)
        if "role_type_raw" not in row and "role_type" in row:
            row["role_type_raw"] = row.pop("role_type", "")
        heur = infer_role_from_heuristics(row.get("role_name", ""), row.get("role_type_raw", ""), row.get("raw", ""))
        for k, v in heur.items():
            row.setdefault(k, v)
        enriched_roles.append(row)
    roles = enriched_roles

    # First create heuristic scenes so LLM can enhance them.
    by_scene: dict[int, list[dict]] = defaultdict(list)
    for row in dialogues:
        by_scene[int(row["scene_index"])].append(row)

    max_scene_index = max((int(s.get("scene_index", 0)) for s in scenes), default=0)
    scenes = [
        build_scene_enrichment_rows(dict(scene), by_scene.get(int(scene["scene_index"]), []), max_scene_index, preface_meta=preface_meta)
        for scene in scenes
    ]

    if llm_cfg and llm_cfg.enabled and llm_session is not None:
        preface_meta = llm_enrich_document_meta(
            llm_session, llm_cfg, preface_meta, play_title, doc_context=doc_context, dialogues=dialogues
        )
        roles = llm_enrich_roles_batched(llm_session, llm_cfg, roles, dialogues, play_title)
        scenes, relation_rows_llm, theme_rows_llm = llm_enrich_scenes_and_relations(
            llm_session, llm_cfg, scenes, dialogues, play_title
        )
    else:
        relation_rows_llm = []
        theme_rows_llm = []

    performance_rows = build_performance_rows(dialogues, role_names, doc_id, play_title)

    heuristic_relations = heuristic_relations_from_dialogues(dialogues, role_names, play_title)
    # Normalize heuristic relation labels and preserve raw labels if present.
    normalized_heuristic_relations: list[dict] = []
    for rel in heuristic_relations:
        rel = dict(rel)
        rel["relation_type_raw"] = rel.get("relation_type", "")
        rel["relation_type"] = normalize_relation_type(rel.get("relation_type", ""))
        normalized_heuristic_relations.append(rel)

    relations = normalized_heuristic_relations + relation_rows_llm
    if not relations:
        relations = normalized_heuristic_relations
    for rel in relations:
        rel["relation_type"] = normalize_relation_type(rel.get("relation_type", rel.get("relation_type_raw", "")))
        if not is_relation_speaker(normalize_text(rel.get("source", "")), role_names):
            rel["_drop"] = True
        if not is_relation_speaker(normalize_text(rel.get("target", "")), role_names):
            rel["_drop"] = True
    relations = [r for r in relations if not r.pop("_drop", False)]
    relations = dedupe_rows(
        relations,
        ["doc_title", "scene_index", "scene", "source", "target", "relation_type", "derived_by"],
    )
    relations_aggregated = aggregate_relations(relations)

    theme_rows: list[dict] = []
    # LLM themes first
    for t in theme_rows_llm:
        theme_rows.append({
            "doc_title": play_title,
            "scene_index": int(t.get("scene_index", 0) or 0),
            "scene": t.get("scene", ""),
            "theme_label_raw": t.get("theme_label_raw", t.get("theme_label", "")),
            "theme_label": normalize_theme_label(t.get("theme_label", t.get("theme_label_raw", ""))),
            "theme_strength": int(t.get("theme_strength", 1) or 1),
            "evidence": t.get("evidence", ""),
            "derived_by": t.get("derived_by", "llm"),
        })

    # Heuristic themes from scenes.
    for scene in scenes:
        idx = int(scene.get("scene_index", 0))
        raw_labels = _llm_items(scene.get("theme_labels", ""))
        if not raw_labels:
            rows = by_scene.get(idx, [])
            joined = " ".join(analysis_payload(r) for r in rows)
            # prefer preface synopsis/note too for scene 0
            raw_labels = simple_theme_fallback([joined, preface_meta.get("synopsis", ""), preface_meta.get("note_text", ""), scene.get("summary", "")])
        for label in raw_labels:
            norm = normalize_theme_label(label)
            if not norm:
                continue
            theme_rows.append({
                "doc_title": play_title,
                "scene_index": idx,
                "scene": scene.get("scene", ""),
                "theme_label_raw": label,
                "theme_label": norm,
                "theme_strength": 1,
                "evidence": scene.get("summary", ""),
                "derived_by": "keyword" if label in raw_labels else "doc_keyword",
            })

    if not theme_rows:
        for theme in simple_theme_fallback([preface_meta.get("synopsis", ""), preface_meta.get("note_text", "")]):
            theme_rows.append({
                "doc_title": play_title,
                "scene_index": 0,
                "scene": "序幕/前置内容",
                "theme_label_raw": theme,
                "theme_label": normalize_theme_label(theme),
                "theme_strength": 1,
                "evidence": preface_meta.get("synopsis", ""),
                "derived_by": "doc_keyword",
            })

    # Deduplicate raw theme rows.
    seen_theme_keys = set()
    deduped_themes = []
    for t in theme_rows:
        key = (
            normalize_text(t.get("doc_title", "")),
            int(t.get("scene_index", 0) or 0),
            normalize_text(t.get("scene", "")),
            normalize_theme_label(t.get("theme_label", t.get("theme_label_raw", ""))),
        )
        if key in seen_theme_keys:
            continue
        seen_theme_keys.add(key)
        t["theme_label"] = normalize_theme_label(t.get("theme_label", t.get("theme_label_raw", "")))
        deduped_themes.append(t)
    theme_rows = deduped_themes
    themes_aggregated = aggregate_themes(theme_rows)

    # enrich final scenes with stable defaults and canonical values
    max_scene_index = max((int(s.get("scene_index", 0)) for s in scenes), default=0)
    for scene in scenes:
        idx = int(scene.get("scene_index", 0))
        rows = by_scene.get(idx, [])
        scene = build_scene_enrichment_rows(scene, rows, max_scene_index, preface_meta=preface_meta)
        scene["conflict_stage"] = normalize_conflict_stage(scene.get("conflict_stage", ""), int(scene.get("tension_level", 0) or 0), idx, max_scene_index)
        scene["scene_function"] = normalize_scene_function(scene.get("scene_function", ""), int(scene.get("tension_level", 0) or 0), idx, max_scene_index)
        scene["theme_labels"] = "；".join([normalize_theme_label(x) for x in _llm_items(scene.get("theme_labels", "")) if normalize_theme_label(x)])

    # Raw snapshot for auditability
    raw_snapshot = {
        "metadata": {
            **source_meta,
            **collection_meta,
            "parser_version": PARSER_VERSION,
            "phase": "pre_llm",
            "play_title": play_title,
            "doc_id": doc_id,
            "aliases": preface_meta.get("aliases", []),
            "alias_text": preface_meta.get("alias_text", ""),
            "synopsis": preface_meta.get("synopsis", ""),
            "note_text": preface_meta.get("note_text", ""),
            "period_hint": preface_meta.get("period_hint", ""),
            "genre_hint": preface_meta.get("genre_hint", ""),
            "line_count": len(lines),
            "role_count": len(roles),
            "scene_count": len(scenes),
            "dialogue_count": len(dialogues),
            "performance_count": len(performance_rows),
            "relation_count": len(relations),
            "theme_count": len(theme_rows),
            "llm_enabled": False,
            "llm_model": "",
        },
        "roles": deepcopy(roles),
        "scenes": deepcopy(scenes),
        "dialogues": deepcopy(dialogues),
        "performances": deepcopy(performance_rows),
        "relations": deepcopy(relations),
        "relations_aggregated": deepcopy(relations_aggregated),
        "themes": deepcopy(theme_rows),
        "themes_aggregated": deepcopy(themes_aggregated),
    }

    metadata = {
        **source_meta,
        **collection_meta,
        "parser_version": PARSER_VERSION,
        "play_title": play_title,
        "doc_id": doc_id,
        "aliases": preface_meta.get("aliases", []),
        "alias_text": preface_meta.get("alias_text", ""),
        "synopsis": preface_meta.get("synopsis", ""),
        "note_text": preface_meta.get("note_text", ""),
        "period_hint": preface_meta.get("period_hint", ""),
        "genre_hint": preface_meta.get("genre_hint", ""),
        "doc_tags": preface_meta.get("doc_tags", []),
        "main_conflict": preface_meta.get("main_conflict", ""),
        "line_count": len(lines),
        "role_count": len(roles),
        "scene_count": len(scenes),
        "dialogue_count": len(dialogues),
        "performance_count": len(performance_rows),
        "relation_count": len(relations),
        "aggregated_relation_count": len(relations_aggregated),
        "theme_count": len(theme_rows),
        "aggregated_theme_count": len(themes_aggregated),
        "llm_enabled": bool(llm_cfg.enabled) if llm_cfg else False,
        "llm_model": llm_cfg.model if llm_cfg and llm_cfg.enabled else "",
    }

    structured = {
        "metadata": metadata,
        "roles": roles,
        "scenes": scenes,
        "dialogues": dialogues,
        "performances": performance_rows,
        "relations": relations,
        "relations_aggregated": relations_aggregated,
        "themes": theme_rows,
        "themes_aggregated": themes_aggregated,
        "structured_raw": raw_snapshot,
    }
    return finalize_structured_package(
        structured, md_text, play_title, doc_id, llm_cfg=llm_cfg, llm_session=llm_session
    )


def _csv_row_count(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with path.open("r", encoding="utf-8", newline="") as f:
        return max(0, sum(1 for _ in f) - 1)


def _structured_output_valid(meta: dict) -> bool:
    try:
        structured_path = Path(meta["structured_json"])
        documents_csv = Path(meta["documents_csv"])
        roles_csv = Path(meta["roles_csv"])
        scenes_csv = Path(meta["scenes_csv"])
        dialogues_csv = Path(meta["dialogues_csv"])
        performances_csv = Path(meta["performances_csv"])
        relations_csv = Path(meta["relations_csv"])
        themes_csv = Path(meta["themes_csv"])
        if not all(p.exists() for p in [structured_path, documents_csv, roles_csv, scenes_csv, dialogues_csv, performances_csv, relations_csv, themes_csv]):
            return False
        if any(_csv_row_count(p) <= 0 for p in [documents_csv, roles_csv, scenes_csv, dialogues_csv, performances_csv, relations_csv, themes_csv]):
            return False
        data = json.loads(structured_path.read_text(encoding="utf-8"))
        if data.get("metadata", {}).get("parser_version") != PARSER_VERSION:
            return False
        return True
    except Exception:
        return False


def write_manifest(manifest_path: Path, records: list[dict]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "source_file", "source_path", "relative_path", "collection_dir", "collection_code",
        "collection_name", "collection_label", "work_code", "work_title_hint", "title",
        "batch_id", "state", "full_zip_url", "target_dir", "md_path", "raw_md_path",
        "cleaned_md_path", "documents_csv", "roles_csv", "scenes_csv", "dialogues_csv",
        "performances_csv", "relations_csv", "relations_aggregated_csv", "themes_csv",
        "themes_aggregated_csv", "entity_aliases_csv", "theme_pairs_csv", "narrative_curve_csv",
        "network_metrics_csv", "structured_raw_json", "structured_json", "skipped",
        "error", "role_count", "scene_count", "dialogue_count", "performance_count",
        "relation_count", "aggregated_relation_count", "theme_count", "aggregated_theme_count",
        "parse_quality_score", "parser_version", "llm_enabled", "llm_model",
    ]
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            writer.writerow({k: rec.get(k, "") for k in fieldnames})


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Batch convert PDFs with MinerU and structure Jingju scripts.")
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument("--input-dir", type=Path, help="Directory containing PDFs")
    src.add_argument("--files", nargs="*", type=Path, help="Explicit PDF files")
    p.add_argument("--output-dir", type=Path, default=Path("opera_output"), help="Output directory (default: opera_output)")
    p.add_argument("--manifest", type=Path, help="Optional CSV manifest output path")

    p.add_argument("--model", default=os.getenv("MINERU_MODEL", "vlm"), help="pipeline|vlm|MinerU-HTML")
    p.add_argument("--language", default=os.getenv("MINERU_LANG", "ch"), help="Language code")
    p.add_argument("--wait", type=int, default=int(os.getenv("MINERU_WAIT", "15")), help="Poll interval seconds")
    p.add_argument("--timeout-minutes", type=int, default=int(os.getenv("MINERU_TIMEOUT", "120")), help="Per-batch timeout")
    p.add_argument("--chunk-size", type=int, default=50, help="Local upload batch size (<=50 recommended)")
    p.add_argument("--no-recursive", action="store_true", help="Do not scan subfolders")
    p.add_argument("--clean-md", dest="clean_md", action="store_true", help="Apply conservative cleanup to full.md")
    p.add_argument("--no-clean-md", dest="clean_md", action="store_false", help="Do not clean full.md")
    p.set_defaults(clean_md=True)
    p.add_argument("--no-skip-existing", action="store_true", help="Rebuild outputs even if markdown exists")
    p.add_argument("--use-env-proxy", action="store_true", help="Allow requests to use system proxy env vars")
    p.add_argument("--keep-md", action="store_true", help="Keep cleaned Markdown output per file")
    p.add_argument("--keep-transient", action="store_true", help="Keep MinerU zip/extracted transient files")
    p.add_argument("--structured-only", action="store_true", help="Only output structured files, not markdown")
    p.add_argument("--llm-enabled", action="store_true", help="Enable LLM enrichment")
    p.add_argument("--llm-api-key-env", default="DEEPSEEK_API_KEY", help="Environment variable that stores the LLM API key")
    p.add_argument("--llm-base-url", default="https://api.deepseek.com", help="LLM base URL")
    p.add_argument("--llm-model", default="deepseek-chat", help="LLM model name")
    p.add_argument("--llm-timeout-seconds", type=int, default=90, help="LLM request timeout")
    p.add_argument("--llm-max-tokens", type=int, default=2000, help="LLM max tokens")
    p.add_argument("--llm-max-input-chars", type=int, default=7000, help="LLM max input chars per scene")
    p.add_argument("--llm-max-scene-lines", type=int, default=100, help="LLM max scene lines")
    p.add_argument("--llm-max-role-evidence", type=int, default=14, help="LLM max evidence lines per role")
    p.add_argument("--llm-max-dialogue-lines", type=int, default=100, help="Max dialogue lines to LLM-annotate per play")
    p.add_argument("--llm-dialogue-batch-size", type=int, default=18, help="Dialogue lines per LLM batch request")
    p.add_argument("--llm-roles-batch-size", type=int, default=8, help="Roles per LLM batch request")
    p.add_argument("--llm-max-relation-refine", type=int, default=48, help="Max aggregated relation edges to LLM-refine")
    p.add_argument("--no-llm-dialogue", action="store_true", help="Disable LLM dialogue emotion/target enrichment")
    p.add_argument("--no-llm-relation-refine", action="store_true", help="Disable LLM aggregated relation refinement")

    p.add_argument("--combined-docs-csv", type=Path, help="Optional combined docs CSV")
    p.add_argument("--combined-roles-csv", type=Path, help="Optional combined roles CSV")
    p.add_argument("--combined-scenes-csv", type=Path, help="Optional combined scenes CSV")
    p.add_argument("--combined-dialogue-csv", type=Path, help="Optional combined dialogues CSV")
    p.add_argument("--combined-dialogue-jsonl", type=Path, help="Optional combined dialogues JSONL")
    p.add_argument("--combined-performances-csv", type=Path, help="Optional combined performance events CSV")
    p.add_argument("--combined-performances-jsonl", type=Path, help="Optional combined performances JSONL")
    p.add_argument("--combined-relations-csv", type=Path, help="Optional combined relations CSV (raw)")
    p.add_argument("--combined-relations-aggregated-csv", type=Path, help="Optional combined relations CSV (aggregated)")
    p.add_argument("--combined-themes-csv", type=Path, help="Optional combined themes CSV (raw)")
    p.add_argument("--combined-themes-aggregated-csv", type=Path, help="Optional combined themes CSV (aggregated)")
    p.add_argument("--combined-structured-jsonl", type=Path, help="Optional combined structured JSONL")
    p.add_argument("--combined-entity-aliases-csv", type=Path, help="Optional combined entity alias CSV")
    p.add_argument("--combined-theme-pairs-csv", type=Path, help="Optional combined theme co-occurrence CSV")
    p.add_argument("--combined-narrative-curve-csv", type=Path, help="Optional combined narrative curve CSV")
    p.add_argument("--combined-network-metrics-csv", type=Path, help="Optional combined network metrics CSV")
    p.add_argument(
        "--combine-only",
        action="store_true",
        help="Skip MinerU; rebuild combined CSV/JSONL from existing structured.json under --output-dir",
    )
    p.add_argument(
        "--repack-only",
        action="store_true",
        help="Reorganize each play into layered folders (01_meta..06_narrative) from structured.json",
    )
    p.add_argument(
        "--status-only",
        action="store_true",
        help="Compare input PDFs vs existing outputs; do not call MinerU",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N PDFs (0=all). Useful for smoke tests.",
    )
    p.add_argument(
        "--collection-prefix",
        type=str,
        default="",
        help="Only combine/status paths under this relative prefix, e.g. 01000000",
    )
    p.add_argument(
        "--auto-combine",
        action="store_true",
        default=True,
        help="After batch run, write opera_output/all_*.csv (default: on)",
    )
    p.add_argument(
        "--no-auto-combine",
        action="store_false",
        dest="auto_combine",
        help="Skip writing combined all_*.csv after batch",
    )
    p.set_defaults(manifest=Path("opera_output/mineru_manifest.csv"))
    return p


def target_paths(out_root: Path, pdf_path: Path, rel_path: Path) -> tuple[Path, Path]:
    rel_parent = rel_path.parent
    stem = pdf_path.stem
    target_dir = out_root / rel_parent / stem
    md_path = target_dir / f"{stem}.md"
    return target_dir, md_path


def play_output_layout(play_dir: Path) -> dict[str, Path]:
    """Layered per-play output paths for downstream analysis."""
    return {
        "root": play_dir,
        "meta_dir": play_dir / "01_meta",
        "cast_dir": play_dir / "02_cast",
        "script_dir": play_dir / "03_script",
        "graph_dir": play_dir / "04_graph",
        "themes_dir": play_dir / "05_themes",
        "narrative_dir": play_dir / "06_narrative",
        "audit_dir": play_dir / "audit",
        "documents_csv": play_dir / "01_meta" / "documents.csv",
        "roles_csv": play_dir / "02_cast" / "roles.csv",
        "scenes_csv": play_dir / "03_script" / "scenes.csv",
        "dialogues_csv": play_dir / "03_script" / "dialogues.csv",
        "performances_csv": play_dir / "03_script" / "performances.csv",
        "relations_csv": play_dir / "04_graph" / "relations.csv",
        "relations_aggregated_csv": play_dir / "04_graph" / "relations_aggregated.csv",
        "network_metrics_csv": play_dir / "04_graph" / "network_metrics.csv",
        "entity_aliases_csv": play_dir / "04_graph" / "entity_aliases.csv",
        "themes_csv": play_dir / "05_themes" / "themes.csv",
        "themes_aggregated_csv": play_dir / "05_themes" / "themes_aggregated.csv",
        "theme_pairs_csv": play_dir / "05_themes" / "theme_pairs.csv",
        "narrative_curve_csv": play_dir / "06_narrative" / "narrative_curve.csv",
        "structured_json": play_dir / "structured.json",
        "structured_raw_json": play_dir / "audit" / "structured_raw.json",
        "cleaned_md": play_dir / "audit" / "cleaned_full.md",
        "raw_md": play_dir / "audit" / "raw_full.md",
    }


def resolve_play_documents_csv(play_dir: Path) -> Path | None:
    layered = play_dir / "01_meta" / "documents.csv"
    if layered.exists():
        return layered
    flat = play_dir / "documents.csv"
    return flat if flat.exists() else None


def write_play_package(
    layout: dict[str, Path],
    *,
    doc_id: str,
    title: str,
    doc_row: dict,
    roles: list[dict],
    scenes: list[dict],
    dialogues: list[dict],
    performances: list[dict],
    relations: list[dict],
    relations_aggregated: list[dict],
    themes: list[dict],
    themes_aggregated: list[dict],
    entity_aliases: list[dict],
    theme_pairs: list[dict],
    narrative_curve: list[dict],
    network_metrics: list[dict],
    structured: dict,
    structured_raw: dict | None,
) -> None:
    for key in ("meta_dir", "cast_dir", "script_dir", "graph_dir", "themes_dir", "narrative_dir", "audit_dir"):
        layout[key].mkdir(parents=True, exist_ok=True)

    write_csv(
        layout["documents_csv"],
        [doc_row],
        [
            "doc_id", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
            "source_file", "source_path", "relative_path", "title", "aliases", "alias_text", "period_hint", "genre_hint",
            "synopsis", "note_text", "doc_tags", "text_length", "parse_quality_score", "parse_quality_label", "analysis_ready",
            "parser_version", "scene_count", "role_count", "dialogue_count", "performance_count", "relation_count",
            "aggregated_relation_count", "theme_count", "aggregated_theme_count", "entity_alias_count", "theme_pair_count",
            "llm_enabled", "llm_model", "state", "raw_md_path", "cleaned_md_path", "structured_raw_json", "structured_json",
        ],
    )
    write_csv(
        layout["roles_csv"],
        [dict(r, doc_id=doc_id, doc_title=title) for r in roles],
        [
            "doc_id", "doc_title", "char_id", "role_name", "alias_names", "role_type_raw", "role_type_inferred",
            "gender_inferred", "age_inferred", "status_identity", "personality_tags", "behavior_tags", "speech_style",
            "narrative_function", "line_count", "scene_coverage", "scene_count_present", "centrality_hint",
            "evidence", "source_line", "raw", "confidence", "llm_error", "role_note",
        ],
    )
    write_csv(
        layout["scenes_csv"],
        [dict(s, doc_id=doc_id, doc_title=title) for s in scenes],
        [
            "doc_id", "doc_title", "scene_id", "scene_index", "scene", "line_no", "start_line", "end_line", "location", "time_hint",
            "main_event", "dominant_action", "summary", "conflict_stage", "tension_level", "key_characters", "theme_labels",
            "scene_function", "narrative_turning_point", "characters_present", "line_count", "dialogue_count", "lyric_count",
            "dialogue_density", "lyric_density", "speech_density",
            "stage_direction_count", "narration_count", "llm_error",
        ],
    )
    write_csv(
        layout["dialogues_csv"],
        [dict(d, doc_id=doc_id, doc_title=title, line_category=d.get("row_type", "")) for d in dialogues],
        [
            "doc_id", "doc_title", "line_id", "scene_id", "source_file", "source_path", "relative_path", "batch_id",
            "scene_index", "scene", "line_no", "row_type", "line_category", "speaker", "speaker_char_id", "cue", "text",
            "target", "target_char_id", "emotion_tag", "speech_act", "entity_tags", "action_tags", "is_key_line",
            "emotion_derived_by", "target_derived_by", "llm_confidence", "raw_line",
        ],
    )
    write_csv(
        layout["performances_csv"],
        [dict(p, doc_id=doc_id, doc_title=title, line_category=p.get("row_type", "")) for p in performances],
        [
            "doc_id", "doc_title", "line_id", "scene_id", "source_file", "source_path", "relative_path", "batch_id",
            "scene_index", "scene", "line_no", "row_type", "line_category", "speaker", "speaker_char_id", "cue",
            "perform_type", "perform_subtype", "action_tags", "participants", "text", "raw_line",
        ],
    )
    write_csv(
        layout["relations_csv"],
        [dict(r, doc_id=doc_id, doc_title=title) for r in relations],
        [
            "doc_id", "doc_title", "scene_index", "scene", "source", "target", "relation_type_raw", "relation_type",
            "weight", "evidence", "evidence_line_ids", "derived_by", "llm_confidence",
        ],
    )
    write_csv(
        layout["relations_aggregated_csv"],
        [dict(r, doc_id=doc_id, doc_title=title) for r in relations_aggregated],
        [
            "doc_id", "doc_title", "source", "target", "relation_type", "weight", "merged_relation_types",
            "evidence", "derived_by", "llm_confidence",
        ],
    )
    write_csv(
        layout["themes_csv"],
        [dict(t, doc_id=doc_id, doc_title=title) for t in themes],
        [
            "doc_id", "doc_title", "scene_index", "scene", "theme_stage", "theme_label_raw", "theme_label", "theme_strength",
            "theme_role_links", "evidence", "derived_by",
        ],
    )
    write_csv(
        layout["themes_aggregated_csv"],
        [dict(t, doc_id=doc_id, doc_title=title) for t in themes_aggregated],
        ["doc_id", "doc_title", "theme_label", "weight", "evidence", "derived_by"],
    )
    write_csv(
        layout["entity_aliases_csv"],
        entity_aliases,
        ["doc_id", "doc_title", "alias_name", "canonical_name", "char_id", "alias_char_id", "derived_by"],
    )
    write_csv(
        layout["theme_pairs_csv"],
        theme_pairs,
        ["doc_id", "doc_title", "theme_a", "theme_b", "cooccurrence_weight", "evidence", "derived_by"],
    )
    write_csv(
        layout["narrative_curve_csv"],
        narrative_curve,
        [
            "doc_id", "doc_title", "scene_id", "scene_index", "scene", "conflict_stage", "scene_function",
            "tension_level", "tension_norm", "dialogue_density", "lyric_density", "speech_density",
            "narrative_turning_point", "is_climax", "derived_by",
        ],
    )
    write_csv(
        layout["network_metrics_csv"],
        network_metrics,
        [
            "doc_id", "doc_title", "character", "char_id", "degree", "weighted_degree",
            "degree_centrality", "strength_centrality", "derived_by",
        ],
    )
    write_json(layout["structured_json"], structured)
    write_json(layout["structured_raw_json"], structured_raw or structured)
    index_text = (
        "剧目结构化数据目录（r6）\n"
        "01_meta/documents.csv      文档元数据与质量评分\n"
        "02_cast/roles.csv          角色画像（含 LLM 行当/性格）\n"
        "03_script/                 场次、台词、表演事件\n"
        "04_graph/                  人物关系、网络指标、别名\n"
        "05_themes/                 主题与共现\n"
        "06_narrative/              叙事张力曲线\n"
        "audit/                     清洗后 Markdown 与原始结构化快照\n"
        "structured.json            全量 JSON（可视化/二次开发入口）\n"
    )
    (layout["root"] / "README.txt").write_text(index_text, encoding="utf-8")


def convert_one_batch(
    session: requests.Session,
    cfg: MinerUConfig,
    files: Sequence[Path],
    rel_paths: Sequence[Path],
    out_root: Path,
    llm_cfg: LLMConfig,
    llm_session: requests.Session | None,
) -> list[dict]:
    batch_id, upload_urls = request_upload_urls(session, cfg, files, rel_paths)
    upload_files(session, upload_urls, files)
    results = poll_results(session, cfg, batch_id)

    processed: list[dict] = []
    for idx, item in enumerate(results):
        file_path = files[idx]
        rel_path = rel_paths[idx]
        file_name = item.get("file_name", file_path.name)
        state = item.get("state")
        zip_url = item.get("full_zip_url")
        target_dir, md_path = target_paths(out_root, file_path, rel_path)
        layout = play_output_layout(target_dir)
        collection_meta = infer_collection_info(rel_path)
        doc_id = stable_data_id(rel_path)

        meta = {
            "doc_id": doc_id,
            "source_file": file_name,
            "source_path": str(file_path),
            "relative_path": rel_path.as_posix(),
            **collection_meta,
            "batch_id": batch_id,
            "state": state,
            "full_zip_url": zip_url,
            "target_dir": str(target_dir),
            "md_path": str(md_path),
            "raw_md_path": str(layout["raw_md"]),
            "cleaned_md_path": str(layout["cleaned_md"]),
            "documents_csv": str(layout["documents_csv"]),
            "roles_csv": str(layout["roles_csv"]),
            "scenes_csv": str(layout["scenes_csv"]),
            "dialogues_csv": str(layout["dialogues_csv"]),
            "performances_csv": str(layout["performances_csv"]),
            "relations_csv": str(layout["relations_csv"]),
            "relations_aggregated_csv": str(layout["relations_aggregated_csv"]),
            "themes_csv": str(layout["themes_csv"]),
            "themes_aggregated_csv": str(layout["themes_aggregated_csv"]),
            "entity_aliases_csv": str(layout["entity_aliases_csv"]),
            "theme_pairs_csv": str(layout["theme_pairs_csv"]),
            "narrative_curve_csv": str(layout["narrative_curve_csv"]),
            "network_metrics_csv": str(layout["network_metrics_csv"]),
            "structured_raw_json": str(layout["structured_raw_json"]),
            "structured_json": str(layout["structured_json"]),
        }

        if state != "done" or not zip_url:
            meta["error"] = item.get("err_msg") or "unexpected state / missing zip url"
            processed.append(meta)
            log_warn(f"[WARN] {file_name} -> {meta['error']}")
            continue

        if cfg.skip_existing and _structured_output_valid(meta):
            meta["skipped"] = True
            processed.append(meta)
            log_info(f"[SKIP] {file_name} -> already valid r6 output")
            continue

        extract_dir = download_and_extract_zip(session, zip_url, target_dir)
        full_md = locate_full_md(extract_dir)
        md_text = full_md.read_text(encoding="utf-8", errors="ignore")
        target_dir.mkdir(parents=True, exist_ok=True)

        layout["audit_dir"].mkdir(parents=True, exist_ok=True)
        raw_md_path = layout["raw_md"]
        cleaned_md_path = layout["cleaned_md"]
        parse_md_text = clean_markdown_text(md_text) if cfg.clean_md else md_text
        cleaned_md_path.write_text(parse_md_text, encoding="utf-8")
        if not cfg.clean_md or md_text.strip() != parse_md_text.strip():
            raw_md_path.write_text(md_text, encoding="utf-8")
        elif raw_md_path.exists():
            raw_md_path.unlink()
        if cfg.keep_md and not cfg.structured_only:
            md_path.write_text(parse_md_text, encoding="utf-8")

        structured = parse_markdown_structure(
            parse_md_text,
            source_meta={
                "source_file": file_name,
                "source_path": str(file_path),
                "relative_path": rel_path.as_posix(),
                "batch_id": batch_id,
                "doc_id": doc_id,
            },
            llm_cfg=llm_cfg,
            llm_session=llm_session,
        )

        roles = structured["roles"]
        scenes = structured["scenes"]
        dialogues = structured["dialogues"]
        performances = structured.get("performances", [])
        relations = structured["relations"]
        relations_aggregated = structured["relations_aggregated"]
        themes = structured["themes"]
        themes_aggregated = structured["themes_aggregated"]
        entity_aliases = structured.get("entity_aliases", [])
        theme_pairs = structured.get("theme_pairs", [])
        narrative_curve = structured.get("narrative_curve", [])
        network_metrics = structured.get("network_metrics", [])
        structured_raw = structured.get("structured_raw", {})

        title = structured["metadata"].get("play_title", "")
        meta_md = structured["metadata"]
        doc_row = {
            "doc_id": doc_id,
            **collection_meta,
            "source_file": file_name,
            "source_path": str(file_path),
            "relative_path": play_relative_path(rel_path),
            "title": title,
            "aliases": "；".join(meta_md.get("aliases", []) or []),
            "alias_text": meta_md.get("alias_text", ""),
            "period_hint": meta_md.get("period_hint", ""),
            "genre_hint": meta_md.get("genre_hint", ""),
            "synopsis": meta_md.get("synopsis", ""),
            "note_text": meta_md.get("note_text", ""),
            "doc_tags": "；".join(meta_md.get("doc_tags", []) or []),
            "text_length": meta_md.get("text_length", len(parse_md_text)),
            "parse_quality_score": meta_md.get("parse_quality_score", 0),
            "parse_quality_label": meta_md.get("parse_quality_label", ""),
            "analysis_ready": meta_md.get("analysis_ready", False),
            "parser_version": meta_md.get("parser_version", PARSER_VERSION),
            "scene_count": len(scenes),
            "role_count": len(roles),
            "dialogue_count": len(dialogues),
            "performance_count": len(performances),
            "relation_count": len(relations),
            "aggregated_relation_count": len(relations_aggregated),
            "theme_count": len(themes),
            "aggregated_theme_count": len(themes_aggregated),
            "entity_alias_count": len(entity_aliases),
            "theme_pair_count": len(theme_pairs),
            "llm_enabled": meta_md.get("llm_enabled", False),
            "llm_model": meta_md.get("llm_model", ""),
            "state": state,
            "raw_md_path": str(raw_md_path) if raw_md_path.exists() else "",
            "cleaned_md_path": str(cleaned_md_path),
            "structured_raw_json": str(layout["structured_raw_json"]),
            "structured_json": str(layout["structured_json"]),
        }

        write_play_package(
            layout,
            doc_id=doc_id,
            title=title,
            doc_row=doc_row,
            roles=roles,
            scenes=scenes,
            dialogues=dialogues,
            performances=performances,
            relations=relations,
            relations_aggregated=relations_aggregated,
            themes=themes,
            themes_aggregated=themes_aggregated,
            entity_aliases=entity_aliases,
            theme_pairs=theme_pairs,
            narrative_curve=narrative_curve,
            network_metrics=network_metrics,
            structured=structured,
            structured_raw=structured_raw,
        )

        meta.update({
            "title": title,
            "raw_md_path": str(raw_md_path) if raw_md_path.exists() else "",
            "cleaned_md_path": str(cleaned_md_path),
            "play_title": title,
            "parser_version": meta_md.get("parser_version", PARSER_VERSION),
            "parse_quality_score": meta_md.get("parse_quality_score", 0),
            "role_count": len(roles),
            "scene_count": len(scenes),
            "dialogue_count": len(dialogues),
            "performance_count": len(performances),
            "relation_count": len(relations),
            "aggregated_relation_count": len(relations_aggregated),
            "theme_count": len(themes),
            "aggregated_theme_count": len(themes_aggregated),
            "llm_enabled": meta_md.get("llm_enabled", False),
            "llm_model": meta_md.get("llm_model", ""),
        })

        (target_dir / "mineru_result.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        if not cfg.keep_transient:
            shutil.rmtree(target_dir / "extracted", ignore_errors=True)
            zip_path = target_dir / "result.zip"
            if zip_path.exists():
                zip_path.unlink()

        processed.append(meta)
        print(f"[OK] {file_name} -> {md_path if cfg.keep_md and not cfg.structured_only else target_dir / 'structured.json'}")

    return processed


def apply_default_combined_paths(out_root: Path, args: argparse.Namespace) -> None:
    defaults = {
        "combined_docs_csv": out_root / "all_docs.csv",
        "combined_roles_csv": out_root / "all_roles.csv",
        "combined_scenes_csv": out_root / "all_scenes.csv",
        "combined_dialogue_csv": out_root / "all_dialogues.csv",
        "combined_dialogue_jsonl": out_root / "all_dialogues.jsonl",
        "combined_performances_csv": out_root / "all_performances.csv",
        "combined_performances_jsonl": out_root / "all_performances.jsonl",
        "combined_relations_csv": out_root / "all_relations.csv",
        "combined_relations_aggregated_csv": out_root / "all_relations_aggregated.csv",
        "combined_themes_csv": out_root / "all_themes.csv",
        "combined_themes_aggregated_csv": out_root / "all_themes_aggregated.csv",
        "combined_entity_aliases_csv": out_root / "all_entity_aliases.csv",
        "combined_theme_pairs_csv": out_root / "all_theme_pairs.csv",
        "combined_narrative_curve_csv": out_root / "all_narrative_curve.csv",
        "combined_network_metrics_csv": out_root / "all_network_metrics.csv",
        "combined_structured_jsonl": out_root / "all_structured.jsonl",
    }
    for attr, path in defaults.items():
        if getattr(args, attr, None) is None:
            setattr(args, attr, path)


def build_play_meta_for_status(target_dir: Path, rel_path: Path, doc_id: str) -> dict:
    layout = play_output_layout(target_dir)
    collection_meta = infer_collection_info(rel_path)
    return {
        "doc_id": doc_id,
        "relative_path": rel_path.as_posix(),
        **collection_meta,
        "target_dir": str(target_dir),
        "structured_json": str(layout["structured_json"]),
        "documents_csv": str(layout["documents_csv"]),
        "roles_csv": str(layout["roles_csv"]),
        "scenes_csv": str(layout["scenes_csv"]),
        "dialogues_csv": str(layout["dialogues_csv"]),
        "performances_csv": str(layout["performances_csv"]),
        "relations_csv": str(layout["relations_csv"]),
        "themes_csv": str(layout["themes_csv"]),
    }


def run_status_only(args: argparse.Namespace) -> int:
    if not args.input_dir:
        print("--status-only 需要 --input-dir", file=sys.stderr)
        return 2
    input_root = args.input_dir.resolve()
    out_root = args.output_dir.resolve()
    pdfs = iter_files(input_root, args.files, recursive=not args.no_recursive)
    if args.limit and args.limit > 0:
        pdfs = pdfs[: args.limit]
    prefix = (args.collection_prefix or "").strip().replace("\\", "/").strip("/")

    done_r6: list[str] = []
    done_old: list[str] = []
    pending: list[str] = []
    for pdf in pdfs:
        rel = relative_pdf_path(pdf, input_root)
        if prefix and not rel.as_posix().startswith(prefix):
            continue
        target_dir = out_root / rel.parent / pdf.stem
        meta = build_play_meta_for_status(target_dir, rel, stable_data_id(rel))
        if _structured_output_valid(meta):
            done_r6.append(rel.as_posix())
        elif meta["structured_json"] and Path(meta["structured_json"]).exists():
            done_old.append(rel.as_posix())
        else:
            pending.append(rel.as_posix())

    total = len(done_r6) + len(done_old) + len(pending)
    print(f"[STATUS] input_root={input_root}")
    print(f"[STATUS] output_root={out_root}  parser={PARSER_VERSION}")
    if prefix:
        print(f"[STATUS] collection_prefix={prefix}")
    print(f"[STATUS] total={total}  r6_ok={len(done_r6)}  old_version={len(done_old)}  pending={len(pending)}")
    if pending[:8]:
        print("[STATUS] pending sample:")
        for p in pending[:8]:
            print(f"  - {p}")
    if done_old[:5]:
        print("[STATUS] need rerun (--no-skip-existing) for old parser:")
        for p in done_old[:5]:
            print(f"  - {p}")
    batches = (len(pending) + 49) // 50 if pending else 0
    if pending:
        print(f"[STATUS] pending MinerU batches (~50/batch): {batches}")
    return 0


def collect_combined_from_output(out_root: Path, collection_prefix: str = "") -> dict[str, list]:
    buckets: dict[str, list] = {
        "structured": [],
        "docs": [],
        "roles": [],
        "scenes": [],
        "dialogues": [],
        "performances": [],
        "relations": [],
        "relations_aggregated": [],
        "themes": [],
        "themes_aggregated": [],
        "entity_aliases": [],
        "theme_pairs": [],
        "narrative_curve": [],
        "network_metrics": [],
    }
    for structured_path in sorted(out_root.rglob("structured.json")):
        try:
            structured = json.loads(structured_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[WARN] skip invalid JSON: {structured_path} ({exc})")
            continue
        rel_path = structured_path.parent.relative_to(out_root)
        if collection_prefix:
            norm_prefix = collection_prefix.replace("\\", "/").strip("/")
            if not rel_path.as_posix().startswith(norm_prefix):
                continue
        collection_meta = infer_collection_info(rel_path)
        sm = structured.get("metadata", {})
        doc_id = sm.get("doc_id") or stable_data_id(rel_path)
        play_dir = structured_path.parent
        play_layout = play_output_layout(play_dir)
        documents_csv = resolve_play_documents_csv(play_dir)
        source_file = ""
        source_path = ""
        if documents_csv and documents_csv.exists():
            try:
                with documents_csv.open("r", encoding="utf-8", newline="") as f:
                    row = next(csv.DictReader(f), {})
                source_file = row.get("source_file", "")
                source_path = row.get("source_path", "")
            except Exception:
                pass
        row_collection_meta = {
            "doc_id": doc_id,
            **collection_meta,
            "source_file": source_file,
            "source_path": source_path,
            "relative_path": play_relative_path(rel_path),
            "title": sm.get("play_title", collection_meta.get("work_title_hint", "")),
        }
        doc_row = {
            **row_collection_meta,
            "aliases": "；".join(sm.get("aliases", []) or []) if isinstance(sm.get("aliases"), list) else sm.get("aliases", ""),
            "alias_text": sm.get("alias_text", ""),
            "period_hint": sm.get("period_hint", ""),
            "genre_hint": sm.get("genre_hint", ""),
            "synopsis": sm.get("synopsis", ""),
            "note_text": sm.get("note_text", ""),
            "doc_tags": "；".join(sm.get("doc_tags", []) or []) if isinstance(sm.get("doc_tags"), list) else sm.get("doc_tags", ""),
            "text_length": sm.get("text_length", 0),
            "parse_quality_score": sm.get("parse_quality_score", 0),
            "parse_quality_label": sm.get("parse_quality_label", ""),
            "analysis_ready": sm.get("analysis_ready") if "analysis_ready" in sm else is_analysis_ready(sm),
            "parser_version": sm.get("parser_version", ""),
            "scene_count": sm.get("scene_count", 0),
            "role_count": sm.get("role_count", 0),
            "dialogue_count": sm.get("dialogue_count", 0),
            "performance_count": sm.get("performance_count", 0),
            "relation_count": sm.get("relation_count", 0),
            "aggregated_relation_count": sm.get("aggregated_relation_count", 0),
            "theme_count": sm.get("theme_count", 0),
            "aggregated_theme_count": sm.get("aggregated_theme_count", 0),
            "entity_alias_count": sm.get("entity_alias_count", 0),
            "theme_pair_count": sm.get("theme_pair_count", 0),
            "llm_enabled": sm.get("llm_enabled", False),
            "llm_model": sm.get("llm_model", ""),
            "state": "combined",
            "raw_md_path": str(play_layout["raw_md"]) if play_layout["raw_md"].exists() else (
                str(play_dir / "raw_full.md") if (play_dir / "raw_full.md").exists() else ""
            ),
            "cleaned_md_path": str(play_layout["cleaned_md"]) if play_layout["cleaned_md"].exists() else (
                str(play_dir / "cleaned_full.md") if (play_dir / "cleaned_full.md").exists() else ""
            ),
            "structured_raw_json": str(play_layout["structured_raw_json"]) if play_layout["structured_raw_json"].exists() else str(play_dir / "structured_raw.json"),
            "structured_json": str(structured_path),
        }
        buckets["structured"].append(structured)
        buckets["docs"].append(doc_row)
        buckets["roles"].extend(merge_collection_row(r, row_collection_meta) for r in structured.get("roles", []))
        buckets["scenes"].extend(merge_collection_row(s, row_collection_meta) for s in structured.get("scenes", []))
        buckets["dialogues"].extend(merge_collection_row(d, row_collection_meta) for d in structured.get("dialogues", []))
        buckets["performances"].extend(merge_collection_row(p, row_collection_meta) for p in structured.get("performances", []))
        buckets["relations"].extend(merge_collection_row(r, row_collection_meta) for r in structured.get("relations", []))
        buckets["relations_aggregated"].extend(merge_collection_row(r, row_collection_meta) for r in structured.get("relations_aggregated", []))
        buckets["themes"].extend(merge_collection_row(t, row_collection_meta) for t in structured.get("themes", []))
        buckets["themes_aggregated"].extend(merge_collection_row(t, row_collection_meta) for t in structured.get("themes_aggregated", []))
        buckets["entity_aliases"].extend(merge_collection_row(a, row_collection_meta) for a in structured.get("entity_aliases", []))
        buckets["theme_pairs"].extend(merge_collection_row(t, row_collection_meta) for t in structured.get("theme_pairs", []))
        buckets["narrative_curve"].extend(merge_collection_row(n, row_collection_meta) for n in structured.get("narrative_curve", []))
        buckets["network_metrics"].extend(merge_collection_row(n, row_collection_meta) for n in structured.get("network_metrics", []))
    return buckets


def write_combined_exports(args: argparse.Namespace, buckets: dict[str, list]) -> None:
    all_docs = buckets.get("docs", [])
    all_roles = buckets.get("roles", [])
    all_scenes = buckets.get("scenes", [])
    all_dialogues = buckets.get("dialogues", [])
    all_performances = buckets.get("performances", [])
    all_relations = buckets.get("relations", [])
    all_relations_aggregated = buckets.get("relations_aggregated", [])
    all_themes = buckets.get("themes", [])
    all_themes_aggregated = buckets.get("themes_aggregated", [])
    all_entity_aliases = buckets.get("entity_aliases", [])
    all_theme_pairs = buckets.get("theme_pairs", [])
    all_narrative_curve = buckets.get("narrative_curve", [])
    all_network_metrics = buckets.get("network_metrics", [])
    all_structured = buckets.get("structured", [])

    if args.combined_docs_csv and all_docs:
        write_csv(
            args.combined_docs_csv.resolve(),
            all_docs,
            [
                "doc_id", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "source_file", "source_path", "relative_path", "title", "aliases", "alias_text", "period_hint", "genre_hint",
                "synopsis", "note_text", "doc_tags", "text_length", "parse_quality_score", "parse_quality_label", "analysis_ready",
                "parser_version", "scene_count", "role_count", "dialogue_count", "performance_count", "relation_count",
                "aggregated_relation_count", "theme_count", "aggregated_theme_count", "entity_alias_count", "theme_pair_count",
                "llm_enabled", "llm_model", "state", "raw_md_path", "cleaned_md_path", "structured_raw_json", "structured_json",
            ],
        )
        print(f"[COMBINED] docs CSV -> {args.combined_docs_csv.resolve()} ({len(all_docs)} plays)")

    if args.combined_roles_csv and all_roles:
        write_csv(
            args.combined_roles_csv.resolve(),
            all_roles,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "char_id", "role_name", "alias_names", "role_type_raw", "role_type_inferred", "gender_inferred", "age_inferred", "status_identity",
                "personality_tags", "behavior_tags", "speech_style", "narrative_function", "line_count", "scene_coverage", "scene_count_present", "centrality_hint",
                "evidence", "source_line", "raw", "confidence", "llm_error", "role_note",
            ],
        )
        print(f"[COMBINED] roles CSV -> {args.combined_roles_csv.resolve()}")

    if args.combined_scenes_csv and all_scenes:
        write_csv(
            args.combined_scenes_csv.resolve(),
            all_scenes,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "scene_id", "scene_index", "scene", "line_no", "start_line", "end_line", "location", "time_hint", "main_event",
                "dominant_action", "summary", "conflict_stage", "tension_level", "key_characters", "theme_labels",
                "scene_function", "narrative_turning_point", "characters_present", "line_count", "dialogue_count",
                "lyric_count", "dialogue_density", "lyric_density", "speech_density",
                "stage_direction_count", "narration_count", "llm_error",
            ],
        )
        print(f"[COMBINED] scenes CSV -> {args.combined_scenes_csv.resolve()}")

    if args.combined_dialogue_csv and all_dialogues:
        write_csv(
            args.combined_dialogue_csv.resolve(),
            all_dialogues,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "line_id", "scene_id", "source_file", "source_path", "relative_path", "batch_id", "scene_index", "scene", "line_no", "row_type",
                "line_category", "speaker", "speaker_char_id", "cue", "text", "target", "target_char_id",
                "emotion_tag", "speech_act", "entity_tags", "action_tags", "is_key_line",
                "emotion_derived_by", "target_derived_by", "llm_confidence", "raw_line",
            ],
        )
        print(f"[COMBINED] dialogues CSV -> {args.combined_dialogue_csv.resolve()}")

    if args.combined_dialogue_jsonl and all_dialogues:
        write_jsonl(args.combined_dialogue_jsonl.resolve(), all_dialogues)
        print(f"[COMBINED] dialogues JSONL -> {args.combined_dialogue_jsonl.resolve()}")

    if args.combined_performances_csv and all_performances:
        write_csv(
            args.combined_performances_csv.resolve(),
            all_performances,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "line_id", "scene_id", "source_file", "source_path", "relative_path", "batch_id", "scene_index", "scene", "line_no", "row_type",
                "line_category", "speaker", "speaker_char_id", "cue", "perform_type", "perform_subtype", "action_tags", "participants", "text", "raw_line",
            ],
        )
        print(f"[COMBINED] performances CSV -> {args.combined_performances_csv.resolve()}")

    if args.combined_performances_jsonl and all_performances:
        write_jsonl(args.combined_performances_jsonl.resolve(), all_performances)
        print(f"[COMBINED] performances JSONL -> {args.combined_performances_jsonl.resolve()}")

    if args.combined_relations_csv and all_relations:
        write_csv(
            args.combined_relations_csv.resolve(),
            all_relations,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "scene_index", "scene", "source", "target", "relation_type_raw", "relation_type", "weight", "evidence", "evidence_line_ids", "derived_by", "llm_confidence",
            ],
        )
        print(f"[COMBINED] relations CSV -> {args.combined_relations_csv.resolve()}")

    if args.combined_relations_aggregated_csv and all_relations_aggregated:
        write_csv(
            args.combined_relations_aggregated_csv.resolve(),
            all_relations_aggregated,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "source", "target", "relation_type", "weight", "merged_relation_types", "evidence", "derived_by", "llm_confidence",
            ],
        )
        print(f"[COMBINED] relations aggregated CSV -> {args.combined_relations_aggregated_csv.resolve()}")

    if args.combined_themes_csv and all_themes:
        write_csv(
            args.combined_themes_csv.resolve(),
            all_themes,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "scene_index", "scene", "theme_stage", "theme_label_raw", "theme_label", "theme_strength", "theme_role_links", "evidence", "derived_by",
            ],
        )
        print(f"[COMBINED] themes CSV -> {args.combined_themes_csv.resolve()}")

    if args.combined_themes_aggregated_csv and all_themes_aggregated:
        write_csv(
            args.combined_themes_aggregated_csv.resolve(),
            all_themes_aggregated,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "theme_label", "weight", "evidence", "derived_by",
            ],
        )
        print(f"[COMBINED] themes aggregated CSV -> {args.combined_themes_aggregated_csv.resolve()}")

    if args.combined_entity_aliases_csv and all_entity_aliases:
        write_csv(
            args.combined_entity_aliases_csv.resolve(),
            all_entity_aliases,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "alias_name", "canonical_name", "char_id", "alias_char_id", "derived_by",
            ],
        )
        print(f"[COMBINED] entity aliases CSV -> {args.combined_entity_aliases_csv.resolve()}")

    if args.combined_theme_pairs_csv and all_theme_pairs:
        write_csv(
            args.combined_theme_pairs_csv.resolve(),
            all_theme_pairs,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "theme_a", "theme_b", "cooccurrence_weight", "evidence", "derived_by",
            ],
        )
        print(f"[COMBINED] theme pairs CSV -> {args.combined_theme_pairs_csv.resolve()}")

    if args.combined_narrative_curve_csv and all_narrative_curve:
        write_csv(
            args.combined_narrative_curve_csv.resolve(),
            all_narrative_curve,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "scene_id", "scene_index", "scene", "conflict_stage", "scene_function", "tension_level", "tension_norm",
                "dialogue_density", "lyric_density", "speech_density", "narrative_turning_point", "is_climax", "derived_by",
            ],
        )
        print(f"[COMBINED] narrative curve CSV -> {args.combined_narrative_curve_csv.resolve()}")

    if args.combined_network_metrics_csv and all_network_metrics:
        write_csv(
            args.combined_network_metrics_csv.resolve(),
            all_network_metrics,
            [
                "doc_id", "doc_title", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "character", "char_id", "degree", "weighted_degree", "degree_centrality", "strength_centrality", "derived_by",
            ],
        )
        print(f"[COMBINED] network metrics CSV -> {args.combined_network_metrics_csv.resolve()}")

    if args.combined_structured_jsonl and all_structured:
        write_jsonl(args.combined_structured_jsonl.resolve(), all_structured)
        print(f"[COMBINED] structured JSONL -> {args.combined_structured_jsonl.resolve()}")


def repack_one_play(play_dir: Path, out_root: Path) -> bool:
    """Reorganize flat r4 outputs into layered r6 folders using existing structured.json."""
    structured_path = play_dir / "structured.json"
    if not structured_path.exists():
        return False
    try:
        structured = json.loads(structured_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] repack skip {play_dir}: {exc}")
        return False

    layout = play_output_layout(play_dir)
    sm = structured.get("metadata", {})
    try:
        rel_path = play_dir.relative_to(out_root)
    except ValueError:
        rel_path = Path(play_dir.name)
    collection_meta = infer_collection_info(rel_path)
    doc_id = sm.get("doc_id") or stable_data_id(rel_path)
    title = sm.get("play_title", sm.get("title", collection_meta.get("work_title_hint", "")))

    doc_row: dict = {}
    flat_doc = play_dir / "documents.csv"
    if flat_doc.exists():
        try:
            with flat_doc.open("r", encoding="utf-8", newline="") as f:
                doc_row = dict(next(csv.DictReader(f), {}))
        except Exception:
            doc_row = {}
    if not doc_row:
        doc_row = {
            "doc_id": doc_id,
            **collection_meta,
            "title": title,
            "aliases": "；".join(sm.get("aliases", []) or []) if isinstance(sm.get("aliases"), list) else sm.get("aliases", ""),
            "alias_text": sm.get("alias_text", ""),
            "period_hint": sm.get("period_hint", ""),
            "genre_hint": sm.get("genre_hint", ""),
            "synopsis": sm.get("synopsis", ""),
            "note_text": sm.get("note_text", ""),
            "doc_tags": "；".join(sm.get("doc_tags", []) or []) if isinstance(sm.get("doc_tags"), list) else sm.get("doc_tags", ""),
            "text_length": sm.get("text_length", 0),
            "parse_quality_score": sm.get("parse_quality_score", 0),
            "parse_quality_label": sm.get("parse_quality_label", ""),
            "analysis_ready": sm.get("analysis_ready") if "analysis_ready" in sm else is_analysis_ready(sm),
            "parser_version": sm.get("parser_version", PARSER_VERSION),
            "scene_count": sm.get("scene_count", len(structured.get("scenes", []))),
            "role_count": sm.get("role_count", len(structured.get("roles", []))),
            "dialogue_count": sm.get("dialogue_count", len(structured.get("dialogues", []))),
            "performance_count": sm.get("performance_count", len(structured.get("performances", []))),
            "relation_count": sm.get("relation_count", len(structured.get("relations", []))),
            "aggregated_relation_count": sm.get("aggregated_relation_count", len(structured.get("relations_aggregated", []))),
            "theme_count": sm.get("theme_count", len(structured.get("themes", []))),
            "aggregated_theme_count": sm.get("aggregated_theme_count", len(structured.get("themes_aggregated", []))),
            "entity_alias_count": len(structured.get("entity_aliases", [])),
            "theme_pair_count": len(structured.get("theme_pairs", [])),
            "llm_enabled": sm.get("llm_enabled", False),
            "llm_model": sm.get("llm_model", ""),
            "state": "repacked",
        }
    doc_row["structured_json"] = str(layout["structured_json"])
    doc_row["structured_raw_json"] = str(layout["structured_raw_json"])
    for src_name, dst_key in (("cleaned_full.md", "cleaned_md"), ("raw_full.md", "raw_md")):
        src = play_dir / src_name
        dst = layout[dst_key]
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                shutil.copy2(src, dst)
    doc_row["cleaned_md_path"] = str(layout["cleaned_md"]) if layout["cleaned_md"].exists() else doc_row.get("cleaned_md_path", "")
    doc_row["raw_md_path"] = str(layout["raw_md"]) if layout["raw_md"].exists() else doc_row.get("raw_md_path", "")

    write_play_package(
        layout,
        doc_id=doc_id,
        title=title,
        doc_row=doc_row,
        roles=structured.get("roles", []),
        scenes=structured.get("scenes", []),
        dialogues=structured.get("dialogues", []),
        performances=structured.get("performances", []),
        relations=structured.get("relations", []),
        relations_aggregated=structured.get("relations_aggregated", []),
        themes=structured.get("themes", []),
        themes_aggregated=structured.get("themes_aggregated", []),
        entity_aliases=structured.get("entity_aliases", []),
        theme_pairs=structured.get("theme_pairs", []),
        narrative_curve=structured.get("narrative_curve", []),
        network_metrics=structured.get("network_metrics", []),
        structured=structured,
        structured_raw=structured.get("structured_raw"),
    )
    return True


def run_repack_only(args: argparse.Namespace) -> int:
    out_root: Path = args.output_dir.resolve()
    if not out_root.exists():
        print(f"Output directory not found: {out_root}", file=sys.stderr)
        return 1
    count = 0
    for structured_path in sorted(out_root.rglob("structured.json")):
        if repack_one_play(structured_path.parent, out_root):
            count += 1
    print(f"[REPACK-ONLY] reorganized {count} plays under {out_root}")
    if count == 0:
        return 1
    return 0


def run_combine_only(args: argparse.Namespace) -> int:
    out_root: Path = args.output_dir.resolve()
    if not out_root.exists():
        print(f"Output directory not found: {out_root}", file=sys.stderr)
        return 1
    apply_default_combined_paths(out_root, args)
    buckets = collect_combined_from_output(out_root, args.collection_prefix or "")
    if not buckets["docs"]:
        print(f"No structured.json found under {out_root}", file=sys.stderr)
        return 1
    write_combined_exports(args, buckets)
    ready = sum(1 for d in buckets["docs"] if d.get("analysis_ready") in (True, "True", "true", 1, "1"))
    print(f"[COMBINE-ONLY] {len(buckets['docs'])} plays, analysis_ready={ready}")
    return 0


def print_batch_summary(records: list[dict]) -> None:
    ok = sum(1 for r in records if not r.get("error") and not r.get("skipped"))
    skipped = sum(1 for r in records if r.get("skipped"))
    failed = sum(1 for r in records if r.get("error"))
    print(f"[SUMMARY] ok={ok} skipped={skipped} failed={failed} total={len(records)}")


def main() -> int:
    load_local_env()
    args = build_argparser().parse_args()
    if args.combine_only:
        return run_combine_only(args)
    if args.repack_only:
        return run_repack_only(args)
    if args.status_only:
        return run_status_only(args)

    if not args.input_dir and not args.files:
        print("Need --input-dir or --files (or use --combine-only / --repack-only / --status-only).", file=sys.stderr)
        return 2

    token = os.getenv("MINERU_TOKEN", "").strip()
    if not token:
        print(
            "MINERU_TOKEN is required.\n"
            "  PowerShell（当前窗口）:\n"
            '    $env:MINERU_TOKEN = "你的MinerU密钥"\n'
            "  或在项目根目录创建 .env 文件（参考 .env.example）:\n"
            "    MINERU_TOKEN=你的密钥\n"
            "    DEEPSEEK_API_KEY=你的密钥",
            file=sys.stderr,
        )
        return 2

    files = iter_files(args.input_dir, args.files, recursive=not args.no_recursive)
    if args.limit and args.limit > 0:
        files = files[: args.limit]
    if not files:
        print("No PDF files found.", file=sys.stderr)
        return 1

    print(f"[PLAN] PDF count={len(files)}  chunk_size={args.chunk_size}  batches≈{(len(files)+args.chunk_size-1)//args.chunk_size}")
    print(f"[PLAN] llm_enabled={bool(args.llm_enabled)}  parser={PARSER_VERSION}  skip_existing={not args.no_skip_existing}")

    input_root = args.input_dir.resolve() if args.input_dir else common_input_root(files)
    rel_paths = [relative_pdf_path(p, input_root) for p in files]

    out_root: Path = args.output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    cfg = MinerUConfig(
        token=token,
        model_version=args.model,
        language=args.language,
        wait_seconds=args.wait,
        timeout_minutes=args.timeout_minutes,
        chunk_size=args.chunk_size,
        recursive=not args.no_recursive,
        clean_md=args.clean_md,
        skip_existing=not args.no_skip_existing,
        trust_env=bool(args.use_env_proxy),
        keep_md=bool(args.keep_md),
        keep_transient=bool(args.keep_transient),
        structured_only=bool(args.structured_only),
    )

    llm_cfg = LLMConfig(
        enabled=bool(args.llm_enabled),
        api_key=os.getenv(args.llm_api_key_env, "").strip(),
        base_url=args.llm_base_url,
        model=args.llm_model,
        timeout_seconds=args.llm_timeout_seconds,
        max_tokens=args.llm_max_tokens,
        max_input_chars=args.llm_max_input_chars,
        max_scene_lines=args.llm_max_scene_lines,
        max_role_evidence=args.llm_max_role_evidence,
        enrich_dialogues=not args.no_llm_dialogue,
        enrich_relations=not args.no_llm_relation_refine,
        max_dialogue_llm_lines=args.llm_max_dialogue_lines,
        dialogue_batch_size=args.llm_dialogue_batch_size,
        max_relation_refine_pairs=args.llm_max_relation_refine,
        roles_batch_size=args.llm_roles_batch_size,
    )

    all_records: list[dict] = []
    all_docs: list[dict] = []
    all_roles: list[dict] = []
    all_scenes: list[dict] = []
    all_dialogues: list[dict] = []
    all_performances: list[dict] = []
    all_relations: list[dict] = []
    all_relations_aggregated: list[dict] = []
    all_themes: list[dict] = []
    all_themes_aggregated: list[dict] = []
    all_entity_aliases: list[dict] = []
    all_theme_pairs: list[dict] = []
    all_narrative_curve: list[dict] = []
    all_network_metrics: list[dict] = []
    all_structured: list[dict] = []

    llm_session = build_session(trust_env=False) if llm_cfg.enabled else None
    mineru_session = build_session(trust_env=cfg.trust_env)

    with mineru_session as session:
        for batch_files, batch_rel_paths in zip(chunked(files, cfg.chunk_size), chunked(rel_paths, cfg.chunk_size)):
            batch_records = convert_one_batch(session, cfg, batch_files, batch_rel_paths, out_root, llm_cfg, llm_session)
            all_records.extend(batch_records)

            for rec in batch_records:
                if rec.get("error"):
                    continue
                structured_path = Path(rec["structured_json"])
                if not structured_path.exists():
                    continue
                structured = json.loads(structured_path.read_text(encoding="utf-8"))
                all_structured.append(structured)

                doc_id = rec["doc_id"]
                row_collection_meta = {
                    "doc_id": doc_id,
                    "collection_dir": rec.get("collection_dir", ""),
                    "collection_code": rec.get("collection_code", ""),
                    "collection_name": rec.get("collection_name", ""),
                    "collection_label": rec.get("collection_label", ""),
                    "work_code": rec.get("work_code", ""),
                    "work_title_hint": rec.get("work_title_hint", ""),
                    "source_file": rec.get("source_file", ""),
                    "source_path": rec.get("source_path", ""),
                    "relative_path": rec.get("relative_path", ""),
                    "title": rec.get("title", rec.get("play_title", "")),
                }

                sm = structured.get("metadata", {})
                doc_row = {
                    **row_collection_meta,
                    "aliases": "；".join(sm.get("aliases", []) or []) if isinstance(sm.get("aliases"), list) else rec.get("aliases", ""),
                    "alias_text": sm.get("alias_text", rec.get("alias_text", "")),
                    "period_hint": sm.get("period_hint", rec.get("period_hint", "")),
                    "genre_hint": sm.get("genre_hint", rec.get("genre_hint", "")),
                    "synopsis": sm.get("synopsis", rec.get("synopsis", "")),
                    "note_text": sm.get("note_text", rec.get("note_text", "")),
                    "doc_tags": "；".join(sm.get("doc_tags", []) or []) if isinstance(sm.get("doc_tags"), list) else rec.get("doc_tags", ""),
                    "text_length": sm.get("text_length", 0),
                    "parse_quality_score": sm.get("parse_quality_score", rec.get("parse_quality_score", 0)),
                    "parse_quality_label": sm.get("parse_quality_label", ""),
                    "analysis_ready": sm.get("analysis_ready", False),
                    "parser_version": sm.get("parser_version", rec.get("parser_version", "")),
                    "scene_count": sm.get("scene_count", rec.get("scene_count", 0)),
                    "role_count": sm.get("role_count", rec.get("role_count", 0)),
                    "dialogue_count": sm.get("dialogue_count", rec.get("dialogue_count", 0)),
                    "performance_count": sm.get("performance_count", rec.get("performance_count", 0)),
                    "relation_count": sm.get("relation_count", rec.get("relation_count", 0)),
                    "aggregated_relation_count": sm.get("aggregated_relation_count", rec.get("aggregated_relation_count", 0)),
                    "theme_count": sm.get("theme_count", rec.get("theme_count", 0)),
                    "aggregated_theme_count": sm.get("aggregated_theme_count", rec.get("aggregated_theme_count", 0)),
                    "entity_alias_count": sm.get("entity_alias_count", 0),
                    "theme_pair_count": sm.get("theme_pair_count", 0),
                    "llm_enabled": sm.get("llm_enabled", rec.get("llm_enabled", False)),
                    "llm_model": sm.get("llm_model", rec.get("llm_model", "")),
                    "state": rec.get("state", ""),
                    "raw_md_path": rec.get("raw_md_path", ""),
                    "cleaned_md_path": rec.get("cleaned_md_path", ""),
                    "structured_raw_json": rec.get("structured_raw_json", ""),
                    "structured_json": rec.get("structured_json", ""),
                }
                all_docs.append(doc_row)
                all_roles.extend(merge_collection_row(r, row_collection_meta) for r in structured.get("roles", []))
                all_scenes.extend(merge_collection_row(s, row_collection_meta) for s in structured.get("scenes", []))
                all_dialogues.extend(merge_collection_row(d, row_collection_meta) for d in structured.get("dialogues", []))
                all_performances.extend(merge_collection_row(p, row_collection_meta) for p in structured.get("performances", []))
                all_relations.extend(merge_collection_row(r, row_collection_meta) for r in structured.get("relations", []))
                all_relations_aggregated.extend(merge_collection_row(r, row_collection_meta) for r in structured.get("relations_aggregated", []))
                all_themes.extend(merge_collection_row(t, row_collection_meta) for t in structured.get("themes", []))
                all_themes_aggregated.extend(merge_collection_row(t, row_collection_meta) for t in structured.get("themes_aggregated", []))
                all_entity_aliases.extend(merge_collection_row(a, row_collection_meta) for a in structured.get("entity_aliases", []))
                all_theme_pairs.extend(merge_collection_row(t, row_collection_meta) for t in structured.get("theme_pairs", []))
                all_narrative_curve.extend(merge_collection_row(n, row_collection_meta) for n in structured.get("narrative_curve", []))
                all_network_metrics.extend(merge_collection_row(n, row_collection_meta) for n in structured.get("network_metrics", []))

    print_batch_summary(all_records)

    if args.manifest:
        write_manifest(args.manifest.resolve(), all_records)
        print(f"[MANIFEST] saved -> {args.manifest.resolve()}")

    if args.auto_combine:
        apply_default_combined_paths(out_root, args)
        buckets = collect_combined_from_output(out_root, args.collection_prefix or "")
        if buckets.get("docs"):
            write_combined_exports(args, buckets)
            ready = sum(
                1 for d in buckets["docs"]
                if d.get("analysis_ready") in (True, "True", "true", 1, "1")
            )
            print(f"[AUTO-COMBINE] {len(buckets['docs'])} plays, analysis_ready={ready}")
        else:
            print("[AUTO-COMBINE] no structured.json found, skipped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
