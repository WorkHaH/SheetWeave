#!/usr/bin/env python3
"""
Low-resolution layout recovery + vector PDF assembly for drawing sets.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import cv2
import numpy as np

if __package__:
    from .merge_drawings import (
        PreparedImage,
        VALID_DIRECTIONS,
        build_edge_map,
        build_line_map,
        crop_with_bbox,
        ensure_dir,
        evaluate_direction,
        normalize_gray,
        trim_uniform_borders,
    )
    from .merge_pdf_drawings import (
        MatchCandidate,
        OverviewLayout,
        OverviewPlacement,
        PageSheetMatch,
        build_guided_neighbor_pairs,
        detect_overview_layout,
        detect_sheet_code_for_page,
        derive_page_matches_from_numeric_index,
        extract_tsv_words,
        normalized_correlation,
        parse_manual_layout,
    )
    from .vector_pdf_export import PagePlacement, export_vector_pdf, read_page_sizes
else:  # pragma: no cover - keeps direct script execution convenient.
    from merge_drawings import (
        PreparedImage,
        VALID_DIRECTIONS,
        build_edge_map,
        build_line_map,
        crop_with_bbox,
        ensure_dir,
        evaluate_direction,
        normalize_gray,
        trim_uniform_borders,
    )
    from merge_pdf_drawings import (
        MatchCandidate,
        OverviewLayout,
        OverviewPlacement,
        PageSheetMatch,
        build_guided_neighbor_pairs,
        detect_overview_layout,
        detect_sheet_code_for_page,
        derive_page_matches_from_numeric_index,
        extract_tsv_words,
        normalized_correlation,
        parse_manual_layout,
    )
    from vector_pdf_export import PagePlacement, export_vector_pdf, read_page_sizes


@dataclass
class PageRecord:
    index: int
    image_path: Path
    prepared: PreparedImage
    thumbnail: np.ndarray
    thumb_line: np.ndarray
    page_width_pts: float
    page_height_pts: float
    render_width_px: int
    render_height_px: int
    prepared_bbox_px: tuple[int, int, int, int]


@dataclass
class EdgeRecord:
    page_a: int
    page_b: int
    candidate: MatchCandidate
    source: str
    accepted: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge drawing PDFs into a larger vector PDF."
    )
    parser.add_argument("--pdf", required=True, help="Source drawing PDF.")
    parser.add_argument("--out", required=True, help="Output directory.")
    parser.add_argument(
        "--overview-layout-json",
        help="Optional manual/VLM overview layout JSON.",
    )
    parser.add_argument(
        "--render-dpi",
        type=int,
        default=42,
        help="Low render DPI used for layout recovery only.",
    )
    parser.add_argument(
        "--title-block-ratio",
        type=float,
        default=0.13,
        help="Fraction of right-side width to drop before matching.",
    )
    parser.add_argument(
        "--max-features",
        type=int,
        default=1800,
        help="Max ORB features used for expensive page comparisons.",
    )
    parser.add_argument(
        "--band-ratio",
        type=float,
        default=0.38,
        help="Fraction of each page edge used for overlap matching.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.56,
        help="Minimum confidence for accepted overlap edges.",
    )
    parser.add_argument(
        "--min-structural-score",
        type=float,
        default=0.14,
        help="Minimum structural score for accepted overlap edges.",
    )
    parser.add_argument(
        "--top-k-neighbors",
        type=int,
        default=2,
        help="Per-direction candidate neighbors when no overview page is usable.",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "review"),
        default="review",
        help="Review mode writes more diagnostics.",
    )
    parser.add_argument(
        "--bridge-render-dpi",
        type=int,
        default=110,
        help="High DPI used only for cross-component bridge recovery.",
    )
    parser.add_argument(
        "--bridge-max-features",
        type=int,
        default=3200,
        help="Max ORB features used during cross-component bridge recovery.",
    )
    return parser.parse_args()


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required executable not found in PATH: {name}")
    return path


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def render_pdf_pages(pdf_path: Path, render_dir: Path, dpi: int) -> list[Path]:
    prefix = render_dir / "page"
    command = [
        require_binary("pdftoppm"),
        "-png",
        "-r",
        str(dpi),
        str(pdf_path),
        str(prefix),
    ]
    run_command(command)
    rendered = sorted(render_dir.glob("page-*.png"))
    if not rendered:
        raise RuntimeError(f"No rendered pages were produced for {pdf_path}")
    return rendered


def render_selected_pages(
    pdf_path: Path,
    page_numbers: Sequence[int],
    render_dir: Path,
    dpi: int,
) -> dict[int, Path]:
    ensure_dir(render_dir)
    rendered: dict[int, Path] = {}
    pdftoppm = require_binary("pdftoppm")
    for page_number in sorted(set(page_numbers)):
        out_base = render_dir / f"page-{page_number:03d}"
        command = [
            pdftoppm,
            "-png",
            "-singlefile",
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-r",
            str(dpi),
            str(pdf_path),
            str(out_base),
        ]
        run_command(command)
        out_path = out_base.with_suffix(".png")
        if not out_path.exists():
            raise RuntimeError(f"Expected rendered page was not created: {out_path}")
        rendered[page_number] = out_path
    return rendered


def crop_focus_region_with_bbox(
    color: np.ndarray,
    title_block_ratio: float,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    h, w = color.shape[:2]
    right_crop = int(w * title_block_ratio)
    usable_w = max(200, w - right_crop)
    top_margin = int(h * 0.02)
    bottom_margin = int(h * 0.02)
    left_margin = int(usable_w * 0.01)
    x0 = left_margin
    y0 = top_margin
    x1 = usable_w
    y1 = max(y0 + 1, h - bottom_margin)
    return color[y0:y1, x0:x1], (x0, y0, x1, y1)


def prepare_page_image(
    color: np.ndarray,
    *,
    path: Path,
    title_block_ratio: float,
) -> tuple[PreparedImage, tuple[int, int, int, int]]:
    focused, focus_bbox = crop_focus_region_with_bbox(color, title_block_ratio)
    gray = cv2.cvtColor(focused, cv2.COLOR_BGR2GRAY)
    gray, trim_bbox = trim_uniform_borders(gray)
    focused = crop_with_bbox(focused, trim_bbox)
    gray = normalize_gray(gray)
    edge = build_edge_map(gray)
    line_map = build_line_map(gray)
    bbox = (
        focus_bbox[0] + trim_bbox[0],
        focus_bbox[1] + trim_bbox[1],
        focus_bbox[0] + trim_bbox[2],
        focus_bbox[1] + trim_bbox[3],
    )
    return (
        PreparedImage(path=path, color=focused, gray=gray, edge=edge, line_map=line_map),
        bbox,
    )


def make_thumbnail(image: np.ndarray, max_size: tuple[int, int] = (360, 260)) -> np.ndarray:
    h, w = image.shape[:2]
    scale = min(max_size[0] / max(1, w), max_size[1] / max(1, h))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)


def build_page_records(
    rendered_pages: Sequence[Path],
    page_sizes: Sequence[tuple[float, float]],
    *,
    title_block_ratio: float,
) -> list[PageRecord]:
    records: list[PageRecord] = []
    for index, image_path in enumerate(rendered_pages, start=1):
        color = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if color is None:
            raise RuntimeError(f"Could not read rendered page: {image_path}")
        prepared, bbox = prepare_page_image(
            color,
            path=image_path,
            title_block_ratio=title_block_ratio,
        )
        thumbnail = make_thumbnail(prepared.color)
        thumb_line = make_thumbnail(
            cv2.cvtColor(prepared.line_map, cv2.COLOR_GRAY2BGR),
        )
        thumb_line = cv2.cvtColor(thumb_line, cv2.COLOR_BGR2GRAY)
        page_width_pts, page_height_pts = page_sizes[index - 1]
        records.append(
            PageRecord(
                index=index,
                image_path=image_path,
                prepared=prepared,
                thumbnail=thumbnail,
                thumb_line=thumb_line,
                page_width_pts=page_width_pts,
                page_height_pts=page_height_pts,
                render_width_px=color.shape[1],
                render_height_px=color.shape[0],
                prepared_bbox_px=bbox,
            )
        )
    return records


def build_selected_page_records(
    rendered_pages: dict[int, Path],
    page_sizes: Sequence[tuple[float, float]],
    *,
    title_block_ratio: float,
) -> dict[int, PageRecord]:
    records: dict[int, PageRecord] = {}
    for page_number, image_path in sorted(rendered_pages.items()):
        color = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if color is None:
            raise RuntimeError(f"Could not read rendered page: {image_path}")
        prepared, bbox = prepare_page_image(
            color,
            path=image_path,
            title_block_ratio=title_block_ratio,
        )
        thumbnail = make_thumbnail(prepared.color)
        thumb_line = make_thumbnail(
            cv2.cvtColor(prepared.line_map, cv2.COLOR_GRAY2BGR),
        )
        thumb_line = cv2.cvtColor(thumb_line, cv2.COLOR_BGR2GRAY)
        page_width_pts, page_height_pts = page_sizes[page_number - 1]
        records[page_number] = PageRecord(
            index=page_number,
            image_path=image_path,
            prepared=prepared,
            thumbnail=thumbnail,
            thumb_line=thumb_line,
            page_width_pts=page_width_pts,
            page_height_pts=page_height_pts,
            render_width_px=color.shape[1],
            render_height_px=color.shape[0],
            prepared_bbox_px=bbox,
        )
    return records


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


def resize_to_match(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if a.shape == b.shape:
        return a, b
    target_h = min(a.shape[0], b.shape[0])
    target_w = min(a.shape[1], b.shape[1])
    if target_h <= 0 or target_w <= 0:
        return a, b
    return (
        cv2.resize(a, (target_w, target_h), interpolation=cv2.INTER_AREA),
        cv2.resize(b, (target_w, target_h), interpolation=cv2.INTER_AREA),
    )


def quick_direction_scores(page_a: PageRecord, page_b: PageRecord) -> list[tuple[str, float]]:
    regions_a = thumbnail_candidate_regions(page_a.thumb_line)
    regions_b = thumbnail_candidate_regions(page_b.thumb_line)
    opposite = {
        "right_of": "left_of",
        "left_of": "right_of",
        "below": "above",
        "above": "below",
    }
    scored: list[tuple[str, float]] = []
    for direction in VALID_DIRECTIONS:
        band_a, band_b = resize_to_match(regions_a[direction], regions_b[opposite[direction]])
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
    max_features: int,
    band_ratio: float,
    allowed_directions: Sequence[str] | None,
    max_directions: int,
    refine_translation: bool,
) -> MatchCandidate | None:
    quick_scores = quick_direction_scores(page_a, page_b)
    if allowed_directions is not None:
        allowed = set(allowed_directions)
        quick_scores = [item for item in quick_scores if item[0] in allowed]
    directions = [direction for direction, score in quick_scores[:max_directions] if score >= 0.12]
    if not directions:
        directions = list(allowed_directions or [quick_scores[0][0] if quick_scores else "right_of"])
    candidates: list[MatchCandidate] = []
    for direction in directions:
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


def build_layout_cell_boxes(layout: OverviewLayout) -> dict[str, tuple[float, float, float, float]]:
    row_centers: dict[int, list[float]] = {}
    col_centers: dict[int, list[float]] = {}
    for placement in layout.placements.values():
        row_centers.setdefault(placement.row, []).append(placement.center_y)
        col_centers.setdefault(placement.col, []).append(placement.center_x)
    sorted_rows = sorted((row, sum(values) / len(values)) for row, values in row_centers.items())
    sorted_cols = sorted((col, sum(values) / len(values)) for col, values in col_centers.items())
    row_boundaries: dict[int, tuple[float, float]] = {}
    col_boundaries: dict[int, tuple[float, float]] = {}
    for idx, (row, center) in enumerate(sorted_rows):
        top = 0.0 if idx == 0 else (sorted_rows[idx - 1][1] + center) / 2.0
        bottom = layout.page_height if idx == len(sorted_rows) - 1 else (center + sorted_rows[idx + 1][1]) / 2.0
        row_boundaries[row] = (top, bottom)
    for idx, (col, center) in enumerate(sorted_cols):
        left = 0.0 if idx == 0 else (sorted_cols[idx - 1][1] + center) / 2.0
        right = layout.page_width if idx == len(sorted_cols) - 1 else (center + sorted_cols[idx + 1][1]) / 2.0
        col_boundaries[col] = (left, right)
    boxes: dict[str, tuple[float, float, float, float]] = {}
    for code, placement in layout.placements.items():
        top, bottom = row_boundaries[placement.row]
        left, right = col_boundaries[placement.col]
        boxes[code] = (left, top, right, bottom)
    return boxes


def build_region_preview(
    image: np.ndarray,
    box: tuple[float, float, float, float],
    *,
    page_width: float,
    page_height: float,
) -> np.ndarray | None:
    h, w = image.shape[:2]
    scale_x = w / max(1.0, page_width)
    scale_y = h / max(1.0, page_height)
    left, top, right, bottom = box
    x0 = max(0, int(math.floor(left * scale_x)))
    x1 = min(w, int(math.ceil(right * scale_x)))
    y0 = max(0, int(math.floor(top * scale_y)))
    y1 = min(h, int(math.ceil(bottom * scale_y)))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    region = image[y0:y1, x0:x1]
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    gray = normalize_gray(gray)
    line_map = build_line_map(gray)
    thumb = make_thumbnail(cv2.cvtColor(line_map, cv2.COLOR_GRAY2BGR), max_size=(260, 200))
    return cv2.cvtColor(thumb, cv2.COLOR_BGR2GRAY)


def score_region_match(region_thumb: np.ndarray, page_thumb: np.ndarray) -> float:
    a, b = resize_to_match(region_thumb, page_thumb)
    row_score = normalized_correlation(a.sum(axis=1), b.sum(axis=1))
    col_score = normalized_correlation(a.sum(axis=0), b.sum(axis=0))
    density_a = float(np.count_nonzero(a)) / max(1, a.size)
    density_b = float(np.count_nonzero(b)) / max(1, b.size)
    density_score = 1.0 - abs(density_a - density_b)
    return 0.45 * row_score + 0.35 * col_score + 0.20 * density_score


def derive_visual_overview_matches(
    overview_layout: OverviewLayout,
    overview_record: PageRecord,
    records: Sequence[PageRecord],
    existing_matches: Sequence[PageSheetMatch],
    out_dir: Path,
) -> tuple[list[PageSheetMatch], dict[str, Any]]:
    ensure_dir(out_dir)
    existing_by_page = {match.page_number: match for match in existing_matches}
    existing_codes = {match.code for match in existing_matches}
    boxes = build_layout_cell_boxes(overview_layout)
    overview_image = cv2.imread(str(overview_record.image_path), cv2.IMREAD_COLOR)
    if overview_image is None:
        raise RuntimeError(f"Could not read overview page image: {overview_record.image_path}")

    region_thumbs: dict[str, np.ndarray] = {}
    for code, box in boxes.items():
        if code in existing_codes:
            continue
        thumb = build_region_preview(
            overview_image,
            box,
            page_width=overview_layout.page_width,
            page_height=overview_layout.page_height,
        )
        if thumb is not None:
            region_thumbs[code] = thumb

    candidate_pairs: list[tuple[float, int, str, float]] = []
    diagnostics: dict[str, Any] = {"ambiguous_pages": [], "region_scores": {}}
    for record in records:
        if record.index == overview_layout.page_number or record.index in existing_by_page:
            continue
        per_page_scores: list[tuple[str, float]] = []
        for code, region_thumb in region_thumbs.items():
            score = score_region_match(region_thumb, record.thumb_line)
            per_page_scores.append((code, score))
            candidate_pairs.append((score, record.index, code, score))
        per_page_scores.sort(key=lambda item: item[1], reverse=True)
        diagnostics["region_scores"][str(record.index)] = per_page_scores[:5]
        if len(per_page_scores) >= 2:
            margin = per_page_scores[0][1] - per_page_scores[1][1]
        else:
            margin = per_page_scores[0][1] if per_page_scores else 0.0
        if not per_page_scores or per_page_scores[0][1] < 0.26 or margin < 0.05:
            diagnostics["ambiguous_pages"].append(
                {
                    "page_number": record.index,
                    "top_matches": per_page_scores[:3],
                }
            )

    matched_pages = {match.page_number for match in existing_matches}
    matched_codes = set(existing_codes)
    greedy_matches = list(existing_matches)
    candidate_pairs.sort(reverse=True)
    for score, page_number, code, _ in candidate_pairs:
        if score < 0.24 or page_number in matched_pages or code in matched_codes:
            continue
        greedy_matches.append(
            PageSheetMatch(
                page_number=page_number,
                code=code,
                score=float(score),
                source_text="overview_visual_match",
            )
        )
        matched_pages.add(page_number)
        matched_codes.add(code)

    request_payload = {
        "overview_page": overview_layout.page_number,
        "overview_layout_kind": overview_layout.kind,
        "ambiguous_pages": diagnostics["ambiguous_pages"],
        "matched_pages": [
            {
                "page_number": match.page_number,
                "code": match.code,
                "score": match.score,
                "source_text": match.source_text,
            }
            for match in greedy_matches
        ],
    }
    (out_dir / "vlm-request.json").write_text(
        json.dumps(request_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return sorted(greedy_matches, key=lambda item: item.page_number), diagnostics


def augment_affine(matrix: np.ndarray) -> np.ndarray:
    return np.vstack([matrix, [0.0, 0.0, 1.0]])


def solve_component_placements(
    page_numbers: Sequence[int],
    edges: Sequence[EdgeRecord],
) -> tuple[dict[int, np.ndarray], list[EdgeRecord]]:
    if not page_numbers:
        return {}, []
    parent = {page: page for page in page_numbers}

    def find(page: int) -> int:
        while parent[page] != page:
            parent[page] = parent[parent[page]]
            page = parent[page]
        return page

    def union(a: int, b: int) -> bool:
        root_a = find(a)
        root_b = find(b)
        if root_a == root_b:
            return False
        parent[root_b] = root_a
        return True

    accepted = [edge for edge in edges if edge.accepted]
    fallback = [edge for edge in edges if not edge.accepted]
    accepted.sort(key=lambda item: item.candidate.confidence, reverse=True)
    fallback.sort(key=lambda item: item.candidate.score, reverse=True)

    selected: list[EdgeRecord] = []
    target_edges = max(0, len(page_numbers) - 1)
    for pool in (accepted, fallback):
        for edge in pool:
            if union(edge.page_a, edge.page_b):
                selected.append(edge)
            if len(selected) >= target_edges:
                break
        if len(selected) >= target_edges:
            break

    adjacency: dict[int, list[tuple[int, np.ndarray]]] = {}
    for edge in selected:
        matrix_ab = augment_affine(edge.candidate.matrix)
        matrix_ba = np.linalg.inv(matrix_ab)
        adjacency.setdefault(edge.page_a, []).append((edge.page_b, matrix_ab))
        adjacency.setdefault(edge.page_b, []).append((edge.page_a, matrix_ba))

    placements: dict[int, np.ndarray] = {}
    for anchor in sorted(page_numbers):
        if anchor in placements:
            continue
        placements[anchor] = np.eye(3, dtype=np.float32)
        queue = [anchor]
        while queue:
            current = queue.pop(0)
            current_transform = placements[current]
            for next_page, local_matrix in adjacency.get(current, []):
                if next_page in placements:
                    continue
                placements[next_page] = current_transform @ local_matrix
                queue.append(next_page)
    return placements, selected


def partition_pages(page_numbers: Sequence[int], selected_edges: Sequence[EdgeRecord]) -> list[list[int]]:
    parent = {page: page for page in page_numbers}

    def find(page: int) -> int:
        while parent[page] != page:
            parent[page] = parent[parent[page]]
            page = parent[page]
        return page

    def union(a: int, b: int) -> None:
        root_a = find(a)
        root_b = find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for edge in selected_edges:
        union(edge.page_a, edge.page_b)
    groups: dict[int, list[int]] = {}
    for page in page_numbers:
        groups.setdefault(find(page), []).append(page)
    return [sorted(group) for group in groups.values()]


def build_no_overview_candidate_pairs(
    records: Sequence[PageRecord],
    *,
    top_k: int,
) -> dict[tuple[int, int], list[str]]:
    directional: dict[tuple[int, str], list[tuple[float, int]]] = {}
    for record_a in records:
        for record_b in records:
            if record_a.index == record_b.index:
                continue
            scores = quick_direction_scores(record_a, record_b)
            if not scores:
                continue
            direction, score = scores[0]
            if score < 0.12:
                continue
            directional.setdefault((record_a.index, direction), []).append((score, record_b.index))

    pair_directions: dict[tuple[int, int], list[str]] = {}
    for (page_a, direction), scored in directional.items():
        scored.sort(reverse=True)
        for _, page_b in scored[:top_k]:
            key = tuple(sorted((page_a, page_b)))
            pair_directions.setdefault(key, [])
            if page_a < page_b:
                page_direction = direction
            else:
                opposite = {
                    "right_of": "left_of",
                    "left_of": "right_of",
                    "below": "above",
                    "above": "below",
                }
                page_direction = opposite[direction]
            if page_direction not in pair_directions[key]:
                pair_directions[key].append(page_direction)
    return pair_directions


def compare_candidate_pairs(
    records: Sequence[PageRecord],
    pair_directions: dict[tuple[int, int], list[str]],
    *,
    max_features: int,
    band_ratio: float,
    min_confidence: float,
    min_structural_score: float,
    source: str,
) -> list[EdgeRecord]:
    page_lookup = {record.index: record for record in records}
    edges: list[EdgeRecord] = []
    for (page_a, page_b), directions in sorted(pair_directions.items()):
        candidate = compare_pages(
            page_lookup[page_a],
            page_lookup[page_b],
            max_features=max_features,
            band_ratio=band_ratio,
            allowed_directions=directions,
            max_directions=max(1, len(directions)),
            refine_translation=True,
        )
        if candidate is None:
            continue
        edges.append(
            EdgeRecord(
                page_a=page_a,
                page_b=page_b,
                candidate=candidate,
                source=source,
                accepted=(
                    candidate.confidence >= min_confidence
                    and candidate.structural_score >= min_structural_score
                ),
            )
        )
    return edges


def render_raster_canvas(
    records: Sequence[PageRecord],
    placements: dict[int, np.ndarray],
    out_path: Path,
) -> None:
    corners: list[np.ndarray] = []
    for page_number, transform in placements.items():
        record = records[page_number - 1]
        h, w = record.prepared.color.shape[:2]
        pts = np.array([[0, 0], [w, 0], [0, h], [w, h]], dtype=np.float32).reshape(-1, 1, 2)
        corners.append(cv2.perspectiveTransform(pts, transform).reshape(-1, 2))
    all_points = np.vstack(corners)
    min_x = int(np.floor(all_points[:, 0].min()))
    min_y = int(np.floor(all_points[:, 1].min()))
    max_x = int(np.ceil(all_points[:, 0].max()))
    max_y = int(np.ceil(all_points[:, 1].max()))
    tx = -min(0, min_x)
    ty = -min(0, min_y)
    canvas_w = max_x + tx
    canvas_h = max_y + ty
    translate = np.array([[1.0, 0.0, tx], [0.0, 1.0, ty], [0.0, 0.0, 1.0]], dtype=np.float32)
    merged = np.full((canvas_h, canvas_w, 3), 255, dtype=np.uint8)
    coverage = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    for page_number, transform in placements.items():
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


def build_image_to_pdf_matrix(record: PageRecord) -> np.ndarray:
    x0, y0, x1, y1 = record.prepared_bbox_px
    crop_w_px = x1 - x0
    crop_h_px = y1 - y0
    sx = record.page_width_pts / max(1.0, record.render_width_px)
    sy = record.page_height_pts / max(1.0, record.render_height_px)
    crop_h_pts = crop_h_px * sy
    return np.array(
        [
            [sx, 0.0, 0.0],
            [0.0, -sy, crop_h_pts],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def build_page_placement(record: PageRecord, transform: np.ndarray, anchor_record: PageRecord) -> PagePlacement:
    anchor_matrix = build_image_to_pdf_matrix(anchor_record)
    local_matrix = build_image_to_pdf_matrix(record)
    transform_pdf = anchor_matrix @ transform @ np.linalg.inv(local_matrix)
    x0, y0, x1, y1 = record.prepared_bbox_px
    sx = record.page_width_pts / max(1.0, record.render_width_px)
    sy = record.page_height_pts / max(1.0, record.render_height_px)
    crop_left = x0 * sx
    crop_right = x1 * sx
    crop_top = record.page_height_pts - y0 * sy
    crop_bottom = record.page_height_pts - y1 * sy
    return PagePlacement(
        page_number=record.index,
        page_width_pts=record.page_width_pts,
        page_height_pts=record.page_height_pts,
        crop_left_pts=float(crop_left),
        crop_bottom_pts=float(crop_bottom),
        crop_right_pts=float(crop_right),
        crop_top_pts=float(crop_top),
        transform=transform_pdf.tolist(),
        label=f"P{record.index:02d}",
    )


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_overview_solution(
    layout: OverviewLayout,
    page_matches: Sequence[PageSheetMatch],
    records: Sequence[PageRecord],
    *,
    max_features: int,
    band_ratio: float,
    min_confidence: float,
    min_structural_score: float,
) -> tuple[list[EdgeRecord], list[int], dict[str, str]]:
    page_lookup = {match.code: match.page_number for match in page_matches if match.code in layout.placements}
    page_to_code = {str(page): code for code, page in page_lookup.items()}
    guided_pairs = build_guided_neighbor_pairs(layout, page_lookup)
    pair_directions = {
        tuple(sorted((page_a, page_b))): [direction]
        for page_a, page_b, _, _, direction in guided_pairs
    }
    edges = compare_candidate_pairs(
        records,
        pair_directions,
        max_features=max_features,
        band_ratio=band_ratio,
        min_confidence=min_confidence,
        min_structural_score=min_structural_score,
        source="overview_guided",
    )
    placed_pages = sorted(page_lookup.values())
    return edges, placed_pages, page_to_code


def build_component_index(components: Sequence[Sequence[int]]) -> dict[int, int]:
    component_index: dict[int, int] = {}
    for idx, component in enumerate(components):
        for page in component:
            component_index[page] = idx
    return component_index


def build_page_placement_lookup(
    layout: OverviewLayout,
    page_matches: Sequence[PageSheetMatch],
) -> dict[int, OverviewPlacement]:
    placement_lookup: dict[int, OverviewPlacement] = {}
    for match in page_matches:
        placement = layout.placements.get(match.code)
        if placement is not None:
            placement_lookup[match.page_number] = placement
    return placement_lookup


def build_cross_component_bridge_pairs(
    layout: OverviewLayout,
    page_matches: Sequence[PageSheetMatch],
    components: Sequence[Sequence[int]],
    existing_edges: Sequence[EdgeRecord],
) -> dict[tuple[int, int], list[str]]:
    page_lookup = {
        match.code: match.page_number
        for match in page_matches
        if match.code in layout.placements
    }
    component_index = build_component_index(components)
    edge_lookup = {
        tuple(sorted((edge.page_a, edge.page_b))): edge
        for edge in existing_edges
    }
    bridge_pairs: dict[tuple[int, int], list[str]] = {}
    for page_a, page_b, _, _, direction in build_guided_neighbor_pairs(layout, page_lookup):
        if component_index.get(page_a) == component_index.get(page_b):
            continue
        key = tuple(sorted((page_a, page_b)))
        existing = edge_lookup.get(key)
        if existing is not None and existing.accepted:
            continue
        bridge_pairs[key] = [direction]
    return bridge_pairs


def convert_candidate_to_target_space(
    candidate: MatchCandidate,
    source_a: PageRecord,
    source_b: PageRecord,
    target_a: PageRecord,
    target_b: PageRecord,
) -> MatchCandidate:
    scale_a_x = target_a.prepared.width / max(1.0, source_a.prepared.width)
    scale_a_y = target_a.prepared.height / max(1.0, source_a.prepared.height)
    scale_b_x = target_b.prepared.width / max(1.0, source_b.prepared.width)
    scale_b_y = target_b.prepared.height / max(1.0, source_b.prepared.height)
    scale_a = np.array(
        [[scale_a_x, 0.0, 0.0], [0.0, scale_a_y, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    scale_b = np.array(
        [[scale_b_x, 0.0, 0.0], [0.0, scale_b_y, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    matrix = augment_affine(candidate.matrix)
    converted = scale_a @ matrix @ np.linalg.inv(scale_b)
    return MatchCandidate(
        direction=candidate.direction,
        matrix=converted[:2, :],
        score=candidate.score,
        confidence=candidate.confidence,
        structural_score=candidate.structural_score,
        residual_error=candidate.residual_error * ((scale_a_x + scale_a_y) / 2.0),
        match_count=candidate.match_count,
        inlier_count=candidate.inlier_count,
        overlap_ratio=candidate.overlap_ratio,
    )


def recover_bridge_edges(
    pdf_path: Path,
    lowres_records: Sequence[PageRecord],
    page_sizes: Sequence[tuple[float, float]],
    layout: OverviewLayout,
    page_matches: Sequence[PageSheetMatch],
    components: Sequence[Sequence[int]],
    existing_edges: Sequence[EdgeRecord],
    *,
    render_dpi: int,
    title_block_ratio: float,
    max_features: int,
    band_ratio: float,
    min_confidence: float,
    min_structural_score: float,
    work_dir: Path,
) -> list[EdgeRecord]:
    bridge_pairs = build_cross_component_bridge_pairs(
        layout,
        page_matches,
        components,
        existing_edges,
    )
    if not bridge_pairs:
        return []
    selected_pages = sorted({page for pair in bridge_pairs for page in pair})
    rendered = render_selected_pages(
        pdf_path,
        selected_pages,
        work_dir / "bridge-rendered",
        dpi=render_dpi,
    )
    highres_records = build_selected_page_records(
        rendered,
        page_sizes,
        title_block_ratio=title_block_ratio,
    )
    lowres_lookup = {record.index: record for record in lowres_records}
    bridge_edges: list[EdgeRecord] = []
    for (page_a, page_b), directions in sorted(bridge_pairs.items()):
        candidate = compare_pages(
            highres_records[page_a],
            highres_records[page_b],
            max_features=max_features,
            band_ratio=band_ratio,
            allowed_directions=directions,
            max_directions=max(1, len(directions)),
            refine_translation=True,
        )
        if candidate is None:
            continue
        converted = convert_candidate_to_target_space(
            candidate,
            highres_records[page_a],
            highres_records[page_b],
            lowres_lookup[page_a],
            lowres_lookup[page_b],
        )
        bridge_edges.append(
            EdgeRecord(
                page_a=page_a,
                page_b=page_b,
                candidate=converted,
                source="bridge_highres",
                accepted=(
                    converted.confidence >= min_confidence
                    and converted.structural_score >= min_structural_score
                ),
            )
        )
    return bridge_edges


def synthesize_template_bridge_edges(
    layout: OverviewLayout,
    page_matches: Sequence[PageSheetMatch],
    components: Sequence[Sequence[int]],
    existing_edges: Sequence[EdgeRecord],
) -> list[EdgeRecord]:
    page_placements = build_page_placement_lookup(layout, page_matches)
    bridge_pairs = build_cross_component_bridge_pairs(
        layout,
        page_matches,
        components,
        existing_edges,
    )
    if not bridge_pairs:
        return []

    templates: dict[tuple[int, int], list[EdgeRecord]] = {}
    for edge in existing_edges:
        placement_a = page_placements.get(edge.page_a)
        placement_b = page_placements.get(edge.page_b)
        if placement_a is None or placement_b is None:
            continue
        signature = (
            placement_b.row - placement_a.row,
            placement_b.col - placement_a.col,
        )
        templates.setdefault(signature, []).append(edge)

    synthetic_edges: list[EdgeRecord] = []
    for (page_a, page_b), _directions in sorted(bridge_pairs.items()):
        placement_a = page_placements.get(page_a)
        placement_b = page_placements.get(page_b)
        if placement_a is None or placement_b is None:
            continue
        signature = (
            placement_b.row - placement_a.row,
            placement_b.col - placement_a.col,
        )
        template_edges = templates.get(signature, [])
        if not template_edges:
            continue
        matrices = np.stack([augment_affine(edge.candidate.matrix) for edge in template_edges], axis=0)
        median_matrix = np.median(matrices, axis=0)
        confidence = float(np.median([edge.candidate.confidence for edge in template_edges]))
        structural = float(np.median([edge.candidate.structural_score for edge in template_edges]))
        score = float(np.median([edge.candidate.score for edge in template_edges]))
        match_count = int(round(float(np.median([edge.candidate.match_count for edge in template_edges]))))
        inlier_count = int(round(float(np.median([edge.candidate.inlier_count for edge in template_edges]))))
        overlap_ratio = float(np.median([edge.candidate.overlap_ratio for edge in template_edges]))
        residual_error = float(np.median([edge.candidate.residual_error for edge in template_edges]))
        template = MatchCandidate(
            direction=template_edges[0].candidate.direction,
            matrix=median_matrix[:2, :],
            score=score,
            confidence=confidence,
            structural_score=structural,
            residual_error=residual_error,
            match_count=match_count,
            inlier_count=inlier_count,
            overlap_ratio=overlap_ratio,
        )
        synthetic_edges.append(
            EdgeRecord(
                page_a=page_a,
                page_b=page_b,
                candidate=template,
                source="overview_template",
                accepted=True,
            )
        )
    return synthetic_edges


def create_layout_contact_sheet(
    records: Sequence[PageRecord],
    ordered_pages: Sequence[int],
    page_to_code: Dict[str, str],
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
        canvas[y:y + th, x:x + tw] = thumb
        label = f"P{page_number:02d}  {page_to_code.get(str(page_number), '?')}"
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


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    work_dir = out_dir / "work"
    ensure_dir(work_dir)

    source_pdf = Path(args.pdf)
    normalized_pdf = source_pdf
    normalized = False
    render_dir = work_dir / "rendered"
    ensure_dir(render_dir)
    rendered_pages = render_pdf_pages(normalized_pdf, render_dir, dpi=args.render_dpi)
    page_sizes = read_page_sizes(normalized_pdf)
    if len(rendered_pages) != len(page_sizes):
        raise RuntimeError(
            f"Rendered {len(rendered_pages)} pages but PDF has {len(page_sizes)} pages."
        )

    records = build_page_records(
        rendered_pages,
        page_sizes,
        title_block_ratio=args.title_block_ratio,
    )
    page_count = len(records)
    text_cache: Dict[int, Tuple[List[Any], float, float]] = {}

    overview_layout: OverviewLayout | None = None
    manual_matches: list[PageSheetMatch] = []
    layout_source = "none"
    if args.overview_layout_json:
        overview_layout, manual_matches = parse_manual_layout(Path(args.overview_layout_json))
        layout_source = "manual_json"
    else:
        overview_layout = detect_overview_layout(normalized_pdf, page_count, text_cache)
        if overview_layout is not None:
            layout_source = "ocr_auto"

    page_matches: list[PageSheetMatch] = []
    overview_diagnostics: dict[str, Any] = {}
    ordered_pages: list[int] = []
    page_to_code: dict[str, str] = {}
    edges: list[EdgeRecord]
    solution_mode = "neighbor_graph"
    if overview_layout is not None:
        if manual_matches:
            page_matches = [match for match in manual_matches if match.code in overview_layout.placements]
        elif overview_layout.kind == "numeric_index":
            page_matches = derive_page_matches_from_numeric_index(overview_layout, page_count)
        else:
            page_matches = []
            for page_number in range(1, page_count + 1):
                if page_number == overview_layout.page_number:
                    continue
                match = detect_sheet_code_for_page(normalized_pdf, page_number, text_cache)
                if match is not None and match.code in overview_layout.placements:
                    page_matches.append(match)
        page_matches, overview_diagnostics = derive_visual_overview_matches(
            overview_layout,
            records[overview_layout.page_number - 1],
            records,
            page_matches,
            out_dir / "final",
        )
        if len(page_matches) >= max(4, len(overview_layout.placements) // 2):
            edges, ordered_pages, page_to_code = build_overview_solution(
                overview_layout,
                page_matches,
                records,
                max_features=max(2200, args.max_features),
                band_ratio=max(0.40, args.band_ratio),
                min_confidence=max(0.52, args.min_confidence * 0.92),
                min_structural_score=max(0.10, args.min_structural_score * 0.85),
            )
            solution_mode = "overview_guided"
        else:
            pair_directions = build_no_overview_candidate_pairs(records, top_k=args.top_k_neighbors)
            edges = compare_candidate_pairs(
                records,
                pair_directions,
                max_features=args.max_features,
                band_ratio=args.band_ratio,
                min_confidence=args.min_confidence,
                min_structural_score=args.min_structural_score,
                source="neighbor_graph",
            )
    else:
        pair_directions = build_no_overview_candidate_pairs(records, top_k=args.top_k_neighbors)
        edges = compare_candidate_pairs(
            records,
            pair_directions,
            max_features=args.max_features,
            band_ratio=args.band_ratio,
            min_confidence=args.min_confidence,
            min_structural_score=args.min_structural_score,
            source="neighbor_graph",
        )

    all_pages = list(range(1, page_count + 1))
    if solution_mode == "overview_guided" and overview_layout is not None:
        solved_pages = sorted(set(ordered_pages))
    else:
        solved_pages = all_pages

    placements, selected_edges = solve_component_placements(solved_pages, edges)
    components = partition_pages(solved_pages, selected_edges)
    bridge_edges: list[EdgeRecord] = []
    synthetic_edges: list[EdgeRecord] = []

    if (
        solution_mode == "overview_guided"
        and overview_layout is not None
        and len(components) > 1
    ):
        bridge_edges = recover_bridge_edges(
            normalized_pdf,
            records,
            page_sizes,
            overview_layout,
            page_matches,
            components,
            edges,
            render_dpi=args.bridge_render_dpi,
            title_block_ratio=args.title_block_ratio,
            max_features=args.bridge_max_features,
            band_ratio=max(0.42, args.band_ratio),
            min_confidence=max(0.45, args.min_confidence * 0.80),
            min_structural_score=max(0.10, args.min_structural_score * 0.75),
            work_dir=work_dir,
        )
        if bridge_edges:
            edges = edges + bridge_edges
            placements, selected_edges = solve_component_placements(solved_pages, edges)
            components = partition_pages(solved_pages, selected_edges)
        if len(components) > 1:
            synthetic_edges = synthesize_template_bridge_edges(
                overview_layout,
                page_matches,
                components,
                edges,
            )
            if synthetic_edges:
                edges = edges + synthetic_edges
                placements, selected_edges = solve_component_placements(solved_pages, edges)
                components = partition_pages(solved_pages, selected_edges)

    final_dir = out_dir / "final"
    ensure_dir(final_dir)
    outputs: dict[str, Any] = {}
    component_payloads: list[dict[str, Any]] = []

    if solution_mode == "overview_guided" and overview_layout is not None:
        create_layout_contact_sheet(records, ordered_pages, page_to_code, final_dir / "layout-contact.png")

    for idx, component_pages in enumerate(components, start=1):
        component_dir = final_dir if len(components) == 1 else out_dir / "groups" / f"group-{idx:02d}"
        ensure_dir(component_dir)
        component_placements = {page: placements[page] for page in component_pages if page in placements}
        if not component_placements:
            continue
        raster_name = "full-merged.png" if len(components) == 1 else "merged.png"
        render_raster_canvas(records, component_placements, component_dir / raster_name)
        anchor_page = min(component_pages)
        anchor_record = records[anchor_page - 1]
        vector_items = [
            build_page_placement(records[page - 1], component_placements[page], anchor_record)
            for page in sorted(component_placements)
        ]
        job_name = "full-merged" if len(components) == 1 else f"group-{idx:02d}-merged"
        pdf_name = "full-merged.pdf" if len(components) == 1 else "merged-vector.pdf"
        pdf_path, tex_path = export_vector_pdf(
            normalized_pdf,
            vector_items,
            component_dir / pdf_name,
            work_dir=component_dir,
            job_name=job_name,
        )
        payload = {
            "group_id": idx,
            "pages": sorted(component_placements),
            "raster_preview": str(component_dir / raster_name),
            "vector_pdf": str(pdf_path),
            "latex_source": str(tex_path),
        }
        component_payloads.append(payload)
        if len(components) == 1:
            outputs = payload

    summary = {
        "source_pdf": str(source_pdf),
        "normalized_pdf": str(normalized_pdf),
        "normalized_rotations": normalized,
        "page_count": page_count,
        "render_dpi": args.render_dpi,
        "solution_mode": solution_mode,
        "overview_layout": None
        if overview_layout is None
        else {
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
        "layout_source": layout_source,
        "page_matches": [
            {
                "page_number": match.page_number,
                "code": match.code,
                "score": match.score,
                "source_text": match.source_text,
            }
            for match in page_matches
        ],
        "overview_diagnostics": overview_diagnostics,
        "edge_count": len(edges),
        "selected_edge_count": len(selected_edges),
        "bridge_edge_count": len(bridge_edges),
        "synthetic_edge_count": len(synthetic_edges),
        "edges": [
            {
                "page_a": edge.page_a,
                "page_b": edge.page_b,
                "direction": edge.candidate.direction,
                "confidence": edge.candidate.confidence,
                "structural_score": edge.candidate.structural_score,
                "score": edge.candidate.score,
                "source": edge.source,
                "accepted": edge.accepted,
            }
            for edge in edges
        ],
        "components": component_payloads,
        "final_outputs": outputs,
    }
    write_json(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
