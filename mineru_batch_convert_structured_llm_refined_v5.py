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
- removes duplicated function definitions;
- adds optional LLM enrichment for roles / scenes / relations / themes;
- keeps only analysis-relevant fields by default;
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

PARSER_VERSION = "2026-05-16-r1"

ROLE_SECTION_MARKERS = ("主要角色", "角色表", "剧中人")
ROLE_SECTION_END_MARKERS = ("情节", "注释", "第一场", "【第一场】")
SCENE_MARKER_RE = re.compile(r"^\s*【?\s*第\s*[一二三四五六七八九十百零〇0-9]+\s*场\s*】?\s*$")
SCENE_BODY_RE = re.compile(r"第\s*[一二三四五六七八九十百零〇0-9]+\s*场")
PAGE_HEADER_RE = re.compile(r"^\s*中国京剧戏考\b.*$")
PAGE_FOOTER_URL_RE = re.compile(r"^\s*https?://scripts\.xikao\.com/play/\d+\s*$")
TCPDF_RE = re.compile(r"^\s*Powered by TCPDF \(www\.tcpdf\.org\)\s*$")
STANDALONE_PAGE_NO_RE = re.compile(r"^\s*\d+\s*$")
BOILERPLATE_RE = re.compile(r"^\s*根据《戏考》第一册整理\s*$")

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
    "四上手引赵云急急风过场", "军士", "差役", "家人", "院子", "随从", "侍从"
}

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
    timeout_seconds: int = 60
    max_tokens: int = 1400
    max_input_chars: int = 7000
    max_scene_lines: int = 100
    max_role_evidence: int = 12


# -------------------------
# Generic helpers
# -------------------------


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
            return pdf_path.relative_to(input_root)
        except ValueError:
            pass
    return Path(pdf_path.name)


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
            print(f"[MinerU] batch={batch_id} state={state}", file=sys.stderr)
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


def split_scene_marker(line: str) -> str | None:
    text = normalize_text(line)
    if SCENE_MARKER_RE.match(text):
        return re.sub(r"\s+", "", text)
    return None


def is_role_section_marker(line: str) -> bool:
    text = normalize_text(line)
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
        text = normalize_text(raw)
        if not text:
            continue
        if is_role_section_marker(text):
            start_idx = i + 1
            break

    if start_idx is None:
        return roles, set()

    for i in range(start_idx, len(lines)):
        text = normalize_text(lines[i])
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
        speaker = normalize_text(row.get("speaker", ""))
        cue = normalize_text(row.get("cue", ""))
        text = normalize_text(row.get("text", ""))
        if not (speaker or cue or text):
            continue
        part = f"{speaker}（{cue}）{text}".strip()
        if len(chunks) >= max_lines:
            break
        if total + len(part) > max_chars:
            break
        chunks.append(part)
        total += len(part)
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

    for scene in scenes:
        idx = int(scene["scene_index"])
        rows = by_scene.get(idx, [])
        transcript = compact_scene_transcript(rows, max_lines=llm.max_scene_lines, max_chars=llm.max_input_chars)
        characters = sorted({normalize_text(r.get("speaker", "")) for r in rows if normalize_text(r.get("speaker", ""))})
        if not transcript.strip():
            enriched_scenes.append(scene)
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
        row = dict(scene)
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
        enriched_scenes.append(row)

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
    by_scene: dict[int, list[dict]] = defaultdict(list)
    for row in dialogues:
        by_scene[int(row["scene_index"])].append(row)

    relations: list[dict] = []
    candidate_roles = set(role_names) | COMMON_STAGE_SPEAKERS
    for idx, rows in by_scene.items():
        # 1) speaker adjacency relations
        prev_speaker = ""
        prev_text = ""
        for row in rows:
            speaker = normalize_text(row.get("speaker", ""))
            text = normalize_text(row.get("text", ""))
            if speaker and prev_speaker and speaker != prev_speaker:
                relations.append({
                    "doc_title": doc_title,
                    "scene_index": idx,
                    "scene": row.get("scene", ""),
                    "source": prev_speaker,
                    "target": speaker,
                    "relation_type": "对话/应答",
                    "weight": 1,
                    "evidence": prev_text[:60],
                    "derived_by": "adjacency",
                })
            prev_speaker = speaker or prev_speaker
            prev_text = text or prev_text

        # 2) explicit mentions and imperative relations
        for row in rows:
            speaker = normalize_text(row.get("speaker", ""))
            text = normalize_text(row.get("text", ""))
            if not speaker or not text:
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
        text = normalize_text(raw)
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
    doc_context = {
        "play_title": play_title,
        **collection_meta,
    }

    if llm_cfg and llm_cfg.enabled and llm_session is not None:
        preface_meta = llm_enrich_documents(llm_session, llm_cfg, preface_meta, doc_context, [])

    roles, role_names = parse_role_section(lines)
    scenes, dialogues = parse_scenes_and_dialogues(lines, source_meta, role_names=role_names)
    for sc in scenes:
        sc.setdefault("scene_kind", "preface" if int(sc.get("scene_index", 0)) == 0 else "scene")

    # Fallback to dialogue speakers when the role section is missing or incomplete.
    if not roles:
        roles = fallback_roles_from_dialogues(dialogues)
        role_names = {normalize_text(r.get("role_name", "")) for r in roles if normalize_text(r.get("role_name", ""))}

    # Always enrich roles heuristically first so the pipeline works without LLM.
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

    relations: list[dict] = []
    themes: list[dict] = []

    # Build performance rows and heuristic relations / themes before optional LLM enrichment.
    performance_rows = build_performance_rows(dialogues, role_names, doc_id, play_title)
    if not llm_cfg or not llm_cfg.enabled or llm_session is None:
        relations = heuristic_relations_from_dialogues(dialogues, role_names, play_title)
    else:
        roles = llm_enrich_roles(llm_session, llm_cfg, roles, dialogues, play_title)
        scenes, relations, themes = llm_enrich_scenes_and_relations(llm_session, llm_cfg, scenes, dialogues, play_title)
        # Merge heuristic relations so adjacency/mention edges are not lost when LLM is on.
        relations = dedupe_rows(relations + heuristic_relations_from_dialogues(dialogues, role_names, play_title),
                                ["doc_title", "scene_index", "source", "target", "relation_type"])

    if not relations:
        relations = heuristic_relations_from_dialogues(dialogues, role_names, play_title)

    if not themes:
        for scene in scenes:
            idx = int(scene["scene_index"])
            rows = [r for r in dialogues if int(r["scene_index"]) == idx]
            theme_labels = simple_theme_fallback([f"{r.get('speaker','')} {r.get('cue','')} {r.get('text','')}" for r in rows])
            scene["theme_labels"] = "；".join(theme_labels)
            if not scene.get("summary"):
                scene["summary"] = fallback_scene_summary(rows)
            if not scene.get("conflict_stage"):
                scene["conflict_stage"] = "铺垫" if idx == 0 else ("收束" if idx == max(s["scene_index"] for s in scenes) else "发展")
            if not scene.get("scene_function"):
                scene["scene_function"] = "过渡" if idx == 0 else "推进"
            if not scene.get("tension_level"):
                tension = 1 + min(4, sum(1 for r in rows if any(k in normalize_text(r.get("text", "")) for k in ["死", "自刎", "劈", "哭", "惊", "病", "吐"])) )
                scene["tension_level"] = tension
            for theme in theme_labels:
                themes.append({
                    "doc_title": play_title,
                    "scene_index": idx,
                    "scene": scene.get("scene", ""),
                    "theme_label": theme,
                    "evidence": "",
                    "derived_by": "keyword",
                })

    # Aggregate scene-level counts in a way that serves later visual analysis.
    by_scene = defaultdict(list)
    for d in dialogues:
        by_scene[int(d["scene_index"])].append(d)

    max_scene_index = max((int(s.get("scene_index", 0)) for s in scenes), default=0)
    for scene in scenes:
        idx = int(scene["scene_index"])
        rows = by_scene.get(idx, [])
        speakers = []
        for r in rows:
            sp = normalize_text(r.get("speaker", ""))
            if sp and sp not in speakers:
                speakers.append(sp)
        scene["characters_present"] = "；".join(speakers)
        scene["key_characters"] = scene.get("key_characters", "") or scene["characters_present"]
        scene["line_count"] = len(rows)
        scene["dialogue_count"] = sum(1 for r in rows if r.get("row_type") == "dialogue")
        scene["lyric_count"] = sum(1 for r in rows if r.get("row_type") == "lyric")
        scene["stage_direction_count"] = sum(1 for r in rows if r.get("row_type") == "stage_direction")
        scene["narration_count"] = sum(1 for r in rows if r.get("row_type") == "narration")
        scene.setdefault("summary", fallback_scene_summary(rows))
        scene.setdefault("conflict_stage", "铺垫" if idx == 0 else ("收束" if idx == max_scene_index else "发展"))
        scene.setdefault("scene_function", "过渡" if idx == 0 else "推进")
        scene.setdefault("tension_level", 1 + min(4, sum(1 for r in rows if any(k in normalize_text(r.get("text", "")) for k in ["死", "自刎", "劈", "哭", "惊", "病", "吐"])) ))

    aggregated_relations = dedupe_rows(relations, ["doc_title", "source", "target", "relation_type"])

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
        "line_count": len(lines),
        "role_count": len(roles),
        "scene_count": len(scenes),
        "dialogue_count": len(dialogues),
        "performance_count": len(performance_rows),
        "relation_count": len(relations),
        "aggregated_relation_count": len(aggregated_relations),
        "theme_count": len(themes),
        "llm_enabled": bool(llm_cfg.enabled) if llm_cfg else False,
        "llm_model": llm_cfg.model if llm_cfg and llm_cfg.enabled else "",
    }

    return {
        "metadata": metadata,
        "roles": roles,
        "scenes": scenes,
        "dialogues": dialogues,
        "performances": performance_rows,
        "relations": relations,
        "relations_aggregated": aggregated_relations,
        "themes": themes,
    }


def target_paths(out_root: Path, pdf_path: Path, rel_path: Path) -> tuple[Path, Path]:
    rel_parent = rel_path.parent
    stem = pdf_path.stem
    target_dir = out_root / rel_parent / stem
    md_path = target_dir / f"{stem}.md"
    return target_dir, md_path


def _csv_row_count(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with path.open("r", encoding="utf-8", newline="") as f:
        return max(0, sum(1 for _ in f) - 1)


def _structured_output_valid(structured_path: Path, scenes_csv: Path, dialogues_csv: Path, roles_csv: Path) -> bool:
    try:
        if not structured_path.exists():
            return False
        data = json.loads(structured_path.read_text(encoding="utf-8"))
        meta = data.get("metadata", {})
        if meta.get("parser_version") != PARSER_VERSION:
            return False
        if int(meta.get("scene_count", 0)) <= 0:
            return False
        if int(meta.get("dialogue_count", 0)) <= 0:
            return False
        if not scenes_csv.exists() or not dialogues_csv.exists() or not roles_csv.exists():
            return False
        if _csv_row_count(scenes_csv) <= 0 or _csv_row_count(dialogues_csv) <= 0:
            return False
        return True
    except Exception:
        return False


def convert_one_batch(
    session: requests.Session,
    cfg: MinerUConfig,
    files: Sequence[Path],
    rel_paths: Sequence[Path],
    out_root: Path,
    llm_cfg: LLMConfig | None = None,
    llm_session: requests.Session | None = None,
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

        collection_meta = infer_collection_info(rel_path)
        meta = {
            "source_file": file_name,
            "source_path": str(file_path),
            "relative_path": rel_path.as_posix(),
            **collection_meta,
            "batch_id": batch_id,
            "state": state,
            "full_zip_url": zip_url,
            "target_dir": str(target_dir),
            "md_path": str(md_path),
            "documents_csv": str(target_dir / "documents.csv"),
            "roles_csv": str(target_dir / "roles.csv"),
            "scenes_csv": str(target_dir / "scenes.csv"),
            "dialogues_csv": str(target_dir / "dialogues.csv"),
            "performances_csv": str(target_dir / "performances.csv"),
            "relations_csv": str(target_dir / "relations.csv"),
            "themes_csv": str(target_dir / "themes.csv"),
            "structured_json": str(target_dir / "structured.json"),
        }

        if state != "done" or not zip_url:
            meta["error"] = item.get("err_msg") or "unexpected state / missing zip url"
            processed.append(meta)
            print(f"[WARN] {file_name} -> {meta['error']}", file=sys.stderr)
            continue

        if cfg.skip_existing and _structured_output_valid(
            Path(meta["structured_json"]),
            Path(meta["scenes_csv"]),
            Path(meta["dialogues_csv"]),
            Path(meta["roles_csv"]),
        ):
            meta["skipped"] = True
            processed.append(meta)
            print(f"[SKIP] {file_name} -> {md_path}", file=sys.stderr)
            continue

        extract_dir = download_and_extract_zip(session, zip_url, target_dir)
        full_md = locate_full_md(extract_dir)
        md_text = full_md.read_text(encoding="utf-8", errors="ignore")
        if cfg.clean_md:
            md_text = clean_markdown_text(md_text)

        target_dir.mkdir(parents=True, exist_ok=True)
        if cfg.keep_md:
            md_path.write_text(md_text, encoding="utf-8")

        structured = parse_markdown_structure(
            md_text,
            source_meta={
                "source_file": file_name,
                "source_path": str(file_path),
                "relative_path": rel_path.as_posix(),
                "batch_id": batch_id,
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

        # Document-level row
        doc_row = {
            "doc_id": stable_data_id(rel_path),
            "source_file": file_name,
            "source_path": str(file_path),
            "relative_path": rel_path.as_posix(),
            "title": structured["metadata"].get("play_title", ""),
            "parser_version": structured["metadata"].get("parser_version", ""),
            "scene_count": len(scenes),
            "role_count": len(roles),
            "dialogue_count": len(dialogues),
            "performance_count": len(performances),
            "relation_count": len(relations),
            "aggregated_relation_count": len(relations_aggregated),
            "theme_count": len(themes),
            "llm_enabled": structured["metadata"].get("llm_enabled", False),
            "llm_model": structured["metadata"].get("llm_model", ""),
            "state": state,
        }

        write_csv(
            target_dir / "documents.csv",
            [doc_row],
            [
                "doc_id", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "source_file", "source_path", "relative_path", "title", "aliases", "alias_text",
                "period_hint", "genre_hint", "synopsis", "note_text", "parser_version",
                "scene_count", "role_count", "dialogue_count", "performance_count", "relation_count", "aggregated_relation_count",
                "theme_count", "llm_enabled", "llm_model", "state",
            ],
        )
        write_csv(
            target_dir / "roles.csv",
            [dict(r, doc_id=doc_row["doc_id"]) for r in roles],
            [
                "doc_id", "role_name", "role_type_raw", "role_type_inferred", "gender_inferred", "age_inferred",
                "status_identity", "personality_tags", "behavior_tags", "speech_style", "evidence",
                "source_line", "raw", "confidence", "llm_error", "role_note",
            ],
        )
        write_csv(
            target_dir / "scenes.csv",
            [dict(s, doc_id=doc_row["doc_id"]) for s in scenes],
            [
                "doc_id", "scene_index", "scene", "line_no", "start_line", "end_line", "summary",
                "conflict_stage", "tension_level", "key_characters", "theme_labels", "scene_function",
                "characters_present", "line_count", "dialogue_count", "lyric_count", "stage_direction_count",
                "narration_count", "llm_error",
            ],
        )
        write_csv(
            target_dir / "dialogues.csv",
            [dict(d, doc_id=doc_row["doc_id"], line_category=d.get("row_type", "")) for d in dialogues],
            [
                "doc_id", "source_file", "source_path", "relative_path", "batch_id", "scene_index", "scene",
                "line_no", "row_type", "line_category", "speaker", "cue", "text", "raw_line",
            ],
        )
        write_csv(
            target_dir / "performances.csv",
            [dict(p, doc_id=doc_row["doc_id"], line_category=p.get("row_type", "")) for p in performances],
            [
                "doc_id", "doc_title", "source_file", "source_path", "relative_path", "batch_id",
                "scene_index", "scene", "line_no", "row_type", "line_category", "speaker", "cue", "perform_type",
                "perform_subtype", "action_tags", "participants", "text", "raw_line",
            ],
        )
        write_csv(
            target_dir / "relations.csv",
            [dict(r, doc_id=doc_row["doc_id"]) for r in relations],
            ["doc_id", "doc_title", "scene_index", "scene", "source", "target", "relation_type", "weight", "evidence", "derived_by"],
        )
        write_csv(
            target_dir / "themes.csv",
            [dict(t, doc_id=doc_row["doc_id"]) for t in themes],
            ["doc_id", "doc_title", "scene_index", "scene", "theme_label", "evidence", "derived_by"],
        )
        write_json(target_dir / "structured.json", structured)

        meta.update({
            "play_title": structured["metadata"].get("play_title", ""),
            "parser_version": structured["metadata"].get("parser_version", ""),
            "role_count": len(roles),
            "scene_count": len(scenes),
            "dialogue_count": len(dialogues),
            "performance_count": len(performances),
            "relation_count": len(relations),
            "theme_count": len(themes),
            "aggregated_relation_count": len(relations_aggregated),
            "llm_enabled": structured["metadata"].get("llm_enabled", False),
            "llm_model": structured["metadata"].get("llm_model", ""),
        })

        (target_dir / "mineru_result.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if not cfg.keep_transient:
            shutil.rmtree(target_dir / "extracted", ignore_errors=True)
            zip_path = target_dir / "result.zip"
            if zip_path.exists():
                zip_path.unlink()

        processed.append(meta)
        print(f"[OK] {file_name} -> {md_path if cfg.keep_md else target_dir / 'structured.json'}")

    return processed


# -------------------------
# CLI / outputs
# -------------------------


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Batch convert PDFs to Markdown using MinerU and structure Jingju scripts.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input-dir", type=Path, help="Directory containing PDFs")
    src.add_argument("--files", nargs="*", type=Path, help="Explicit PDF files")

    p.add_argument("--output-dir", type=Path, required=True, help="Output directory")
    p.add_argument("--manifest", type=Path, help="Optional CSV manifest output path")
    p.add_argument("--combined-docs-csv", type=Path, help="Optional combined document CSV")
    p.add_argument("--combined-roles-csv", type=Path, help="Optional combined roles CSV")
    p.add_argument("--combined-scenes-csv", type=Path, help="Optional combined scenes CSV")
    p.add_argument("--combined-dialogue-csv", type=Path, help="Optional combined dialogues CSV")
    p.add_argument("--combined-dialogue-jsonl", type=Path, help="Optional combined dialogues JSONL")
    p.add_argument("--combined-performances-csv", type=Path, help="Optional combined performance events CSV")
    p.add_argument("--combined-performances-jsonl", type=Path, help="Optional combined performance events JSONL")
    p.add_argument("--combined-relations-csv", type=Path, help="Optional combined relations CSV")
    p.add_argument("--combined-themes-csv", type=Path, help="Optional combined themes CSV")
    p.add_argument("--combined-structured-jsonl", type=Path, help="Optional combined structured JSONL")

    p.add_argument("--model", default=os.getenv("MINERU_MODEL", "vlm"), help="pipeline|vlm|MinerU-HTML")
    p.add_argument("--language", default=os.getenv("MINERU_LANG", "ch"), help="Language code")
    p.add_argument("--wait", type=int, default=int(os.getenv("MINERU_WAIT", "15")), help="Poll interval seconds")
    p.add_argument("--timeout-minutes", type=int, default=int(os.getenv("MINERU_TIMEOUT", "120")), help="Per-batch timeout")
    p.add_argument("--chunk-size", type=int, default=50, help="Local upload batch size")
    p.add_argument("--no-recursive", action="store_true", help="Do not scan subfolders")
    p.add_argument("--clean-md", action="store_true", help="Apply conservative cleanup to full.md")
    p.add_argument("--no-skip-existing", action="store_true", help="Rebuild outputs even if structured outputs exist")
    p.add_argument("--use-env-proxy", action="store_true", help="Allow requests to use system proxy env vars")
    p.add_argument("--keep-md", action="store_true", help="Keep cleaned Markdown beside structured outputs")
    p.add_argument("--keep-transient", action="store_true", help="Keep MinerU zip / extracted intermediates")
    p.add_argument("--structured-only", action="store_true", help="Skip keeping Markdown and only write structured artifacts")

    p.add_argument("--llm-enabled", action="store_true", help="Enable LLM-assisted enrichment")
    p.add_argument("--llm-base-url", default=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"), help="OpenAI-compatible base URL")
    p.add_argument("--llm-model", default=os.getenv("LLM_MODEL", "deepseek-chat"), help="LLM model name")
    p.add_argument("--llm-api-key-env", default=os.getenv("LLM_API_KEY_ENV", "DEEPSEEK_API_KEY"), help="Environment variable holding LLM API key")
    p.add_argument("--llm-timeout-seconds", type=int, default=int(os.getenv("LLM_TIMEOUT_SECONDS", "60")), help="LLM request timeout")
    p.add_argument("--llm-max-tokens", type=int, default=int(os.getenv("LLM_MAX_TOKENS", "1400")), help="LLM max tokens")
    p.add_argument("--llm-max-input-chars", type=int, default=int(os.getenv("LLM_MAX_INPUT_CHARS", "7000")), help="Max chars sent to LLM per scene")
    p.add_argument("--llm-max-scene-lines", type=int, default=int(os.getenv("LLM_MAX_SCENE_LINES", "100")), help="Max rows per scene sent to LLM")
    p.add_argument("--llm-max-role-evidence", type=int, default=int(os.getenv("LLM_MAX_ROLE_EVIDENCE", "12")), help="Max evidence rows per role")
    return p


def write_manifest(manifest_path: Path, records: list[dict]) -> None:
    fieldnames = [
        "source_file", "source_path", "relative_path", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
        "play_title", "parser_version", "batch_id", "state",
        "full_zip_url", "target_dir", "md_path", "documents_csv", "roles_csv", "scenes_csv", "dialogues_csv", "performances_csv", "relations_csv",
        "themes_csv", "structured_json", "skipped", "error", "role_count", "scene_count", "dialogue_count", "performance_count",
        "relation_count", "theme_count", "aggregated_relation_count", "llm_enabled", "llm_model",
    ]
    write_csv(manifest_path, records, fieldnames)


def main() -> int:
    args = build_argparser().parse_args()
    token = os.getenv("MINERU_TOKEN", "").strip()
    if not token:
        print("MINERU_TOKEN is required.", file=sys.stderr)
        return 2

    files = iter_files(args.input_dir, args.files, recursive=not args.no_recursive)
    if not files:
        print("No PDF files found.", file=sys.stderr)
        return 1

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

    if cfg.structured_only:
        cfg.keep_md = False

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
    )
    llm_session = build_llm_session() if llm_cfg.enabled else None

    all_records: list[dict] = []
    all_docs: list[dict] = []
    all_roles: list[dict] = []
    all_scenes: list[dict] = []
    all_dialogues: list[dict] = []
    all_performances: list[dict] = []
    all_relations: list[dict] = []
    all_themes: list[dict] = []
    all_structured: list[dict] = []

    with build_session(trust_env=cfg.trust_env) as session:
        for batch_files, batch_rel_paths in zip(chunked(files, cfg.chunk_size), chunked(rel_paths, cfg.chunk_size)):
            batch_records = convert_one_batch(session, cfg, batch_files, batch_rel_paths, out_root, llm_cfg=llm_cfg, llm_session=llm_session)
            all_records.extend(batch_records)

            for rec in batch_records:
                if rec.get("error") or rec.get("skipped"):
                    continue
                structured_path = Path(rec["structured_json"])
                if not structured_path.exists():
                    continue
                structured = json.loads(structured_path.read_text(encoding="utf-8"))
                all_structured.append(structured)
                doc_id = stable_data_id(Path(rec["relative_path"]))
                doc_meta = structured.get("metadata", {})
                collection_meta = {k: doc_meta.get(k, "") for k in ["collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint"]}
                all_docs.append({
                    "doc_id": doc_id,
                    **collection_meta,
                    "source_file": rec.get("source_file", ""),
                    "source_path": rec.get("source_path", ""),
                    "relative_path": rec.get("relative_path", ""),
                    "title": doc_meta.get("play_title", ""),
                    "aliases": doc_meta.get("aliases", []),
                    "alias_text": doc_meta.get("alias_text", ""),
                    "period_hint": doc_meta.get("period_hint", ""),
                    "genre_hint": doc_meta.get("genre_hint", ""),
                    "synopsis": doc_meta.get("synopsis", ""),
                    "note_text": doc_meta.get("note_text", ""),
                    "parser_version": doc_meta.get("parser_version", ""),
                    "scene_count": doc_meta.get("scene_count", 0),
                    "role_count": doc_meta.get("role_count", 0),
                    "dialogue_count": doc_meta.get("dialogue_count", 0),
                    "performance_count": doc_meta.get("performance_count", 0),
                    "relation_count": doc_meta.get("relation_count", 0),
                    "aggregated_relation_count": doc_meta.get("aggregated_relation_count", 0),
                    "theme_count": doc_meta.get("theme_count", 0),
                    "llm_enabled": doc_meta.get("llm_enabled", False),
                    "llm_model": doc_meta.get("llm_model", ""),
                    "state": rec.get("state", ""),
                })
                row_collection_meta = {k: doc_meta.get(k, "") for k in ["collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint"]}
                all_roles.extend(dict(r, doc_id=doc_id, **row_collection_meta) for r in structured.get("roles", []))
                all_scenes.extend(dict(s, doc_id=doc_id, **row_collection_meta) for s in structured.get("scenes", []))
                all_dialogues.extend(dict(d, doc_id=doc_id, **row_collection_meta) for d in structured.get("dialogues", []))
                all_performances.extend(dict(p, doc_id=doc_id, **row_collection_meta) for p in structured.get("performances", []))
                for rel in (structured.get("relations_aggregated", []) or structured.get("relations", [])):
                    all_relations.append(dict(rel, doc_id=doc_id, **row_collection_meta))
                for theme in structured.get("themes", []):
                    all_themes.append(dict(theme, doc_id=doc_id, **row_collection_meta))

    if args.manifest:
        write_manifest(args.manifest.resolve(), all_records)
        print(f"[MANIFEST] saved -> {args.manifest.resolve()}")

    if args.combined_docs_csv and all_docs:
        write_csv(
            args.combined_docs_csv.resolve(),
            all_docs,
            [
                "doc_id", "collection_dir", "collection_code", "collection_name", "collection_label", "work_code", "work_title_hint",
                "source_file", "source_path", "relative_path", "title", "aliases", "alias_text",
                "period_hint", "genre_hint", "synopsis", "note_text", "parser_version",
                "scene_count", "role_count", "dialogue_count", "performance_count", "relation_count", "aggregated_relation_count",
                "theme_count", "llm_enabled", "llm_model", "state",
            ],
        )
        print(f"[COMBINED] docs CSV -> {args.combined_docs_csv.resolve()}")

    if args.combined_roles_csv and all_roles:
        write_csv(
            args.combined_roles_csv.resolve(),
            all_roles,
            [
                "doc_id", "role_name", "role_type_raw", "role_type_inferred", "gender_inferred", "age_inferred",
                "status_identity", "personality_tags", "behavior_tags", "speech_style", "evidence",
                "source_line", "raw", "confidence", "llm_error", "role_note",
            ],
        )
        print(f"[COMBINED] roles CSV -> {args.combined_roles_csv.resolve()}")

    if args.combined_scenes_csv and all_scenes:
        write_csv(
            args.combined_scenes_csv.resolve(),
            all_scenes,
            [
                "doc_id", "scene_index", "scene", "line_no", "start_line", "end_line", "summary",
                "conflict_stage", "tension_level", "key_characters", "theme_labels", "scene_function",
                "characters_present", "line_count", "dialogue_count", "lyric_count", "stage_direction_count",
                "narration_count", "llm_error",
            ],
        )
        print(f"[COMBINED] scenes CSV -> {args.combined_scenes_csv.resolve()}")

    if args.combined_dialogue_csv and all_dialogues:
        write_csv(
            args.combined_dialogue_csv.resolve(),
            all_dialogues,
            ["doc_id", "source_file", "source_path", "relative_path", "batch_id", "scene_index", "scene", "line_no", "row_type", "line_category", "speaker", "cue", "text", "raw_line"],
        )
        print(f"[COMBINED] dialogues CSV -> {args.combined_dialogue_csv.resolve()}")

    if args.combined_dialogue_jsonl and all_dialogues:
        write_jsonl(args.combined_dialogue_jsonl.resolve(), all_dialogues)
        print(f"[COMBINED] dialogues JSONL -> {args.combined_dialogue_jsonl.resolve()}")

    if args.combined_performances_csv and all_performances:
        write_csv(
            args.combined_performances_csv.resolve(),
            all_performances,
            ["doc_id", "doc_title", "source_file", "source_path", "relative_path", "batch_id", "scene_index", "scene", "line_no", "row_type", "line_category", "speaker", "cue", "perform_type", "perform_subtype", "action_tags", "participants", "text", "raw_line"],
        )
        print(f"[COMBINED] performances CSV -> {args.combined_performances_csv.resolve()}")

    if args.combined_performances_jsonl and all_performances:
        write_jsonl(args.combined_performances_jsonl.resolve(), all_performances)
        print(f"[COMBINED] performances JSONL -> {args.combined_performances_jsonl.resolve()}")

    if args.combined_relations_csv and all_relations:
        write_csv(
            args.combined_relations_csv.resolve(),
            all_relations,
            ["doc_id", "doc_title", "scene_index", "scene", "source", "target", "relation_type", "weight", "evidence", "derived_by"],
        )
        print(f"[COMBINED] relations CSV -> {args.combined_relations_csv.resolve()}")

    if args.combined_themes_csv and all_themes:
        write_csv(
            args.combined_themes_csv.resolve(),
            all_themes,
            ["doc_id", "doc_title", "scene_index", "scene", "theme_label", "evidence", "derived_by"],
        )
        print(f"[COMBINED] themes CSV -> {args.combined_themes_csv.resolve()}")

    if args.combined_structured_jsonl and all_structured:
        write_jsonl(args.combined_structured_jsonl.resolve(), all_structured)
        print(f"[COMBINED] structured JSONL -> {args.combined_structured_jsonl.resolve()}")

    return 0



# =========================
# v4 enhancements
# =========================

PARSER_VERSION = "2026-05-16-r2"


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


def llm_enrich_document_meta(
    session: requests.Session,
    llm: LLMConfig,
    preface_meta: dict,
    doc_title: str,
) -> dict:
    if not llm.enabled:
        return preface_meta

    system = (
        "你是中国京剧剧本信息抽取助手。请只返回严格JSON，不要输出解释。"
        "目标是把剧本前置信息整理成可用于后续分析的标准字段。"
    )
    user = f"""
剧名：{doc_title}
原标题/别名行：{preface_meta.get('alias_text', '')}
剧情简介：{preface_meta.get('synopsis', '')}
注释：{preface_meta.get('note_text', '')}
已有年代线索：{preface_meta.get('period_hint', '')}
已有戏种线索：{preface_meta.get('genre_hint', '')}

请输出JSON对象，字段固定为：
{{
  "aliases": [""],
  "alias_text": "",
  "period_hint": "",
  "genre_hint": "",
  "synopsis": "",
  "note_text": "",
  "doc_tags": [""],
  "confidence": 0.0
}}
要求：
- aliases 只放与剧名确实有关的别名。
- period_hint 与 genre_hint 优先使用已有线索并校正。
- synopsis 与 note_text 用简洁中文概括，不要超过两三句。
- doc_tags 可包含：忠义、权谋、战争、家庭、公案、离散、情爱、喜剧等。
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
            merged["doc_llm_confidence"] = result.get("confidence", "")
            return merged
    except Exception:
        pass
    return preface_meta


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
        "你是中国京剧剧本分析助手。请根据角色表和文本证据，输出严格JSON。"
        "只返回JSON对象，不要输出解释文字。"
    )
    enriched: list[dict] = []
    for role in roles:
        role_name = role.get("role_name", "")
        evidence = collect_role_evidence(dialogues, role_name, max_lines=llm.max_role_evidence)
        user = f"""
剧名：{doc_title}
角色名：{role_name}
原始行当标注：{role.get('role_type_raw', '')}

请根据下列证据推断角色画像：
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
  "evidence": "请用一句话概括证据依据",
  "confidence": 0.0
}}
要求：
- 仅基于证据推断，不要编造。
- personality_tags / behavior_tags / speech_style 用数组。
- 若只能判断大类，也请尽量保留原始行当细分信息。
"""
        row = dict(role)
        try:
            result = llm_chat_json(session, llm, system, user)
            if isinstance(result, dict):
                row["role_type_inferred"] = result.get("role_type_inferred", row.get("role_type_inferred", ""))
                row["gender_inferred"] = result.get("gender_inferred", row.get("gender_inferred", ""))
                row["age_inferred"] = result.get("age_inferred", row.get("age_inferred", ""))
                row["status_identity"] = result.get("status_identity", row.get("status_identity", ""))
                row["personality_tags"] = "；".join(_llm_items(result.get("personality_tags", [])))
                row["behavior_tags"] = "；".join(_llm_items(result.get("behavior_tags", [])))
                row["speech_style"] = "；".join(_llm_items(result.get("speech_style", [])))
                row["evidence"] = _llm_text(result.get("evidence", row.get("evidence", "")))
                row["confidence"] = result.get("confidence", row.get("confidence", ""))
        except Exception as e:
            row["llm_error"] = str(e)
            fallback = infer_role_from_heuristics(role_name, role.get("role_type_raw", ""), evidence)
            for k, v in fallback.items():
                row.setdefault(k, v)
        enriched.append(row)
    return enriched


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
        "你是中国京剧剧本结构化分析助手。请根据场次文本输出严格JSON，"
        "用于后续人物关系、主题、叙事结构分析。只返回JSON对象，不要输出解释文字。"
    )

    enriched_scenes: list[dict] = []
    relation_rows: list[dict] = []
    theme_rows: list[dict] = []

    for scene in scenes:
        idx = int(scene["scene_index"])
        rows = by_scene.get(idx, [])
        transcript = _scene_excerpt(rows, max_lines=llm.max_scene_lines, max_chars=llm.max_input_chars)
        characters = sorted({normalize_text(r.get("speaker", "")) for r in rows if normalize_text(r.get("speaker", ""))})
        if not transcript.strip():
            enriched_scenes.append(scene)
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
  "narrative_turning_point": "",
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
        row = dict(scene)
        try:
            result = llm_chat_json(session, llm, system, user)
            if isinstance(result, dict):
                row["summary"] = _llm_text(result.get("summary", row.get("summary", "")))
                row["conflict_stage"] = _llm_text(result.get("conflict_stage", row.get("conflict_stage", "")))
                row["tension_level"] = result.get("tension_level", row.get("tension_level", 0))
                row["scene_function"] = _llm_text(result.get("scene_function", row.get("scene_function", "")))
                row["key_characters"] = "；".join(_llm_items(result.get("key_characters", [])))
                row["theme_labels"] = "；".join(_llm_items(result.get("theme_labels", [])))
                if result.get("narrative_turning_point"):
                    row["narrative_turning_point"] = _llm_text(result.get("narrative_turning_point"))
                for rel in result.get("relations", []) or []:
                    if not isinstance(rel, dict):
                        continue
                    source = _llm_text(rel.get("source", ""))
                    target = _llm_text(rel.get("target", ""))
                    relation_type = _llm_text(rel.get("relation_type", ""))
                    if not (source and target and relation_type):
                        continue
                    relation_rows.append({
                        "doc_title": doc_title,
                        "scene_index": idx,
                        "scene": scene.get("scene", ""),
                        "source": source,
                        "target": target,
                        "relation_type": relation_type,
                        "weight": rel.get("weight", 1),
                        "evidence": _llm_text(rel.get("evidence", "")),
                        "derived_by": "llm",
                    })
                for theme in _llm_items(result.get("theme_labels", [])):
                    theme_rows.append({
                        "doc_title": doc_title,
                        "scene_index": idx,
                        "scene": scene.get("scene", ""),
                        "theme_label": theme,
                        "evidence": row.get("summary", ""),
                        "derived_by": "llm",
                    })
        except Exception as e:
            row["llm_error"] = str(e)
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
    doc_context = {
        "play_title": play_title,
        **collection_meta,
    }

    if llm_cfg and llm_cfg.enabled and llm_session is not None:
        preface_meta = llm_enrich_documents(llm_session, llm_cfg, preface_meta, doc_context, [])

    roles, role_names = parse_role_section(lines)
    scenes, dialogues = parse_scenes_and_dialogues(lines, source_meta, role_names=role_names)
    for sc in scenes:
        sc.setdefault("scene_kind", "preface" if int(sc.get("scene_index", 0)) == 0 else "scene")

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

    if llm_cfg and llm_cfg.enabled and llm_session is not None:
        preface_meta = llm_enrich_document_meta(llm_session, llm_cfg, preface_meta, play_title)
        roles = llm_enrich_roles(llm_session, llm_cfg, roles, dialogues, play_title)
        scenes, relation_rows_llm, theme_rows_llm = llm_enrich_scenes_and_relations(llm_session, llm_cfg, scenes, dialogues, play_title)
    else:
        relation_rows_llm = []
        theme_rows_llm = []

    performance_rows = build_performance_rows(dialogues, role_names, doc_id, play_title)

    heuristic_relations = heuristic_relations_from_dialogues(dialogues, role_names, play_title)
    relations = heuristic_relations + relation_rows_llm
    if not relations:
        relations = heuristic_relations
    aggregated_relations = aggregate_relations(relations)

    # Scene-level theme fallback and LLM merge.
    by_scene: dict[int, list[dict]] = defaultdict(list)
    for row in dialogues:
        by_scene[int(row["scene_index"])].append(row)
    fallback_theme_rows: list[dict] = []
    for scene in scenes:
        idx = int(scene.get("scene_index", 0))
        transcript = _scene_excerpt(by_scene.get(idx, []), max_lines=40, max_chars=3500)
        theme_labels = _llm_items(scene.get("theme_labels", []))
        if not theme_labels:
            theme_labels = simple_theme_fallback([transcript, preface_meta.get("synopsis", ""), preface_meta.get("note_text", "")])
        if not theme_labels:
            continue
        for theme in theme_labels:
            fallback_theme_rows.append({
                "doc_title": play_title,
                "scene_index": idx,
                "scene": scene.get("scene", ""),
                "theme_label": theme,
                "evidence": scene.get("summary", transcript[:80]),
                "derived_by": "keyword",
            })
    themes = theme_rows_llm + fallback_theme_rows
    if not themes:
        themes = [{
            "doc_title": play_title,
            "scene_index": 0,
            "scene": "序幕/前置内容",
            "theme_label": theme,
            "evidence": preface_meta.get("synopsis", ""),
            "derived_by": "doc_keyword",
        } for theme in simple_theme_fallback([preface_meta.get("synopsis", ""), preface_meta.get("note_text", "")])]
    # de-duplicate theme rows while preserving order
    seen_theme_keys = set()
    deduped_themes = []
    for t in themes:
        key = (normalize_text(t.get("doc_title", "")), int(t.get("scene_index", 0) or 0), normalize_text(t.get("scene", "")), normalize_text(t.get("theme_label", "")))
        if key in seen_theme_keys:
            continue
        seen_theme_keys.add(key)
        deduped_themes.append(t)
    themes = deduped_themes

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
        "line_count": len(lines),
        "role_count": len(roles),
        "scene_count": len(scenes),
        "dialogue_count": len(dialogues),
        "performance_count": len(performance_rows),
        "relation_count": len(relations),
        "aggregated_relation_count": len(aggregated_relations),
        "theme_count": len(themes),
        "llm_enabled": bool(llm_cfg.enabled) if llm_cfg else False,
        "llm_model": llm_cfg.model if llm_cfg and llm_cfg.enabled else "",
    }

    return {
        "metadata": metadata,
        "roles": roles,
        "scenes": scenes,
        "dialogues": dialogues,
        "performances": performance_rows,
        "relations": relations,
        "relations_aggregated": aggregated_relations,
        "themes": themes,
    }

if __name__ == "__main__":
    raise SystemExit(main())
