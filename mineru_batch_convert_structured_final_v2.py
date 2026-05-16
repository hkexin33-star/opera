#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Batch convert local PDFs to Markdown via MinerU API, then structure the results
into analysis-ready datasets for Chinese opera scripts.

This version keeps the Markdown output and additionally produces:
1) role tables   (角色表)
2) scene tables  (场次表)
3) dialogue tables (台词三列表：人物 / 动作或提示 / 台词)
4) structured JSON for each script

Workflow:
  1) Recursively scan PDFs under --input-dir
  2) Batch-upload to MinerU
  3) Download full.md from MinerU result zip
  4) Clean Markdown conservatively (optional)
  5) Parse roles / scenes / dialogues into CSV + JSON
  6) Save per-file and combined corpus-level outputs

Notes:
  - The parsing rules are conservative and are designed for Jingju scripts.
  - The script preserves the original directory hierarchy to avoid collisions.
  - By default, requests do NOT trust system proxies, which avoids ProxyError
    issues on some machines. Use --use-env-proxy only when needed.

Environment variables:
  MINERU_TOKEN   required: API token from mineru.net
  MINERU_MODEL   optional: pipeline|vlm|MinerU-HTML (default: vlm)
  MINERU_LANG    optional: ch|en|... (default: ch)
  MINERU_WAIT    optional: poll interval seconds (default: 15)
  MINERU_TIMEOUT optional: max minutes to wait per batch (default: 120)

Examples:
  python mineru_batch_convert_structured_final.py --input-dir ./opera_dataset --output-dir ./opera_dataset_md --manifest ./mineru_manifest.csv
  python mineru_batch_convert_structured_final.py --input-dir ./opera_dataset --output-dir ./opera_dataset_md --clean-md
  python mineru_batch_convert_structured_final.py --input-dir ./opera_dataset --output-dir ./opera_dataset_md --combined-dialogue-csv ./all_dialogues.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://mineru.net"
UPLOAD_ENDPOINT = f"{API_BASE}/api/v4/file-urls/batch"
RESULT_ENDPOINT = f"{API_BASE}/api/v4/extract-results/batch"

ROLE_SECTION_MARKERS = ("主要角色", "角色表", "剧中人")
SCENE_MARKER_RE = re.compile(r"^\s*【?第[一二三四五六七八九十百零〇0-9]+场】?\s*$")
SCENE_MARKER_INLINE_RE = re.compile(r"【?第[一二三四五六七八九十百零〇0-9]+场】?")
PAGE_HEADER_RE = re.compile(r"^\s*中国京剧戏考\b.*$")
PAGE_FOOTER_URL_RE = re.compile(r"^\s*https?://scripts\.xikao\.com/play/\d+\s*$")
TCPDF_RE = re.compile(r"^\s*Powered by TCPDF \(www\.tcpdf\.org\)\s*$")

# Keep cue words broad enough for Jingju scripts but conservative enough to avoid
# over-splitting normal text.
CUE_RE = re.compile(
    r"^(?P<cue>"
    r"内白|同白|白|念|叫头|笑|唱|白口|道白|旁白|"
    r"西皮[^\s　（）()，。；;、]*"
    r"(?:慢板|二六板|原板|摇板|流水板|导板|散板|娃娃调|二黄慢板|二黄原板|二黄摇板|二黄导板)?|"
    r"快板|导板|原板|慢板|摇板|散板"
    r")$"
)

# Match a stage-direction-only line like "(二童儿同上，诸葛亮上。)" or "（诸葛亮上。）"
STAGE_DIRECTION_ONLY_RE = re.compile(r"^[（(](?P<text>.+?)[）)]$")

# Match a line beginning with speaker + optional cue + text. We support wide spaces.
SPEAKER_PREFIX_RE = re.compile(
    r"^\s*(?P<speaker>[^（(　\s][^（(]*)"
    r"(?:[　\s]*[（(](?P<cue>[^）)]{1,20})[）)])?"
    r"[　\s]*(?P<text>.*)$"
)

# Common footer / boilerplate lines to remove from cleaned markdown.
NOISE_LINE_PATTERNS = (
    PAGE_HEADER_RE,
    PAGE_FOOTER_URL_RE,
    TCPDF_RE,
)

@dataclass
class MinerUConfig:
    token: str
    model_version: str = "vlm"
    language: str = "ch"
    wait_seconds: int = 15
    timeout_minutes: int = 120
    chunk_size: int = 50
    recursive: bool = True
    clean_md: bool = False
    skip_existing: bool = True
    trust_env: bool = False
    extract_structured: bool = True


def iter_files(input_dir: Path | None, files: Sequence[Path] | None, recursive: bool = True) -> list[Path]:
    if files:
        return [p.resolve() for p in files]

    if not input_dir:
        raise ValueError("Either input_dir or files must be provided.")

    input_dir = input_dir.resolve()
    if recursive:
        paths = sorted(p.resolve() for p in input_dir.rglob("*.pdf"))
    else:
        paths = sorted(p.resolve() for p in input_dir.iterdir() if p.suffix.lower() == ".pdf")
    return paths


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
    try:
        resp = session.request(method, url, headers=headers, json=json_payload, timeout=timeout)
    except requests.RequestException:
        time.sleep(2)
        resp = session.request(method, url, headers=headers, json=json_payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


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
        "files": [
            {"name": p.name, "data_id": stable_data_id(rel)}
            for p, rel in zip(batch_files, rel_paths)
        ],
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
    """
    Conservative cleanup for OCR / layout noise:
    - normalize line endings
    - remove obvious boilerplate
    - collapse excessive blank lines
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue

        if any(pat.match(stripped) for pat in NOISE_LINE_PATTERNS):
            continue

        # Usually page labels/URLs are noise; preserve scene markers and content.
        lines.append(line.rstrip())

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + "\n"


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\u3000", " ")).strip()


def split_scene_marker(line: str) -> str | None:
    m = SCENE_MARKER_RE.match(line.strip())
    if m:
        return line.strip()
    m2 = SCENE_MARKER_INLINE_RE.search(line)
    if m2 and line.strip() == m2.group(0):
        return line.strip()
    return None


def is_role_section_marker(line: str) -> bool:
    text = line.strip()
    return any(marker in text for marker in ROLE_SECTION_MARKERS)


def parse_role_section(lines: list[str]) -> list[dict]:
    """
    Extract roles from a '主要角色' section.
    Expected patterns:
      诸葛亮：老生
      司马懿：净
      老军甲：丑
    """
    roles: list[dict] = []
    in_section = False
    for line_no, raw in enumerate(lines, start=1):
        text = normalize_text(raw)
        if not text:
            continue
        if is_role_section_marker(text):
            in_section = True
            continue
        if in_section and ("情节" in text or text.startswith("【第一场】") or SCENE_MARKER_RE.match(text)):
            break
        if not in_section:
            continue

        # Common role pattern "人物：行当/说明"
        m = re.match(r"^(?P<name>[^：:]{1,20})[：:]\s*(?P<role>.+)$", text)
        if m:
            name = m.group("name").strip()
            role = m.group("role").strip()
            if name and role:
                roles.append({
                    "role_name": name,
                    "role_type": role,
                    "role_note": "",
                    "source_line": line_no,
                    "raw": raw,
                })
            continue

        # Sometimes roles may be separated by whitespace; keep conservative.
        if " " in text or "\t" in text:
            parts = re.split(r"\s{2,}|\t+", text)
            if len(parts) == 2 and len(parts[0]) <= 20 and parts[1]:
                roles.append({
                    "role_name": parts[0].strip(),
                    "role_type": parts[1].strip(),
                    "role_note": "",
                    "source_line": line_no,
                    "raw": raw,
                })
    return roles


def classify_row_type(cue: str, text: str) -> str:
    cue_norm = normalize_text(cue)
    text_norm = normalize_text(text)
    if cue_norm and cue_norm in {"白", "念", "叫头", "笑", "唱", "内白", "同白", "道白", "旁白"}:
        if cue_norm in {"叫头", "笑"}:
            return "stage_direction"
        if cue_norm in {"旁白"}:
            return "narration"
        return "dialogue"
    if cue_norm.startswith("西皮") or cue_norm in {"快板", "导板", "原板", "慢板", "摇板", "散板"}:
        return "lyric"
    if text_norm and not cue_norm:
        return "dialogue"
    if cue_norm and not text_norm:
        return "stage_direction"
    return "dialogue"


def parse_scenes_and_dialogues(lines: list[str], source_meta: dict) -> tuple[list[dict], list[dict]]:
    scenes: list[dict] = []
    dialogues: list[dict] = []

    current_scene = {
        "scene_index": 0,
        "scene_title": "序幕/前置内容",
        "start_line": 1,
        "end_line": 0,
    }
    scene_open = False
    scene_index = 0

    for line_no, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue

        scene_marker = split_scene_marker(stripped)
        if scene_marker:
            if scene_open:
                current_scene["end_line"] = line_no - 1
                scenes.append(current_scene)
            scene_index += 1
            current_scene = {
                "scene_index": scene_index,
                "scene_title": scene_marker,
                "start_line": line_no,
                "end_line": 0,
            }
            scene_open = True
            continue

        if not scene_open:
            # pre-scene content
            scene_open = True
            current_scene = {
                "scene_index": 0,
                "scene_title": "序幕/前置内容",
                "start_line": line_no,
                "end_line": 0,
            }

        # Ignore very common page headers that may survive cleaning.
        if any(pat.match(stripped) for pat in NOISE_LINE_PATTERNS):
            continue

        # Stage-direction-only line, e.g. "(四上手引赵云急急风过场，同下。)"
        m_stage = STAGE_DIRECTION_ONLY_RE.match(stripped)
        if m_stage:
            content = normalize_text(m_stage.group("text"))
            dialogues.append({
                **source_meta,
                "scene_index": current_scene["scene_index"],
                "scene_title": current_scene["scene_title"],
                "line_no": line_no,
                "row_type": "stage_direction",
                "speaker": "",
                "cue": content,
                "text": "",
                "raw_line": raw,
            })
            current_scene["end_line"] = line_no
            continue

        # Lines like "诸葛亮　　（白）　　　　　罢了。"
        m = SPEAKER_PREFIX_RE.match(stripped)
        if m:
            speaker = normalize_text(m.group("speaker"))
            cue = normalize_text(m.group("cue") or "")
            text = normalize_text(m.group("text") or "")

            # If the speaker is actually a page title/section heading, skip.
            if speaker in {"中国京剧戏考", "主要角色", "情节"}:
                continue

            # Sometimes the "speaker" capture is actually a scene marker that slipped through.
            if split_scene_marker(speaker):
                continue

            # If cue is empty but the text is a pure cue-like marker, treat text as cue.
            if not cue and text in {"白", "念", "叫头", "笑", "唱"}:
                cue, text = text, ""

            row_type = classify_row_type(cue, text)
            dialogues.append({
                **source_meta,
                "scene_index": current_scene["scene_index"],
                "scene_title": current_scene["scene_title"],
                "line_no": line_no,
                "row_type": row_type,
                "speaker": speaker,
                "cue": cue,
                "text": text,
                "raw_line": raw,
            })
            current_scene["end_line"] = line_no
            continue

        # Fallback: lines beginning with cue markers, such as "（西皮摇板）我用兵数十年从来谨慎，"
        m_cue_front = re.match(r"^\s*[（(](?P<cue>[^）)]{1,30})[）)](?P<text>.*)$", stripped)
        if m_cue_front:
            cue = normalize_text(m_cue_front.group("cue"))
            text = normalize_text(m_cue_front.group("text"))
            row_type = classify_row_type(cue, text)
            dialogues.append({
                **source_meta,
                "scene_index": current_scene["scene_index"],
                "scene_title": current_scene["scene_title"],
                "line_no": line_no,
                "row_type": row_type,
                "speaker": "",
                "cue": cue,
                "text": text,
                "raw_line": raw,
            })
            current_scene["end_line"] = line_no
            continue

        # Otherwise, keep the line as narrative/other textual content.
        dialogues.append({
            **source_meta,
            "scene_index": current_scene["scene_index"],
            "scene_title": current_scene["scene_title"],
            "line_no": line_no,
            "row_type": "narration",
            "speaker": "",
            "cue": "",
            "text": normalize_text(stripped),
            "raw_line": raw,
        })
        current_scene["end_line"] = line_no

    if scene_open:
        if current_scene["end_line"] == 0:
            current_scene["end_line"] = len(lines)
        scenes.append(current_scene)

    return scenes, dialogues


def extract_title(lines: list[str]) -> str:
    # Prefer the first explicit 《...》 title.
    for line in lines[:20]:
        m = re.search(r"《([^》]{1,40})》", line)
        if m:
            return m.group(1).strip()
    # Fallback to the first non-empty line.
    for line in lines[:10]:
        text = normalize_text(line)
        if text:
            return text[:40]
    return ""


def parse_markdown_structure(md_text: str, source_meta: dict) -> dict:
    lines = md_text.splitlines()
    play_title = extract_title(lines)

    roles = parse_role_section(lines)
    scenes, dialogues = parse_scenes_and_dialogues(lines, source_meta)

    return {
        "metadata": {
            **source_meta,
            "play_title": play_title,
            "line_count": len(lines),
            "role_count": len(roles),
            "scene_count": len(scenes),
            "dialogue_count": len(dialogues),
        },
        "roles": roles,
        "scenes": scenes,
        "dialogues": dialogues,
    }


def target_paths(out_root: Path, pdf_path: Path, rel_path: Path) -> tuple[Path, Path]:
    rel_parent = rel_path.parent
    stem = pdf_path.stem
    target_dir = out_root / rel_parent / stem
    md_path = target_dir / f"{stem}.md"
    return target_dir, md_path


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


def write_manifest(manifest_path: Path, records: list[dict]) -> None:
    fieldnames = [
        "source_file",
        "source_path",
        "relative_path",
        "play_title",
        "batch_id",
        "state",
        "full_zip_url",
        "target_dir",
        "md_path",
        "roles_csv",
        "scenes_csv",
        "dialogues_csv",
        "structured_json",
        "skipped",
        "error",
    ]
    write_csv(manifest_path, records, fieldnames)


def convert_one_batch(
    session: requests.Session,
    cfg: MinerUConfig,
    files: Sequence[Path],
    rel_paths: Sequence[Path],
    out_root: Path,
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

        meta = {
            "source_file": file_name,
            "source_path": str(file_path),
            "relative_path": rel_path.as_posix(),
            "batch_id": batch_id,
            "state": state,
            "full_zip_url": zip_url,
            "target_dir": str(target_dir),
            "md_path": str(md_path),
            "roles_csv": str(target_dir / "roles.csv"),
            "scenes_csv": str(target_dir / "scenes.csv"),
            "dialogues_csv": str(target_dir / "dialogues.csv"),
            "structured_json": str(target_dir / "structured.json"),
        }

        if state != "done" or not zip_url:
            meta["error"] = item.get("err_msg") or "unexpected state / missing zip url"
            processed.append(meta)
            print(f"[WARN] {file_name} -> {meta['error']}", file=sys.stderr)
            continue

        if cfg.skip_existing and md_path.exists() and (target_dir / "structured.json").exists():
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
        md_path.write_text(md_text, encoding="utf-8")

        if cfg.extract_structured:
            structured = parse_markdown_structure(
                md_text,
                source_meta={
                    "source_file": file_name,
                    "source_path": str(file_path),
                    "relative_path": rel_path.as_posix(),
                    "batch_id": batch_id,
                },
            )

            roles = structured["roles"]
            scenes = structured["scenes"]
            dialogues = structured["dialogues"]

            write_csv(
                target_dir / "roles.csv",
                roles,
                ["role_name", "role_type", "role_note", "source_line", "raw"],
            )
            write_csv(
                target_dir / "scenes.csv",
                scenes,
                ["scene_index", "scene_title", "start_line", "end_line"],
            )
            write_csv(
                target_dir / "dialogues.csv",
                dialogues,
                [
                    "source_file", "source_path", "relative_path", "batch_id",
                    "scene_index", "scene_title", "line_no",
                    "row_type", "speaker", "cue", "text", "raw_line"
                ],
            )
            write_json(target_dir / "structured.json", structured)

            meta["play_title"] = structured["metadata"].get("play_title", "")
            meta["roles_count"] = len(roles)
            meta["scene_count"] = len(scenes)
            meta["dialogue_count"] = len(dialogues)

        (target_dir / "mineru_result.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        processed.append(meta)
        print(f"[OK] {file_name} -> {md_path}")

    return processed


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Batch convert PDFs to Markdown using MinerU and structure the results.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input-dir", type=Path, help="Directory containing PDFs")
    src.add_argument("--files", nargs="*", type=Path, help="Explicit PDF files")

    p.add_argument("--output-dir", type=Path, required=True, help="Output directory")
    p.add_argument("--manifest", type=Path, help="Optional CSV manifest output path")
    p.add_argument("--combined-dialogue-csv", type=Path, help="Optional combined dialogues CSV")
    p.add_argument("--combined-dialogue-jsonl", type=Path, help="Optional combined dialogues JSONL")
    p.add_argument("--combined-roles-csv", type=Path, help="Optional combined roles CSV")
    p.add_argument("--combined-scenes-csv", type=Path, help="Optional combined scenes CSV")
    p.add_argument("--combined-structured-jsonl", type=Path, help="Optional combined structured JSONL")
    p.add_argument("--model", default=os.getenv("MINERU_MODEL", "vlm"), help="pipeline|vlm|MinerU-HTML")
    p.add_argument("--language", default=os.getenv("MINERU_LANG", "ch"), help="Language code")
    p.add_argument("--wait", type=int, default=int(os.getenv("MINERU_WAIT", "15")), help="Poll interval seconds")
    p.add_argument("--timeout-minutes", type=int, default=int(os.getenv("MINERU_TIMEOUT", "120")), help="Per-batch timeout")
    p.add_argument("--chunk-size", type=int, default=50, help="Local upload batch size (<=50 recommended)")
    p.add_argument("--no-recursive", action="store_true", help="Do not scan subfolders")
    p.add_argument("--clean-md", action="store_true", help="Apply conservative cleanup to full.md")
    p.add_argument("--no-skip-existing", action="store_true", help="Rebuild outputs even if markdown exists")
    p.add_argument("--use-env-proxy", action="store_true", help="Allow requests to use system proxy env vars")
    p.add_argument("--no-structured", action="store_true", help="Only output Markdown without structured tables")
    return p


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
        extract_structured=not args.no_structured,
    )

    all_records: list[dict] = []
    all_dialogues: list[dict] = []
    all_roles: list[dict] = []
    all_scenes: list[dict] = []
    all_structured: list[dict] = []

    with build_session(trust_env=cfg.trust_env) as session:
        for batch_files, batch_rel_paths in zip(chunked(files, cfg.chunk_size), chunked(rel_paths, cfg.chunk_size)):
            batch_records = convert_one_batch(session, cfg, batch_files, batch_rel_paths, out_root)
            all_records.extend(batch_records)

            # Load structured outputs just produced; if skipped, they may already exist.
            if cfg.extract_structured:
                for rec in batch_records:
                    if rec.get("error") or rec.get("skipped"):
                        continue
                    structured_path = Path(rec["structured_json"])
                    if not structured_path.exists():
                        continue
                    structured = json.loads(structured_path.read_text(encoding="utf-8"))
                    all_structured.append(structured)
                    all_roles.extend(structured.get("roles", []))
                    all_scenes.extend(structured.get("scenes", []))
                    all_dialogues.extend(structured.get("dialogues", []))

    if args.manifest:
        write_manifest(args.manifest.resolve(), all_records)
        print(f"[MANIFEST] saved -> {args.manifest.resolve()}")

    # Combined corpus-level outputs
    if args.combined_dialogue_csv and all_dialogues:
        write_csv(
            args.combined_dialogue_csv.resolve(),
            all_dialogues,
            [
                "source_file", "source_path", "relative_path", "batch_id",
                "scene_index", "scene_title", "line_no",
                "row_type", "speaker", "cue", "text", "raw_line"
            ],
        )
        print(f"[COMBINED] dialogues CSV -> {args.combined_dialogue_csv.resolve()}")

    if args.combined_dialogue_jsonl and all_dialogues:
        write_jsonl(args.combined_dialogue_jsonl.resolve(), all_dialogues)
        print(f"[COMBINED] dialogues JSONL -> {args.combined_dialogue_jsonl.resolve()}")

    if args.combined_roles_csv and all_roles:
        write_csv(
            args.combined_roles_csv.resolve(),
            all_roles,
            ["role_name", "role_type", "role_note", "source_line", "raw"],
        )
        print(f"[COMBINED] roles CSV -> {args.combined_roles_csv.resolve()}")

    if args.combined_scenes_csv and all_scenes:
        write_csv(
            args.combined_scenes_csv.resolve(),
            all_scenes,
            ["scene_index", "scene_title", "start_line", "end_line"],
        )
        print(f"[COMBINED] scenes CSV -> {args.combined_scenes_csv.resolve()}")

    if args.combined_structured_jsonl and all_structured:
        write_jsonl(args.combined_structured_jsonl.resolve(), all_structured)
        print(f"[COMBINED] structured JSONL -> {args.combined_structured_jsonl.resolve()}")

    return 0


# =========================
# Robust parsing overrides
# =========================

PARSER_VERSION = "2026-05-10-r2"
ROLE_SECTION_MARKERS = ("主要角色", "角色表", "剧中人")
ROLE_SECTION_END_MARKERS = ("情节", "注释")

# Scene markers like 【第一场】 / 第三场 / 第1场.
SCENE_BODY_RE = re.compile(r"第\s*[一二三四五六七八九十百零〇0-9]+\s*场")
SCENE_MARKER_LINE_RE = re.compile(r"^\s*【?\s*(第\s*[一二三四五六七八九十百零〇0-9]+\s*场)\s*】?\s*$")

# Speaker line: short speaker name followed by cue in parentheses or after colon.
SPEAKER_LINE_RE = re.compile(
    r"^\s*(?P<speaker>[\u4e00-\u9fffA-Za-z0-9·]{1,8})"
    r"(?:\s*(?:：|:)\s*|\s*[（(](?P<cue>[^）)]{1,30})[）)])"
    r"\s*(?P<text>.*)$"
)

# Parenthetical stage direction / cue line.
PAREN_ONLY_RE = re.compile(r"^\s*[（(](?P<cue>[^）)]{1,40})[）)]\s*$")
PAREN_FRONT_RE = re.compile(r"^\s*[（(](?P<cue>[^）)]{1,40})[）)]\s*(?P<text>.*)$")

# Common non-canonical stage speaker labels that may appear even if not in role table.
COMMON_STAGE_SPEAKERS = {
    "童儿", "旗牌", "报子", "龙套", "众人", "老军甲", "老军乙",
    "二老军", "二童儿", "四上手", "四龙套", "四白龙套", "四上手引赵云",
    "四上手引赵云急急风过场"
}


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\u3000", " ")).strip()


def clean_markdown_text(text: str) -> str:
    """Conservative cleanup for OCR / layout noise."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if re.fullmatch(r"中国京剧戏考\b.*", stripped):
            continue
        if re.fullmatch(r"https?://scripts\.xikao\.com/play/\d+", stripped):
            continue
        if re.fullmatch(r"Powered by TCPDF \(www\.tcpdf\.org\)", stripped):
            continue
        if re.fullmatch(r"\d+", stripped):
            # Standalone page numbers are noise for structure extraction.
            continue
        if stripped == "根据《戏考》第一册整理":
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + "\n"


def split_scene_marker(line: str) -> str | None:
    text = normalize_text(line)
    m = SCENE_MARKER_LINE_RE.match(text)
    if not m:
        return None
    return f"【{m.group(1).replace(' ', '')}】"


def is_role_section_marker(line: str) -> bool:
    text = normalize_text(line)
    return any(marker in text for marker in ROLE_SECTION_MARKERS)


def parse_role_section(lines: list[str]) -> tuple[list[dict], set[str]]:
    """
    Parse the role list between 主要角色 and 情节/第一场.
    Returns (roles, role_names).
    """
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
        # Typical form: 诸葛亮：老生
        m = re.match(r"^(?P<name>[^：:]{1,12})[：:](?P<role>.+)$", text)
        if not m:
            m = re.match(r"^(?P<name>[^：:]{1,12})[：:](?P<role>.+)$", text)
        if m:
            name = normalize_text(m.group("name"))
            role = normalize_text(m.group("role"))
            if name and role and len(name) <= 12:
                roles.append({
                    "role_name": name,
                    "role_type": role,
                    "role_note": "",
                    "source_line": line_no + 1,
                    "raw": raw,
                })
                continue

        # Fallback: role name and type separated by wide spaces.
        if re.search(r"\s{2,}", text):
            parts = [p for p in re.split(r"\s{2,}", text) if p]
            if len(parts) == 2 and len(parts[0]) <= 12:
                roles.append({
                    "role_name": parts[0].strip(),
                    "role_type": parts[1].strip(),
                    "role_note": "",
                    "source_line": line_no + 1,
                    "raw": raw,
                })

    return roles, {r["role_name"] for r in roles}


def classify_row_type(cue: str, text: str) -> str:
    cue_norm = normalize_text(cue)
    text_norm = normalize_text(text)
    if cue_norm.startswith("西皮") or cue_norm in {"快板", "导板", "原板", "慢板", "摇板", "散板", "二六板", "流水板"}:
        return "lyric"
    if cue_norm in {"白", "念", "同白", "内白", "道白", "白口", "旁白"}:
        return "dialogue"
    if cue_norm and not text_norm:
        return "stage_direction"
    if text_norm and not cue_norm:
        return "narration"
    return "dialogue"


def _append_to_previous(rows: list[dict], text: str, raw_line: str) -> bool:
    if not rows:
        return False
    prev = rows[-1]
    if prev.get("row_type") not in {"dialogue", "lyric", "narration", "stage_direction"}:
        return False
    if prev.get("scene_index") is None:
        return False
    if not text:
        return False
    prev_text = prev.get("text", "")
    prev["text"] = f"{prev_text} {text}".strip() if prev_text else text
    prev["raw_line"] = f"{prev.get('raw_line', '')}\n{raw_line}".strip()
    return True


def parse_scenes_and_dialogues(lines: list[str], source_meta: dict, role_names: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    scenes: list[dict] = []
    dialogues: list[dict] = []
    role_names = role_names or set()

    current_scene: dict | None = None
    scene_index = -1
    last_row: dict | None = None
    in_role_section = False
    role_section_closed = False

    def open_scene(title: str, line_no: int) -> None:
        nonlocal current_scene, scene_index, last_row
        if current_scene is not None:
            current_scene["end_line"] = line_no - 1
            scenes.append(current_scene)
        scene_index += 1
        current_scene = {
            "scene_index": scene_index,
            "scene": title,
            "line_no": line_no,
            "start_line": line_no,
            "end_line": 0,
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
            }

    # Detect role section boundaries to avoid misclassifying the role list as dialogue.
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

    for idx, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue

        # Skip noise that survives cleaning.
        if any(pat.match(stripped) for pat in NOISE_LINE_PATTERNS):
            continue

        # Update role section state.
        if role_start != -1 and (idx - 1) == role_start:
            in_role_section = True
            continue
        if in_role_section and role_end is not None and (idx - 1) >= role_end:
            in_role_section = False
            role_section_closed = True
        if in_role_section:
            continue

        # Scene title.
        scene_marker = split_scene_marker(stripped)
        if scene_marker:
            open_scene(scene_marker, idx)
            continue

        if stripped in {"主要角色", "情节", "注释"}:
            continue

        # Stage direction-only line, e.g. （四上手引赵云急急风过场，同下。）
        m_paren_only = PAREN_ONLY_RE.match(stripped)
        if m_paren_only:
            ensure_preface(idx)
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
            last_row = row
            current_scene["end_line"] = idx
            continue

        # Speaker + cue lines.
        m = SPEAKER_LINE_RE.match(stripped)
        if m:
            speaker = normalize_text(m.group("speaker"))
            cue = normalize_text(m.group("cue") or "")
            text = normalize_text(m.group("text") or "")

            # Validity filter: avoid mistaking narrative prose for a speaker line.
            if speaker and (
                speaker in role_names
                or speaker in COMMON_STAGE_SPEAKERS
                or len(speaker) <= 4
            ):
                ensure_preface(idx)
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
                last_row = row
                current_scene["end_line"] = idx
                continue

        # Cue-front line without speaker, e.g. （西皮摇板）我用兵数十年从来谨慎，
        m_front = PAREN_FRONT_RE.match(stripped)
        if m_front and len(stripped) <= 120:
            cue = normalize_text(m_front.group("cue"))
            text = normalize_text(m_front.group("text") or "")
            ensure_preface(idx)
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
            last_row = row
            current_scene["end_line"] = idx
            continue

        # Wrapped continuation: merge with previous row if it is clearly a layout wrap.
        if raw.startswith((" ", "\u3000", "\t")) and last_row is not None:
            if _append_to_previous([last_row], normalize_text(stripped), raw):
                current_scene["end_line"] = idx
                continue

        # Fallback: treat as narration/metadata line.
        ensure_preface(idx)
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
        last_row = row
        current_scene["end_line"] = idx

    if current_scene is not None:
        if current_scene["end_line"] == 0:
            current_scene["end_line"] = len(lines)
        scenes.append(current_scene)

    return scenes, dialogues


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


def parse_markdown_structure(md_text: str, source_meta: dict) -> dict:
    lines = md_text.splitlines()
    play_title = extract_title(lines)
    roles, role_names = parse_role_section(lines)
    scenes, dialogues = parse_scenes_and_dialogues(lines, source_meta, role_names=role_names)
    return {
        "metadata": {
            **source_meta,
            "parser_version": PARSER_VERSION,
            "play_title": play_title,
            "line_count": len(lines),
            "role_count": len(roles),
            "scene_count": len(scenes),
            "dialogue_count": len(dialogues),
        },
        "roles": roles,
        "scenes": scenes,
        "dialogues": dialogues,
    }


def _csv_row_count(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with path.open("r", encoding="utf-8", newline="") as f:
        return sum(1 for _ in f) - 1


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
        if _csv_row_count(scenes_csv) <= 0:
            return False
        if _csv_row_count(dialogues_csv) <= 0:
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

        meta = {
            "source_file": file_name,
            "source_path": str(file_path),
            "relative_path": rel_path.as_posix(),
            "batch_id": batch_id,
            "state": state,
            "full_zip_url": zip_url,
            "target_dir": str(target_dir),
            "md_path": str(md_path),
            "roles_csv": str(target_dir / "roles.csv"),
            "scenes_csv": str(target_dir / "scenes.csv"),
            "dialogues_csv": str(target_dir / "dialogues.csv"),
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
        md_path.write_text(md_text, encoding="utf-8")

        if cfg.extract_structured:
            structured = parse_markdown_structure(
                md_text,
                source_meta={
                    "source_file": file_name,
                    "source_path": str(file_path),
                    "relative_path": rel_path.as_posix(),
                    "batch_id": batch_id,
                },
            )

            roles = structured["roles"]
            scenes = structured["scenes"]
            dialogues = structured["dialogues"]

            write_csv(
                target_dir / "roles.csv",
                roles,
                ["role_name", "role_type", "role_note", "source_line", "raw"],
            )
            write_csv(
                target_dir / "scenes.csv",
                scenes,
                ["scene_index", "scene", "line_no", "start_line", "end_line"],
            )
            write_csv(
                target_dir / "dialogues.csv",
                dialogues,
                [
                    "source_file", "source_path", "relative_path", "batch_id",
                    "scene_index", "scene", "line_no",
                    "row_type", "speaker", "cue", "text", "raw_line",
                ],
            )
            write_json(target_dir / "structured.json", structured)

            meta["play_title"] = structured["metadata"].get("play_title", "")
            meta["roles_count"] = len(roles)
            meta["scene_count"] = len(scenes)
            meta["dialogue_count"] = len(dialogues)
            meta["parser_version"] = structured["metadata"].get("parser_version", PARSER_VERSION)

        (target_dir / "mineru_result.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        processed.append(meta)
        print(f"[OK] {file_name} -> {md_path}")

    return processed


def write_manifest(manifest_path: Path, records: list[dict]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "source_file",
        "source_path",
        "relative_path",
        "play_title",
        "parser_version",
        "batch_id",
        "state",
        "full_zip_url",
        "target_dir",
        "md_path",
        "roles_csv",
        "scenes_csv",
        "dialogues_csv",
        "structured_json",
        "skipped",
        "error",
        "roles_count",
        "scene_count",
        "dialogue_count",
    ]
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            writer.writerow({k: rec.get(k, "") for k in fieldnames})


if __name__ == "__main__":
    raise SystemExit(main())
