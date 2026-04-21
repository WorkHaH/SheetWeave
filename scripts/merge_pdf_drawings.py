#!/usr/bin/env python3
"""
Render drawing pages from a PDF, group overlapping pages, and stitch each group.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image

if __package__:
    from .merge_drawings import (
        MatchCandidate,
        PreparedImage,
        VALID_DIRECTIONS,
        ensure_dir,
        evaluate_direction,
        merge_from_candidate,
        prepare_image,
        write_json,
    )
else:  # pragma: no cover - keeps direct script execution convenient.
    from merge_drawings import (
        MatchCandidate,
        PreparedImage,
        VALID_DIRECTIONS,
        ensure_dir,
        evaluate_direction,
        merge_from_candidate,
        prepare_image,
        write_json,
    )

SHEET_CODE_RE = re.compile(r"([A-Z]{2,5}-[A-Z0-9]{2,8}-\d{2})")


@dataclass
class PageRecord:
    index: int
    image_path: Path
    cropped_path: Path
    prepared: PreparedImage
    thumbnail: np.ndarray
    thumb_gray: np.ndarray
    thumb_line: np.ndarray


@dataclass
class TextWord:
    text: str
    left: float
    top: float
    width: float
    height: float
    conf: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    @property
    def center_x(self) -> float:
        return self.left + self.width / 2.0

    @property
    def center_y(self) -> float:
        return self.top + self.height / 2.0


@dataclass
class OverviewPlacement:
    code: str
    row: int
    col: int
    center_x: float
    center_y: float
    bbox: Tuple[float, float, float, float]


@dataclass
class OverviewLayout:
    kind: str
    page_number: int
    page_width: float
    page_height: float
    placements: Dict[str, OverviewPlacement]


@dataclass
class PageSheetMatch:
    page_number: int
    code: str
    score: float
    source_text: str


@dataclass
class GuidedEdge:
    page_a: int
    page_b: int
    code_a: str
    code_b: str
    direction: str
    candidate: MatchCandidate


def parse_manual_layout(
    layout_json_path: Path,
) -> Tuple[OverviewLayout, List[PageSheetMatch]]:
    data = json.loads(layout_json_path.read_text(encoding="utf-8"))
    overview_page = int(data["overview_page"])
    kind = str(data.get("kind", "manual_vlm"))
    page_width = float(data.get("page_width", 0.0))
    page_height = float(data.get("page_height", 0.0))

    raw_placements = data.get("placements", [])
    placements: Dict[str, OverviewPlacement] = {}
    if isinstance(raw_placements, dict):
        iterable = [{"code": code, **value} for code, value in raw_placements.items()]
    else:
        iterable = raw_placements
    for item in iterable:
        code = str(item["code"])
        placements[code] = OverviewPlacement(
            code=code,
            row=int(item["row"]),
            col=int(item["col"]),
            center_x=float(item.get("center_x", 0.0)),
            center_y=float(item.get("center_y", 0.0)),
            bbox=tuple(item.get("bbox", (0.0, 0.0, 0.0, 0.0))),
        )

    page_matches = [
        PageSheetMatch(
            page_number=int(item["page_number"]),
            code=str(item["code"]),
            score=float(item.get("score", 1.0)),
            source_text=str(item.get("source_text", "manual_vlm")),
        )
        for item in data.get("page_matches", [])
    ]
    return (
        OverviewLayout(
            kind=kind,
            page_number=overview_page,
            page_width=page_width,
            page_height=page_height,
            placements=placements,
        ),
        page_matches,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a drawing PDF, group overlapping pages, and stitch groups."
    )
    parser.add_argument("--pdf", help="Path to the source PDF.")
    parser.add_argument(
        "--overview-layout-json",
        help="Optional JSON file with overview layout and page mapping produced by a human or VLM.",
    )
    parser.add_argument(
        "--rendered-dir",
        help="Use an existing directory of rendered page PNGs instead of rendering from PDF.",
    )
    parser.add_argument("--out", required=True, help="Output directory.")
    parser.add_argument(
        "--dpi",
        type=int,
        default=110,
        help="Render DPI for PDF pages.",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=4,
        help="Only compare each page with the next N pages when grouping.",
    )
    parser.add_argument(
        "--title-block-ratio",
        type=float,
        default=0.13,
        help="Fraction of right-side width to drop before overlap matching.",
    )
    parser.add_argument(
        "--group-threshold",
        type=float,
        default=0.58,
        help="Confidence threshold for linking pages into the same group.",
    )
    parser.add_argument(
        "--min-structural-score",
        type=float,
        default=0.18,
        help="Minimum structural score for page grouping.",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "review"),
        default="review",
        help="Review mode keeps more debug artifacts.",
    )
    return parser.parse_args()


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required executable not found in PATH: {name}")
    return path


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def extract_sheet_code(text: str) -> str | None:
    match = SHEET_CODE_RE.search(text.upper())
    if match is None:
        return None
    return match.group(1)


def extract_tsv_words(
    pdf_path: Path,
    page_number: int,
    cache: Dict[int, Tuple[List[TextWord], float, float]],
) -> Tuple[List[TextWord], float, float]:
    if page_number in cache:
        return cache[page_number]

    command = [
        require_binary("pdftotext"),
        "-f",
        str(page_number),
        "-l",
        str(page_number),
        "-tsv",
        str(pdf_path),
        "-",
    ]
    raw = run_command(command).stdout
    reader = csv.DictReader(io.StringIO(raw), delimiter="\t")
    words: List[TextWord] = []
    page_width = 0.0
    page_height = 0.0
    for row in reader:
        text = row.get("text", "").strip()
        try:
            left = float(row.get("left", "0") or 0.0)
            top = float(row.get("top", "0") or 0.0)
            width = float(row.get("width", "0") or 0.0)
            height = float(row.get("height", "0") or 0.0)
            conf = float(row.get("conf", "0") or 0.0)
        except ValueError:
            continue
        if text == "###PAGE###":
            page_width = width
            page_height = height
            continue
        if not text or text.startswith("###"):
            continue
        words.append(
            TextWord(
                text=text,
                left=left,
                top=top,
                width=width,
                height=height,
                conf=conf,
            )
        )
    cache[page_number] = (words, page_width, page_height)
    return cache[page_number]


def parse_page_count(pdfinfo_output: str) -> int:
    for line in pdfinfo_output.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise RuntimeError("Could not parse page count from pdfinfo output.")


def render_pdf_pages(pdf_path: Path, render_dir: Path, dpi: int) -> List[Path]:
    prefix = render_dir / "page"
    pdftoppm = require_binary("pdftoppm")
    command = [
        pdftoppm,
        "-png",
        "-r",
        str(dpi),
        str(pdf_path),
        str(prefix),
    ]
    run_command(command)
    return sorted(render_dir.glob("page-*.png"))


def crop_focus_region(color: np.ndarray, title_block_ratio: float) -> np.ndarray:
    h, w = color.shape[:2]
    right_crop = int(w * title_block_ratio)
    usable_w = max(200, w - right_crop)
    cropped = color[:, :usable_w]
    top_margin = int(h * 0.02)
    bottom_margin = int(h * 0.02)
    left_margin = int(cropped.shape[1] * 0.01)
    return cropped[top_margin:h - bottom_margin, left_margin:cropped.shape[1]]


def make_thumbnail(image: np.ndarray, max_size: Tuple[int, int] = (360, 260)) -> np.ndarray:
    h, w = image.shape[:2]
    scale = min(max_size[0] / max(1, w), max_size[1] / max(1, h))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)


def build_page_records(
    image_paths: Sequence[Path],
    work_dir: Path,
    title_block_ratio: float,
) -> List[PageRecord]:
    cropped_dir = work_dir / "cropped"
    ensure_dir(cropped_dir)
    records: List[PageRecord] = []
    for idx, image_path in enumerate(image_paths, start=1):
        color = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if color is None:
            raise FileNotFoundError(f"Could not read rendered page: {image_path}")
        focused = crop_focus_region(color, title_block_ratio=title_block_ratio)
        cropped_path = cropped_dir / f"page-{idx:02d}-cropped.png"
        cv2.imwrite(str(cropped_path), focused)
        prepared = prepare_image(focused, path=cropped_path)
        thumbnail = make_thumbnail(focused)
        thumb_gray = cv2.cvtColor(thumbnail, cv2.COLOR_BGR2GRAY)
        thumb_line = make_thumbnail(
            cv2.cvtColor(prepared.line_map, cv2.COLOR_GRAY2BGR),
            max_size=(360, 260),
        )
        thumb_line = cv2.cvtColor(thumb_line, cv2.COLOR_BGR2GRAY)
        records.append(
            PageRecord(
                index=idx,
                image_path=image_path,
                cropped_path=cropped_path,
                prepared=prepared,
                thumbnail=thumbnail,
                thumb_gray=thumb_gray,
                thumb_line=thumb_line,
            )
        )
    return records


def cluster_axis(values: Sequence[float], threshold: float) -> List[float]:
    if not values:
        return []
    ordered = sorted(values)
    clusters: List[List[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        if abs(value - clusters[-1][-1]) <= threshold:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def nearest_band_index(value: float, centers: Sequence[float]) -> int:
    distances = [abs(value - center) for center in centers]
    return int(min(range(len(distances)), key=distances.__getitem__))


def build_layout_from_words(
    page_number: int,
    page_width: float,
    page_height: float,
    label_words: Dict[str, TextWord],
    *,
    kind: str,
) -> OverviewLayout | None:
    if len(label_words) < 4:
        return None
    code_words = list(label_words.values())
    x_threshold = max(page_width * 0.07, float(np.median([word.width for word in code_words])) * 0.55)
    y_threshold = max(page_height * 0.05, float(np.median([word.height for word in code_words])) * 1.6)
    col_centers = cluster_axis([word.center_x for word in code_words], threshold=x_threshold)
    row_centers = cluster_axis([word.center_y for word in code_words], threshold=y_threshold)
    placements: Dict[str, OverviewPlacement] = {}
    for code, word in label_words.items():
        placements[code] = OverviewPlacement(
            code=code,
            row=nearest_band_index(word.center_y, row_centers),
            col=nearest_band_index(word.center_x, col_centers),
            center_x=word.center_x,
            center_y=word.center_y,
            bbox=(word.left, word.top, word.right, word.bottom),
        )
    return OverviewLayout(
        kind=kind,
        page_number=page_number,
        page_width=page_width,
        page_height=page_height,
        placements=placements,
    )


def extract_numeric_overview_labels(
    words: Sequence[TextWord],
    page_width: float,
    page_height: float,
    page_count: int,
) -> Dict[str, TextWord]:
    label_words: Dict[str, TextWord] = {}
    min_width = page_width * 0.010
    min_height = page_height * 0.018
    max_value = max(1, page_count - 1)
    for word in words:
        text = word.text.strip()
        if not text.isdigit():
            continue
        value = int(text)
        if value < 1 or value > max_value:
            continue
        if word.width < min_width or word.height < min_height:
            continue
        code = f"{value:02d}"
        prev = label_words.get(code)
        if prev is None or (word.width * word.height) > (prev.width * prev.height):
            label_words[code] = word
    return label_words


def detect_overview_layout(
    pdf_path: Path,
    page_count: int,
    cache: Dict[int, Tuple[List[TextWord], float, float]],
) -> OverviewLayout | None:
    candidate_pages = [1]
    if page_count > 1:
        candidate_pages.append(page_count)

    best_layout: OverviewLayout | None = None
    for page_number in candidate_pages:
        words, page_width, page_height = extract_tsv_words(pdf_path, page_number, cache)
        if page_width <= 0 or page_height <= 0:
            continue
        code_label_words: Dict[str, TextWord] = {}
        min_width = page_width * 0.08
        min_height = page_height * 0.018
        for word in words:
            code = extract_sheet_code(word.text)
            if code is None:
                continue
            if word.width < min_width or word.height < min_height:
                continue
            prev = code_label_words.get(code)
            if prev is None or (word.width * word.height) > (prev.width * prev.height):
                code_label_words[code] = word

        numeric_label_words = extract_numeric_overview_labels(words, page_width, page_height, page_count)
        layout = None
        if len(code_label_words) >= 4:
            layout = build_layout_from_words(
                page_number,
                page_width,
                page_height,
                code_label_words,
                kind="sheet_code",
            )
        if layout is None and len(numeric_label_words) >= 8:
            layout = build_layout_from_words(
                page_number,
                page_width,
                page_height,
                numeric_label_words,
                kind="numeric_index",
            )
        if layout is None:
            continue
        if best_layout is None or len(layout.placements) > len(best_layout.placements):
            best_layout = layout
    return best_layout


def detect_sheet_code_for_page(
    pdf_path: Path,
    page_number: int,
    cache: Dict[int, Tuple[List[TextWord], float, float]],
) -> PageSheetMatch | None:
    words, page_width, page_height = extract_tsv_words(pdf_path, page_number, cache)
    if page_width <= 0 or page_height <= 0:
        return None
    candidates: List[PageSheetMatch] = []
    for word in words:
        code = extract_sheet_code(word.text)
        if code is None:
            continue
        right_ratio = word.right / page_width
        bottom_ratio = word.bottom / page_height
        horizontal_bias = 1.0 if word.width >= word.height else -0.7
        title_region = 1.0 if right_ratio >= 0.84 else 0.0
        size_score = min(1.0, (word.width / page_width) / 0.08) + min(
            1.0,
            (word.height / page_height) / 0.02,
        )
        score = (
            2.8 * right_ratio
            + 1.4 * bottom_ratio
            + 1.8 * title_region
            + 0.8 * (1.0 if bottom_ratio >= 0.82 else 0.0)
            + 0.9 * size_score
            + 0.4 * (1.0 if "/" in word.text else 0.0)
            + horizontal_bias
        )
        candidates.append(
            PageSheetMatch(
                page_number=page_number,
                code=code,
                score=float(score),
                source_text=word.text,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[0]


def direction_from_layout(a: OverviewPlacement, b: OverviewPlacement) -> str | None:
    if a.row == b.row and a.col < b.col:
        return "right_of"
    if a.row == b.row and a.col > b.col:
        return "left_of"
    if a.col == b.col and a.row < b.row:
        return "below"
    if a.col == b.col and a.row > b.row:
        return "above"
    return None


def build_guided_neighbor_pairs(
    layout: OverviewLayout,
    page_lookup: Dict[str, int],
) -> List[Tuple[int, int, str, str, str]]:
    placements = {code: layout.placements[code] for code in page_lookup if code in layout.placements}
    pairs: Dict[Tuple[int, int], Tuple[int, int, str, str, str]] = {}
    for code_a, placement_a in placements.items():
        row_candidates = [
            placement_b
            for code_b, placement_b in placements.items()
            if code_b != code_a and placement_b.row == placement_a.row
        ]
        col_candidates = [
            placement_b
            for code_b, placement_b in placements.items()
            if code_b != code_a and placement_b.col == placement_a.col
        ]
        for candidates, axis in ((row_candidates, "row"), (col_candidates, "col")):
            for direction_sign in (-1, 1):
                best: OverviewPlacement | None = None
                best_distance = None
                for placement_b in candidates:
                    diff = (
                        placement_b.col - placement_a.col
                        if axis == "row"
                        else placement_b.row - placement_a.row
                    )
                    if diff == 0 or (diff > 0) != (direction_sign > 0):
                        continue
                    distance = abs(diff)
                    if best is None or distance < best_distance:
                        best = placement_b
                        best_distance = distance
                if best is None:
                    continue
                direction = direction_from_layout(placement_a, best)
                if direction is None:
                    continue
                page_a = page_lookup[code_a]
                page_b = page_lookup[best.code]
                pair_key = tuple(sorted((page_a, page_b)))
                pairs.setdefault(pair_key, (page_a, page_b, code_a, best.code, direction))
    return list(pairs.values())


def derive_page_matches_from_numeric_index(
    layout: OverviewLayout,
    page_count: int,
) -> List[PageSheetMatch]:
    detail_pages = [page for page in range(1, page_count + 1) if page != layout.page_number]
    expected_codes = sorted(layout.placements)
    max_numeric = max((int(code) for code in expected_codes), default=0)
    page_matches: List[PageSheetMatch] = []
    if layout.page_number == page_count and max_numeric == page_count - 1:
        for page_number in detail_pages:
            code = f"{page_number:02d}"
            if code in layout.placements:
                page_matches.append(
                    PageSheetMatch(
                        page_number=page_number,
                        code=code,
                        score=1.0,
                        source_text="numeric_index_page_number",
                    )
                )
    elif layout.page_number == 1 and max_numeric == page_count - 1:
        for page_number in detail_pages:
            code = f"{page_number - 1:02d}"
            if code in layout.placements:
                page_matches.append(
                    PageSheetMatch(
                        page_number=page_number,
                        code=code,
                        score=1.0,
                        source_text="numeric_index_page_number",
                    )
                )
    return page_matches


def normalized_correlation(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float32).ravel()
    b = b.astype(np.float32).ravel()
    if a.size == 0 or b.size == 0:
        return 0.0
    a -= a.mean()
    b -= b.mean()
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-6:
        return 0.0
    return max(0.0, float(np.dot(a, b) / denom))


def thumbnail_candidate_regions(
    image: np.ndarray,
    band_ratio: float = 0.34,
) -> Dict[str, np.ndarray]:
    h, w = image.shape[:2]
    band_w = max(24, int(w * band_ratio))
    band_h = max(24, int(h * band_ratio))
    return {
        "right_of": image[:, max(0, w - band_w):w],
        "left_of": image[:, 0:band_w],
        "below": image[max(0, h - band_h):h, :],
        "above": image[0:band_h, :],
    }


def resize_to_match(a: np.ndarray, b: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    if a.shape == b.shape:
        return a, b
    target_h = min(a.shape[0], b.shape[0])
    target_w = min(a.shape[1], b.shape[1])
    resized_a = cv2.resize(a, (target_w, target_h), interpolation=cv2.INTER_AREA)
    resized_b = cv2.resize(b, (target_w, target_h), interpolation=cv2.INTER_AREA)
    return resized_a, resized_b


def quick_direction_scores(page_a: PageRecord, page_b: PageRecord) -> List[Tuple[str, float]]:
    regions_a = thumbnail_candidate_regions(page_a.thumb_line)
    regions_b = thumbnail_candidate_regions(page_b.thumb_line)
    scored: List[Tuple[str, float]] = []
    opposite = {
        "right_of": "left_of",
        "left_of": "right_of",
        "below": "above",
        "above": "below",
    }
    for direction in VALID_DIRECTIONS:
        band_a, band_b = resize_to_match(
            regions_a[direction],
            regions_b[opposite[direction]],
        )
        if band_a.size == 0 or band_b.size == 0:
            continue
        row_score = normalized_correlation(band_a.sum(axis=1), band_b.sum(axis=1))
        col_score = normalized_correlation(band_a.sum(axis=0), band_b.sum(axis=0))
        density_a = float(np.count_nonzero(band_a)) / max(1, band_a.size)
        density_b = float(np.count_nonzero(band_b)) / max(1, band_b.size)
        density_score = 1.0 - abs(density_a - density_b)
        if direction in ("right_of", "left_of"):
            score = 0.65 * row_score + 0.2 * col_score + 0.15 * density_score
        else:
            score = 0.65 * col_score + 0.2 * row_score + 0.15 * density_score
        scored.append((direction, float(score)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def compare_pages(
    page_a: PageRecord,
    page_b: PageRecord,
    *,
    max_features: int = 1400,
    band_ratio: float = 0.38,
    refine_translation: bool = False,
    max_directions: int = 2,
    allowed_directions: Sequence[str] | None = None,
) -> MatchCandidate | None:
    quick_scores = quick_direction_scores(page_a, page_b)
    if allowed_directions is not None:
        allowed = set(allowed_directions)
        quick_scores = [(direction, score) for direction, score in quick_scores if direction in allowed]
    candidate_directions = [direction for direction, score in quick_scores[:max_directions] if score >= 0.12]
    if not candidate_directions:
        candidate_directions = [direction for direction, _ in quick_scores[:1]]
    if not candidate_directions:
        if allowed_directions is not None:
            candidate_directions = list(allowed_directions)
        else:
            candidate_directions = list(VALID_DIRECTIONS)
    candidates: List[MatchCandidate] = []
    for direction in candidate_directions:
        candidate = evaluate_direction(
            page_a.prepared,
            page_b.prepared,
            direction=direction,
            max_features=max_features,
            band_ratio=band_ratio,
            refine_translation=refine_translation,
        )
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[0]


def build_overlap_edges(
    records: Sequence[PageRecord],
    *,
    window_size: int,
    group_threshold: float,
    min_structural_score: float,
) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    for idx, page_a in enumerate(records):
        for page_b in records[idx + 1 : idx + 1 + window_size]:
            candidate = compare_pages(page_a, page_b)
            if candidate is None:
                continue
            edge = {
                "page_a": page_a.index,
                "page_b": page_b.index,
                "direction": candidate.direction,
                "confidence": candidate.confidence,
                "score": candidate.score,
                "structural_score": candidate.structural_score,
                "match_count": candidate.match_count,
                "inlier_count": candidate.inlier_count,
                "residual_error": candidate.residual_error,
                "overlap_ratio": candidate.overlap_ratio,
                "accepted": (
                    candidate.confidence >= group_threshold
                    and candidate.structural_score >= min_structural_score
                ),
            }
            edges.append(edge)
    return edges


def build_groups(page_count: int, edges: Sequence[Dict[str, Any]]) -> List[List[int]]:
    parent = list(range(page_count + 1))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra = find(a)
        rb = find(b)
        if ra != rb:
            parent[rb] = ra

    for edge in edges:
        if edge["accepted"]:
            union(int(edge["page_a"]), int(edge["page_b"]))

    groups: Dict[int, List[int]] = {}
    for page in range(1, page_count + 1):
        root = find(page)
        groups.setdefault(root, []).append(page)
    return [sorted(group) for group in groups.values()]


def create_contact_sheet(
    records: Sequence[PageRecord],
    page_numbers: Sequence[int],
    out_path: Path,
) -> None:
    if not page_numbers:
        return
    selected = [records[number - 1] for number in page_numbers]
    cols = min(3, len(selected))
    rows = int(math.ceil(len(selected) / cols))
    tile_w = 380
    tile_h = 300
    canvas = np.full((rows * tile_h, cols * tile_w, 3), 255, dtype=np.uint8)

    for idx, record in enumerate(selected):
        row = idx // cols
        col = idx % cols
        x0 = col * tile_w
        y0 = row * tile_h
        thumb = record.thumbnail
        th, tw = thumb.shape[:2]
        x = x0 + (tile_w - tw) // 2
        y = y0 + 10
        canvas[y : y + th, x : x + tw] = thumb
        cv2.putText(
            canvas,
            f"Page {record.index:02d}",
            (x0 + 12, y0 + tile_h - 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (20, 20, 20),
            2,
            cv2.LINE_AA,
        )
    cv2.imwrite(str(out_path), canvas)


def create_layout_contact_sheet(
    records: Sequence[PageRecord],
    ordered_pages: Sequence[int],
    page_to_code: Dict[int, str],
    out_path: Path,
) -> None:
    if not ordered_pages:
        return
    cols = min(3, len(ordered_pages))
    rows = int(math.ceil(len(ordered_pages) / cols))
    tile_w = 420
    tile_h = 320
    canvas = np.full((rows * tile_h, cols * tile_w, 3), 255, dtype=np.uint8)
    for idx, page_number in enumerate(ordered_pages):
        row = idx // cols
        col = idx % cols
        x0 = col * tile_w
        y0 = row * tile_h
        thumb = records[page_number - 1].thumbnail
        th, tw = thumb.shape[:2]
        x = x0 + (tile_w - tw) // 2
        y = y0 + 10
        canvas[y : y + th, x : x + tw] = thumb
        label = f"P{page_number:02d}  {page_to_code[page_number]}"
        cv2.putText(
            canvas,
            label,
            (x0 + 12, y0 + tile_h - 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.60,
            (20, 20, 20),
            2,
            cv2.LINE_AA,
        )
    cv2.imwrite(str(out_path), canvas)


def create_final_group_contact(
    group_results: Sequence[Dict[str, Any]],
    out_path: Path,
) -> None:
    if not group_results:
        return
    thumbs: List[Tuple[np.ndarray, str]] = []
    for result in group_results:
        merged_path = Path(result["merged_image"])
        image = cv2.imread(str(merged_path), cv2.IMREAD_COLOR)
        if image is None:
            continue
        thumb = make_thumbnail(image, max_size=(520, 260))
        label = f"G{result['group_id']:02d}: " + ",".join(str(p) for p in result["pages"])
        thumbs.append((thumb, label))
    if not thumbs:
        return
    cols = min(2, len(thumbs))
    rows = int(math.ceil(len(thumbs) / cols))
    tile_w = 560
    tile_h = 320
    canvas = np.full((rows * tile_h, cols * tile_w, 3), 255, dtype=np.uint8)
    for idx, (thumb, label) in enumerate(thumbs):
        row = idx // cols
        col = idx % cols
        x0 = col * tile_w
        y0 = row * tile_h
        th, tw = thumb.shape[:2]
        x = x0 + (tile_w - tw) // 2
        y = y0 + 15
        canvas[y : y + th, x : x + tw] = thumb
        cv2.putText(
            canvas,
            label,
            (x0 + 14, y0 + tile_h - 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (20, 20, 20),
            2,
            cv2.LINE_AA,
        )
    cv2.imwrite(str(out_path), canvas)


def write_groups_pdf(group_results: Sequence[Dict[str, Any]], out_path: Path) -> None:
    pages: List[Image.Image] = []
    for result in group_results:
        merged_path = Path(result["merged_image"])
        with Image.open(merged_path) as image:
            rgb = image.convert("RGB")
            pages.append(rgb.copy())
    if not pages:
        return
    first, rest = pages[0], pages[1:]
    first.save(str(out_path), save_all=True, append_images=rest)


def write_single_image_pdf(image_path: Path, out_path: Path) -> None:
    with Image.open(image_path) as image:
        image.convert("RGB").save(str(out_path))


def augment_affine(matrix: np.ndarray) -> np.ndarray:
    return np.vstack([matrix, [0.0, 0.0, 1.0]])


def render_group_canvas(
    records: Sequence[PageRecord],
    placements: Sequence[Tuple[int, np.ndarray]],
    out_path: Path,
) -> None:
    corners: List[np.ndarray] = []
    for page_number, transform in placements:
        record = records[page_number - 1]
        h, w = record.prepared.color.shape[:2]
        pts = np.array([[0, 0], [w, 0], [0, h], [w, h]], dtype=np.float32).reshape(-1, 1, 2)
        warped = cv2.perspectiveTransform(pts, transform).reshape(-1, 2)
        corners.append(warped)
    all_points = np.vstack(corners)
    min_x = int(np.floor(all_points[:, 0].min()))
    min_y = int(np.floor(all_points[:, 1].min()))
    max_x = int(np.ceil(all_points[:, 0].max()))
    max_y = int(np.ceil(all_points[:, 1].max()))
    tx = -min(0, min_x)
    ty = -min(0, min_y)
    canvas_w = max_x + tx
    canvas_h = max_y + ty
    translate = np.array(
        [[1.0, 0.0, tx], [0.0, 1.0, ty], [0.0, 0.0, 1.0]],
        dtype=np.float32,
    )
    merged = np.full((canvas_h, canvas_w, 3), 255, dtype=np.uint8)
    coverage = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    for page_number, transform in placements:
        record = records[page_number - 1]
        shifted = translate @ transform
        warped = cv2.warpPerspective(
            record.prepared.color,
            shifted,
            (canvas_w, canvas_h),
            flags=cv2.INTER_LINEAR,
            borderValue=(255, 255, 255),
        )
        mask = cv2.warpPerspective(
            np.full(record.prepared.color.shape[:2], 255, dtype=np.uint8),
            shifted,
            (canvas_w, canvas_h),
            flags=cv2.INTER_NEAREST,
            borderValue=0,
        )
        only_new = (mask > 0) & (coverage == 0)
        overlap = (mask > 0) & (coverage > 0)
        merged[only_new] = warped[only_new]
        merged[overlap] = np.minimum(merged[overlap], warped[overlap])
        coverage = cv2.bitwise_or(coverage, mask)
    cv2.imwrite(str(out_path), merged)


def stitch_overview_guided_document(
    records: Sequence[PageRecord],
    layout: OverviewLayout,
    page_matches: Sequence[PageSheetMatch],
    out_dir: Path,
    mode: str,
    min_confidence: float,
) -> Dict[str, Any]:
    ensure_dir(out_dir)
    page_lookup = {match.code: match.page_number for match in page_matches if match.code in layout.placements}
    ordered_codes = sorted(
        page_lookup,
        key=lambda code: (
            layout.placements[code].row,
            layout.placements[code].col,
            code,
        ),
    )
    ordered_pages = [page_lookup[code] for code in ordered_codes]
    page_to_code = {page_lookup[code]: code for code in ordered_codes}

    shutil.copy2(records[layout.page_number - 1].image_path, out_dir / "overview-page.png")
    create_layout_contact_sheet(records, ordered_pages, page_to_code, out_dir / "layout-contact.png")

    guided_edges: List[GuidedEdge] = []
    for page_a, page_b, code_a, code_b, direction in build_guided_neighbor_pairs(layout, page_lookup):
        candidate = compare_pages(
            records[page_a - 1],
            records[page_b - 1],
            max_features=2600,
            band_ratio=0.42,
            refine_translation=True,
            max_directions=1,
            allowed_directions=[direction],
        )
        if candidate is None:
            continue
        guided_edges.append(
            GuidedEdge(
                page_a=page_a,
                page_b=page_b,
                code_a=code_a,
                code_b=code_b,
                direction=direction,
                candidate=candidate,
            )
        )

    parent = {page: page for page in ordered_pages}

    def find(page: int) -> int:
        while parent[page] != page:
            parent[page] = parent[parent[page]]
            page = parent[page]
        return page

    def union(page_a: int, page_b: int) -> bool:
        root_a = find(page_a)
        root_b = find(page_b)
        if root_a == root_b:
            return False
        parent[root_b] = root_a
        return True

    accepted_edges = [
        edge
        for edge in guided_edges
        if edge.candidate.confidence >= (min_confidence * 0.72)
        and edge.candidate.structural_score >= 0.12
    ]
    rejected_edges = [edge for edge in guided_edges if edge not in accepted_edges]
    accepted_edges.sort(key=lambda item: item.candidate.confidence, reverse=True)
    rejected_edges.sort(key=lambda item: item.candidate.score, reverse=True)

    selected_edges: List[GuidedEdge] = []
    target_edge_count = max(0, len(ordered_pages) - 1)
    for pool in (accepted_edges, rejected_edges):
        for edge in pool:
            if union(edge.page_a, edge.page_b):
                selected_edges.append(edge)
            if len(selected_edges) >= target_edge_count:
                break
        if len(selected_edges) >= target_edge_count:
            break

    if not ordered_pages:
        raise RuntimeError("Overview-guided stitching found no detail pages to merge.")

    anchor_page = ordered_pages[0]
    placements: Dict[int, np.ndarray] = {anchor_page: np.eye(3, dtype=np.float32)}
    queue: List[int] = [anchor_page]
    adjacency: Dict[int, List[Tuple[int, np.ndarray, GuidedEdge]]] = {}
    for edge in selected_edges:
        matrix_ab = augment_affine(edge.candidate.matrix)
        matrix_ba = np.linalg.inv(matrix_ab)
        adjacency.setdefault(edge.page_a, []).append((edge.page_b, matrix_ab, edge))
        adjacency.setdefault(edge.page_b, []).append((edge.page_a, matrix_ba, edge))

    while queue:
        current_page = queue.pop(0)
        current_transform = placements[current_page]
        for next_page, local_matrix, _ in adjacency.get(current_page, []):
            if next_page in placements:
                continue
            placements[next_page] = current_transform @ local_matrix
            queue.append(next_page)

    placed_pages = sorted(placements)
    render_group_canvas(
        records,
        [(page_number, placements[page_number]) for page_number in placed_pages],
        out_dir / "full-merged.png",
    )
    write_single_image_pdf(out_dir / "full-merged.png", out_dir / "full-merged.pdf")

    edge_payload = [
        {
            "page_a": edge.page_a,
            "page_b": edge.page_b,
            "code_a": edge.code_a,
            "code_b": edge.code_b,
            "expected_direction": edge.direction,
            "confidence": edge.candidate.confidence,
            "structural_score": edge.candidate.structural_score,
            "match_count": edge.candidate.match_count,
            "inlier_count": edge.candidate.inlier_count,
        }
        for edge in guided_edges
    ]
    write_json(out_dir / "guided-edges.json", edge_payload)

    return {
        "mode": "overview_guided",
        "layout_kind": layout.kind,
        "overview_page": layout.page_number,
        "placed_pages": placed_pages,
        "page_to_code": {str(page): page_to_code[page] for page in ordered_pages},
        "layout_contact": str(out_dir / "layout-contact.png"),
        "overview_image": str(out_dir / "overview-page.png"),
        "full_merged_image": str(out_dir / "full-merged.png"),
        "full_merged_pdf": str(out_dir / "full-merged.pdf"),
        "guided_edges": edge_payload,
        "missing_pages": [page for page in ordered_pages if page not in placements],
    }


def stitch_group(
    records: Sequence[PageRecord],
    page_numbers: Sequence[int],
    out_dir: Path,
    mode: str,
    min_confidence: float,
) -> Dict[str, Any]:
    ensure_dir(out_dir)
    prev_record = records[page_numbers[0] - 1]
    current_transform = np.eye(3, dtype=np.float32)
    placements: List[Tuple[int, np.ndarray]] = [(page_numbers[0], current_transform.copy())]
    steps: List[Dict[str, Any]] = []

    for page_number in page_numbers[1:]:
        next_record = records[page_number - 1]
        candidate = compare_pages(
            prev_record,
            next_record,
            max_features=2600,
            band_ratio=0.42,
            refine_translation=True,
            max_directions=2,
        )
        if candidate is None:
            raise RuntimeError(f"Could not align page {prev_record.index} to page {page_number}")
        merged, debug, payload = merge_from_candidate(
            prev_record.prepared,
            next_record.prepared,
            candidate,
            mode="auto",
            min_confidence=min_confidence,
        )
        step_dir = out_dir / f"step-{page_number:02d}"
        ensure_dir(step_dir)
        cv2.imwrite(str(step_dir / "merged.png"), merged)
        cv2.imwrite(str(step_dir / "overlap_debug.png"), debug)
        write_json(step_dir / "transform.json", payload)
        steps.append(
            {
                "page": page_number,
                "status": payload["status"],
                "adjacency": payload["adjacency"],
                "confidence": payload["confidence"],
                "structural_score": payload["structural_score"],
                "match_count": payload["match_count"],
                "inlier_count": payload["inlier_count"],
            }
        )
        local_transform = augment_affine(candidate.matrix)
        current_transform = current_transform @ local_transform
        placements.append((page_number, current_transform.copy()))
        prev_record = next_record

    render_group_canvas(records, placements, out_dir / "merged.png")
    group_status = "accepted"
    if any(step["status"] != "accepted" for step in steps):
        group_status = "needs_review"
    return {
        "pages": list(page_numbers),
        "group_status": group_status,
        "steps": steps,
        "merged_image": str(out_dir / "merged.png"),
    }


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out)
    ensure_dir(out_dir)

    pdf_path = Path(args.pdf) if args.pdf else None
    rendered_pages: List[Path]
    if args.rendered_dir:
        rendered_pages = sorted(Path(args.rendered_dir).glob("page-*.png"))
        if not rendered_pages:
            raise RuntimeError(f"No rendered pages found in: {args.rendered_dir}")
        page_count = len(rendered_pages)
    else:
        if pdf_path is None:
            raise RuntimeError("Either --pdf or --rendered-dir must be provided.")
        pdfinfo = require_binary("pdfinfo")
        info = run_command([pdfinfo, str(pdf_path)])
        page_count = parse_page_count(info.stdout)

        render_dir = out_dir / "rendered"
        ensure_dir(render_dir)
        rendered_pages = render_pdf_pages(pdf_path, render_dir, dpi=args.dpi)
        if len(rendered_pages) != page_count:
            raise RuntimeError(
                f"Expected {page_count} rendered pages, got {len(rendered_pages)}."
            )

    work_dir = out_dir / "work"
    ensure_dir(work_dir)
    records = build_page_records(
        rendered_pages,
        work_dir=work_dir,
        title_block_ratio=args.title_block_ratio,
    )

    text_cache: Dict[int, Tuple[List[TextWord], float, float]] = {}
    layout_source = "auto_detected"
    manual_page_matches: List[PageSheetMatch] = []
    overview_layout: OverviewLayout | None = None
    if args.overview_layout_json:
        overview_layout, manual_page_matches = parse_manual_layout(Path(args.overview_layout_json))
        layout_source = "manual_json"
    elif pdf_path is not None:
        overview_layout = detect_overview_layout(pdf_path, page_count, text_cache)
        if overview_layout is not None:
            pass

    if overview_layout is not None:
        if manual_page_matches:
            page_matches = sorted(
                [match for match in manual_page_matches if match.code in overview_layout.placements],
                key=lambda item: item.page_number,
            )
        elif overview_layout.kind == "numeric_index":
            page_matches = derive_page_matches_from_numeric_index(overview_layout, page_count)
        else:
            page_matches = []
            if pdf_path is not None:
                seen_codes: Dict[str, PageSheetMatch] = {}
                for page_number in range(1, page_count + 1):
                    if page_number == overview_layout.page_number:
                        continue
                    match = detect_sheet_code_for_page(pdf_path, page_number, text_cache)
                    if match is None or match.code not in overview_layout.placements:
                        continue
                    prev = seen_codes.get(match.code)
                    if prev is None or match.score > prev.score:
                        seen_codes[match.code] = match
                page_matches = sorted(seen_codes.values(), key=lambda item: item.page_number)

        if pdf_path is not None or manual_page_matches:
            if len(page_matches) >= max(4, len(overview_layout.placements) // 2):
                final_dir = out_dir / "final"
                ensure_dir(final_dir)
                guided_summary = stitch_overview_guided_document(
                    records,
                    overview_layout,
                    page_matches,
                    final_dir,
                    mode=args.mode,
                    min_confidence=max(0.55, args.group_threshold),
                )
                summary = {
                    "pdf": str(pdf_path),
                    "rendered_dir": args.rendered_dir,
                    "page_count": page_count,
                    "dpi": args.dpi,
                    "title_block_ratio": args.title_block_ratio,
                    "mode": guided_summary["mode"],
                    "layout_source": layout_source,
                    "overview_layout": {
                        "kind": overview_layout.kind,
                        "page_number": overview_layout.page_number,
                        "placements": {
                            code: {
                                "row": placement.row,
                                "col": placement.col,
                                "center_x": placement.center_x,
                                "center_y": placement.center_y,
                            }
                            for code, placement in overview_layout.placements.items()
                        },
                    },
                    "page_matches": [
                        {
                            "page_number": match.page_number,
                            "code": match.code,
                            "score": match.score,
                            "source_text": match.source_text,
                        }
                        for match in page_matches
                    ],
                    "final_outputs": {
                        "overview_image": guided_summary["overview_image"],
                        "layout_contact": guided_summary["layout_contact"],
                        "full_merged_image": guided_summary["full_merged_image"],
                        "full_merged_pdf": guided_summary["full_merged_pdf"],
                    },
                    "guided_edges": guided_summary["guided_edges"],
                    "missing_pages": guided_summary["missing_pages"],
                }
                write_json(out_dir / "summary.json", summary)
                print(json.dumps(summary, ensure_ascii=False))
                return

    edges = build_overlap_edges(
        records,
        window_size=args.window_size,
        group_threshold=args.group_threshold,
        min_structural_score=args.min_structural_score,
    )
    groups = build_groups(len(records), edges)

    group_results: List[Dict[str, Any]] = []
    groups_dir = out_dir / "groups"
    ensure_dir(groups_dir)
    for group_index, page_numbers in enumerate(groups, start=1):
        group_dir = groups_dir / f"group-{group_index:02d}"
        ensure_dir(group_dir)
        create_contact_sheet(records, page_numbers, group_dir / "contact.png")
        result: Dict[str, Any] = {
            "group_id": group_index,
            "pages": list(page_numbers),
            "contact_sheet": str(group_dir / "contact.png"),
            "group_status": "accepted",
        }
        if len(page_numbers) > 1:
            result.update(
                stitch_group(
                    records,
                    page_numbers,
                    group_dir,
                    mode=args.mode,
                    min_confidence=max(0.55, args.group_threshold),
                )
            )
        else:
            single = records[page_numbers[0] - 1]
            cv2.imwrite(str(group_dir / "merged.png"), single.prepared.color)
            result["merged_image"] = str(group_dir / "merged.png")
        group_results.append(result)

    final_dir = out_dir / "final"
    ensure_dir(final_dir)
    create_final_group_contact(group_results, final_dir / "merged-groups-contact.png")
    write_groups_pdf(group_results, final_dir / "merged-groups.pdf")

    summary = {
        "pdf": str(pdf_path) if pdf_path else None,
        "rendered_dir": args.rendered_dir,
        "page_count": page_count,
        "dpi": args.dpi,
        "title_block_ratio": args.title_block_ratio,
        "group_threshold": args.group_threshold,
        "min_structural_score": args.min_structural_score,
        "edges": edges,
        "groups": group_results,
        "final_outputs": {
            "contact_image": str(final_dir / "merged-groups-contact.png"),
            "pdf": str(final_dir / "merged-groups.pdf"),
        },
    }
    write_json(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
