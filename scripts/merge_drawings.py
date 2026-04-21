#!/usr/bin/env python3
"""
Merge two overlapping drawing screenshots into one aligned image.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np


Direction = str
VALID_DIRECTIONS = ("right_of", "left_of", "below", "above")
MAX_CANVAS_PIXELS = 120_000_000


@dataclass
class PreparedImage:
    path: Path
    color: np.ndarray
    gray: np.ndarray
    edge: np.ndarray
    line_map: np.ndarray

    @property
    def width(self) -> int:
        return int(self.color.shape[1])

    @property
    def height(self) -> int:
        return int(self.color.shape[0])


@dataclass
class MatchCandidate:
    direction: Direction
    matrix: np.ndarray
    score: float
    confidence: float
    structural_score: float
    residual_error: float
    match_count: int
    inlier_count: int
    overlap_ratio: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge two overlapping floor-plan images into one output."
    )
    parser.add_argument(
        "--inputs",
        nargs=2,
        required=True,
        metavar=("IMAGE_A", "IMAGE_B"),
        help="Paths to the two images to merge.",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory for merged image and debug artifacts.",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "review"),
        default="auto",
        help="Review mode writes extra debug artifacts and never suppresses low scores.",
    )
    parser.add_argument(
        "--max-features",
        type=int,
        default=6000,
        help="Max ORB features per candidate region.",
    )
    parser.add_argument(
        "--band-ratio",
        type=float,
        default=0.45,
        help="Fraction of image width/height to use for overlap-side matching.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.55,
        help="Minimum confidence required for accepted status in auto mode.",
    )
    return parser.parse_args()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def prepare_image(color: np.ndarray, path: Path | None = None) -> PreparedImage:
    gray = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
    gray, bbox = trim_uniform_borders(gray)
    color = crop_with_bbox(color, bbox)
    gray = normalize_gray(gray)
    edge = build_edge_map(gray)
    line_map = build_line_map(gray)
    return PreparedImage(
        path=path or Path("<memory>"),
        color=color,
        gray=gray,
        edge=edge,
        line_map=line_map,
    )


def load_image(path: Path) -> PreparedImage:
    color = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if color is None:
        raise FileNotFoundError(f"Could not read image: {path}")
    return prepare_image(color=color, path=path)


def trim_uniform_borders(
    gray: np.ndarray,
    tolerance: int = 245,
) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    mask = gray < tolerance
    coords = np.argwhere(mask)
    if coords.size == 0:
        h, w = gray.shape
        return gray, (0, 0, w, h)
    y0, x0 = coords.min(axis=0)
    y1, x1 = coords.max(axis=0) + 1
    return gray[y0:y1, x0:x1], (int(x0), int(y0), int(x1), int(y1))


def crop_with_bbox(color: np.ndarray, bbox: Tuple[int, int, int, int]) -> np.ndarray:
    x0, y0, x1, y1 = bbox
    return color[y0:y1, x0:x1]


def normalize_gray(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    return cv2.equalizeHist(blur)


def build_edge_map(gray: np.ndarray) -> np.ndarray:
    edges = cv2.Canny(gray, 50, 150)
    kernel = np.ones((3, 3), np.uint8)
    return cv2.dilate(edges, kernel, iterations=1)


def build_line_map(gray: np.ndarray) -> np.ndarray:
    _, binary = cv2.threshold(gray, 235, 255, cv2.THRESH_BINARY_INV)
    h, w = gray.shape
    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(25, w // 36), 1)
    )
    vertical_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(25, h // 36))
    )
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel)
    combined = cv2.bitwise_or(horizontal, vertical)
    return cv2.dilate(combined, np.ones((3, 3), np.uint8), iterations=1)


def candidate_regions(image: PreparedImage, band_ratio: float) -> Dict[Direction, Tuple[np.ndarray, Tuple[int, int]]]:
    h, w = image.gray.shape
    band_w = max(80, int(w * band_ratio))
    band_h = max(80, int(h * band_ratio))
    return {
        "right_of": (image.gray[:, max(0, w - band_w):w], (max(0, w - band_w), 0)),
        "left_of": (image.gray[:, 0:band_w], (0, 0)),
        "below": (image.gray[max(0, h - band_h):h, :], (0, max(0, h - band_h))),
        "above": (image.gray[0:band_h, :], (0, 0)),
    }


def opposing_direction(direction: Direction) -> Direction:
    return {
        "right_of": "left_of",
        "left_of": "right_of",
        "below": "above",
        "above": "below",
    }[direction]


def build_orb(max_features: int) -> cv2.ORB:
    return cv2.ORB_create(
        nfeatures=max_features,
        scaleFactor=1.2,
        nlevels=8,
        edgeThreshold=15,
        patchSize=31,
        fastThreshold=5,
    )


def evaluate_direction(
    image_a: PreparedImage,
    image_b: PreparedImage,
    direction: Direction,
    max_features: int,
    band_ratio: float,
    refine_translation: bool = True,
) -> MatchCandidate | None:
    orb = build_orb(max_features)
    regions_a = candidate_regions(image_a, band_ratio)
    regions_b = candidate_regions(image_b, band_ratio)
    patch_a, offset_a = regions_a[direction]
    patch_b, offset_b = regions_b[opposing_direction(direction)]

    kp_a, des_a = orb.detectAndCompute(patch_a, None)
    kp_b, des_b = orb.detectAndCompute(patch_b, None)
    if des_a is None or des_b is None or len(kp_a) < 12 or len(kp_b) < 12:
        return None

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    knn_matches = matcher.knnMatch(des_a, des_b, k=2)
    good: List[cv2.DMatch] = []
    for pair in knn_matches:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < 0.75 * n.distance:
            good.append(m)
    if len(good) < 20:
        return None

    pts_a = np.float32(
        [[kp_a[m.queryIdx].pt[0] + offset_a[0], kp_a[m.queryIdx].pt[1] + offset_a[1]] for m in good]
    )
    pts_b = np.float32(
        [[kp_b[m.trainIdx].pt[0] + offset_b[0], kp_b[m.trainIdx].pt[1] + offset_b[1]] for m in good]
    )
    matrix, inliers = cv2.estimateAffinePartial2D(
        pts_b,
        pts_a,
        method=cv2.RANSAC,
        ransacReprojThreshold=4.0,
        maxIters=5000,
        confidence=0.995,
        refineIters=25,
    )
    if matrix is None or inliers is None:
        return None

    if not is_reasonable_transform(image_a, image_b, matrix):
        return None

    if refine_translation:
        matrix = refine_transform_translation(image_a, image_b, matrix)
        if not is_reasonable_transform(image_a, image_b, matrix):
            return None

    inlier_mask = inliers.ravel().astype(bool)
    inlier_count = int(inlier_mask.sum())
    if inlier_count < 12:
        return None

    residual = reprojection_error(matrix, pts_b[inlier_mask], pts_a[inlier_mask])
    overlap_ratio, structural_score = edge_overlap_score(image_a, image_b, matrix)
    inlier_ratio = inlier_count / max(1, len(good))
    match_score = min(1.0, len(good) / 350.0)
    residual_score = max(0.0, 1.0 - min(residual, 20.0) / 20.0)
    confidence = (
        0.32 * inlier_ratio
        + 0.22 * match_score
        + 0.28 * structural_score
        + 0.10 * residual_score
        + 0.08 * min(1.0, overlap_ratio / 0.22)
    )
    total_score = confidence * (0.5 + 0.5 * structural_score)

    return MatchCandidate(
        direction=direction,
        matrix=matrix,
        score=float(total_score),
        confidence=float(confidence),
        structural_score=float(structural_score),
        residual_error=float(residual),
        match_count=len(good),
        inlier_count=inlier_count,
        overlap_ratio=float(overlap_ratio),
    )


def reprojection_error(matrix: np.ndarray, pts_src: np.ndarray, pts_dst: np.ndarray) -> float:
    transformed = cv2.transform(pts_src.reshape(-1, 1, 2), matrix).reshape(-1, 2)
    errors = np.linalg.norm(transformed - pts_dst, axis=1)
    return float(np.mean(errors)) if len(errors) else 999.0


def estimate_canvas_bounds(
    width_a: int,
    height_a: int,
    width_b: int,
    height_b: int,
    matrix: np.ndarray,
) -> Tuple[int, int, int, int]:
    corners_b = np.array(
        [[0, 0], [width_b, 0], [0, height_b], [width_b, height_b]],
        dtype=np.float32,
    ).reshape(-1, 1, 2)
    warped_corners_b = cv2.transform(corners_b, matrix).reshape(-1, 2)
    all_points = np.vstack(
        [
            np.array(
                [[0, 0], [width_a, 0], [0, height_a], [width_a, height_a]],
                dtype=np.float32,
            ),
            warped_corners_b,
        ]
    )
    min_x = int(np.floor(all_points[:, 0].min()))
    min_y = int(np.floor(all_points[:, 1].min()))
    max_x = int(np.ceil(all_points[:, 0].max()))
    max_y = int(np.ceil(all_points[:, 1].max()))
    return min_x, min_y, max_x, max_y


def is_reasonable_transform(
    image_a: PreparedImage,
    image_b: PreparedImage,
    matrix: np.ndarray,
) -> bool:
    a, b, tx = matrix[0]
    c, d, ty = matrix[1]
    scale_x = math.sqrt(float(a * a + c * c))
    scale_y = math.sqrt(float(b * b + d * d))
    if not (0.85 <= scale_x <= 1.15 and 0.85 <= scale_y <= 1.15):
        return False
    rotation = math.degrees(math.atan2(float(c), float(a)))
    if abs(rotation) > 12:
        return False
    max_dim = max(image_a.width, image_a.height, image_b.width, image_b.height)
    if abs(float(tx)) > max_dim * 3 or abs(float(ty)) > max_dim * 3:
        return False
    min_x, min_y, max_x, max_y = estimate_canvas_bounds(
        image_a.width,
        image_a.height,
        image_b.width,
        image_b.height,
        matrix,
    )
    canvas_w = max_x - min_x
    canvas_h = max_y - min_y
    if canvas_w <= 0 or canvas_h <= 0:
        return False
    if canvas_w * canvas_h > MAX_CANVAS_PIXELS:
        return False
    return True


def build_alignment_canvas(
    image_a: PreparedImage,
    image_b: PreparedImage,
    matrix: np.ndarray,
    source_a: np.ndarray,
    source_b: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    h_a, w_a = source_a.shape[:2]
    h_b, w_b = source_b.shape[:2]
    min_x, min_y, max_x, max_y = estimate_canvas_bounds(w_a, h_a, w_b, h_b, matrix)

    tx = -min(0, min_x)
    ty = -min(0, min_y)
    canvas_w = max_x + tx
    canvas_h = max_y + ty
    if canvas_w * canvas_h > MAX_CANVAS_PIXELS:
        raise ValueError(
            f"Canvas too large for alignment: {canvas_w}x{canvas_h} pixels"
        )
    translate = np.array([[1, 0, tx], [0, 1, ty]], dtype=np.float32)
    shifted_matrix = translate @ np.vstack([matrix, [0, 0, 1]])
    shifted_matrix = shifted_matrix[:2, :]

    canvas_a = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    canvas_a[ty:ty + h_a, tx:tx + w_a] = source_a
    mask_a = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    mask_a[ty:ty + h_a, tx:tx + w_a] = 255

    canvas_b = cv2.warpAffine(
        source_b,
        shifted_matrix,
        (canvas_w, canvas_h),
        flags=cv2.INTER_NEAREST,
        borderValue=0,
    )
    mask_b = cv2.warpAffine(
        np.full((h_b, w_b), 255, dtype=np.uint8),
        shifted_matrix,
        (canvas_w, canvas_h),
        flags=cv2.INTER_NEAREST,
        borderValue=0,
    )
    overlap = cv2.bitwise_and(mask_a, mask_b)
    return canvas_a, canvas_b, mask_a, mask_b, overlap


def refine_transform_translation(
    image_a: PreparedImage,
    image_b: PreparedImage,
    matrix: np.ndarray,
    search_radius: int = 8,
) -> np.ndarray:
    line_a, line_b, _, _, overlap = build_alignment_canvas(
        image_a,
        image_b,
        matrix,
        image_a.line_map,
        image_b.line_map,
    )
    if np.count_nonzero(overlap) == 0:
        return matrix
    x, y, w, h = cv2.boundingRect(overlap)
    roi_a = line_a[y : y + h, x : x + w]
    roi_b = line_b[y : y + h, x : x + w]
    roi_overlap = overlap[y : y + h, x : x + w]
    best_score = -1.0
    best_shift = (0, 0)

    for dx in range(-search_radius, search_radius + 1):
        for dy in range(-search_radius, search_radius + 1):
            shift_matrix = np.array([[1, 0, dx], [0, 1, dy]], dtype=np.float32)
            shifted_b = cv2.warpAffine(
                roi_b,
                shift_matrix,
                (w, h),
                flags=cv2.INTER_NEAREST,
                borderValue=0,
            )
            shifted_overlap = cv2.warpAffine(
                roi_overlap,
                shift_matrix,
                (w, h),
                flags=cv2.INTER_NEAREST,
                borderValue=0,
            )
            mask = shifted_overlap > 0
            if not np.any(mask):
                continue
            a_mask = roi_a > 0
            b_mask = shifted_b > 0
            union = np.count_nonzero((a_mask | b_mask) & mask)
            if union == 0:
                continue
            intersection = np.count_nonzero(a_mask & b_mask & mask)
            score = intersection / union
            score -= 0.003 * (abs(dx) + abs(dy))
            if score > best_score:
                best_score = score
                best_shift = (dx, dy)

    refined = matrix.copy()
    refined[0, 2] += best_shift[0]
    refined[1, 2] += best_shift[1]
    return refined


def edge_overlap_score(
    image_a: PreparedImage,
    image_b: PreparedImage,
    matrix: np.ndarray,
) -> Tuple[float, float]:
    h_a, w_a = image_a.edge.shape
    h_b, w_b = image_b.edge.shape
    edge_a, edge_b, mask_a, mask_b, overlap = build_alignment_canvas(
        image_a,
        image_b,
        matrix,
        image_a.edge,
        image_b.edge,
    )
    overlap_pixels = int(np.count_nonzero(overlap))
    min_area = min(h_a * w_a, h_b * w_b)
    overlap_ratio = overlap_pixels / max(1, min_area)
    if overlap_pixels == 0:
        return 0.0, 0.0

    overlap_binary = overlap > 0
    edge_a_binary = edge_a > 0
    edge_b_binary = edge_b > 0
    line_a, line_b, _, _, _ = build_alignment_canvas(
        image_a,
        image_b,
        matrix,
        image_a.line_map,
        image_b.line_map,
    )
    line_a_binary = line_a > 0
    line_b_binary = line_b > 0
    edge_union = np.count_nonzero((edge_a_binary | edge_b_binary) & overlap_binary)
    edge_intersection = np.count_nonzero(edge_a_binary & edge_b_binary & overlap_binary)
    line_union = np.count_nonzero((line_a_binary | line_b_binary) & overlap_binary)
    line_intersection = np.count_nonzero(line_a_binary & line_b_binary & overlap_binary)
    edge_score = edge_intersection / edge_union if edge_union else 0.0
    line_score = line_intersection / line_union if line_union else 0.0
    structural_score = 0.45 * edge_score + 0.55 * line_score
    return float(overlap_ratio), float(structural_score)


def choose_best_candidate(
    image_a: PreparedImage,
    image_b: PreparedImage,
    max_features: int,
    band_ratio: float,
    refine_translation: bool = True,
) -> MatchCandidate:
    candidates: List[MatchCandidate] = []
    for direction in VALID_DIRECTIONS:
        candidate = evaluate_direction(
            image_a=image_a,
            image_b=image_b,
            direction=direction,
            max_features=max_features,
            band_ratio=band_ratio,
            refine_translation=refine_translation,
        )
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        raise RuntimeError("No valid overlap candidate was found.")
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[0]


def build_payload_from_candidate(
    image_a: PreparedImage,
    image_b: PreparedImage,
    candidate: MatchCandidate,
    shifted_matrix: np.ndarray,
    mode: str,
    min_confidence: float,
) -> Dict[str, Any]:
    status = "accepted"
    if mode == "auto" and candidate.confidence < min_confidence:
        status = "needs_review"

    return {
        "inputs": [
            {"path": str(image_a.path), "width": image_a.width, "height": image_a.height},
            {"path": str(image_b.path), "width": image_b.width, "height": image_b.height},
        ],
        "adjacency": f"image_b_{candidate.direction}_image_a",
        "transform_type": "affine_partial_2d",
        "matrix": [[float(v) for v in row] for row in shifted_matrix],
        "match_count": candidate.match_count,
        "inlier_count": candidate.inlier_count,
        "structural_score": candidate.structural_score,
        "residual_error": candidate.residual_error,
        "overlap_ratio": candidate.overlap_ratio,
        "confidence": candidate.confidence,
        "status": status,
    }


def merge_from_candidate(
    image_a: PreparedImage,
    image_b: PreparedImage,
    candidate: MatchCandidate,
    *,
    mode: str = "auto",
    min_confidence: float = 0.55,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    merged, debug, shifted_matrix = compose_canvas(image_a, image_b, candidate.matrix)
    payload = build_payload_from_candidate(
        image_a,
        image_b,
        candidate,
        shifted_matrix,
        mode=mode,
        min_confidence=min_confidence,
    )
    return merged, debug, payload


def merge_prepared_images(
    image_a: PreparedImage,
    image_b: PreparedImage,
    *,
    mode: str = "auto",
    max_features: int = 6000,
    band_ratio: float = 0.45,
    min_confidence: float = 0.55,
    refine_translation: bool = True,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    best = choose_best_candidate(
        image_a=image_a,
        image_b=image_b,
        max_features=max_features,
        band_ratio=band_ratio,
        refine_translation=refine_translation,
    )
    return merge_from_candidate(
        image_a,
        image_b,
        best,
        mode=mode,
        min_confidence=min_confidence,
    )
    return merged, debug, payload


def compose_canvas(
    image_a: PreparedImage,
    image_b: PreparedImage,
    matrix: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    h_a, w_a = image_a.color.shape[:2]
    h_b, w_b = image_b.color.shape[:2]

    corners_b = np.array(
        [[0, 0], [w_b, 0], [0, h_b], [w_b, h_b]],
        dtype=np.float32,
    ).reshape(-1, 1, 2)
    warped_corners_b = cv2.transform(corners_b, matrix).reshape(-1, 2)
    all_points = np.vstack(
        [
            np.array([[0, 0], [w_a, 0], [0, h_a], [w_a, h_a]], dtype=np.float32),
            warped_corners_b,
        ]
    )
    min_x = int(np.floor(all_points[:, 0].min()))
    min_y = int(np.floor(all_points[:, 1].min()))
    max_x = int(np.ceil(all_points[:, 0].max()))
    max_y = int(np.ceil(all_points[:, 1].max()))

    tx = -min(0, min_x)
    ty = -min(0, min_y)
    canvas_w = max_x + tx
    canvas_h = max_y + ty
    translate = np.array([[1, 0, tx], [0, 1, ty]], dtype=np.float32)
    shifted_matrix = translate @ np.vstack([matrix, [0, 0, 1]])
    shifted_matrix = shifted_matrix[:2, :]

    base = np.full((canvas_h, canvas_w, 3), 255, dtype=np.uint8)
    base[ty:ty + h_a, tx:tx + w_a] = image_a.color
    mask_a = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    mask_a[ty:ty + h_a, tx:tx + w_a] = 255

    warped_b = cv2.warpAffine(
        image_b.color,
        shifted_matrix,
        (canvas_w, canvas_h),
        flags=cv2.INTER_LINEAR,
        borderValue=(255, 255, 255),
    )
    mask_b = cv2.warpAffine(
        np.full((h_b, w_b), 255, dtype=np.uint8),
        shifted_matrix,
        (canvas_w, canvas_h),
        flags=cv2.INTER_NEAREST,
        borderValue=0,
    )

    merged = base.copy()
    only_b_mask = cv2.bitwise_and(mask_b, cv2.bitwise_not(mask_a))
    overlap_mask = cv2.bitwise_and(mask_b, mask_a)
    only_b_idx = only_b_mask > 0
    overlap_idx = overlap_mask > 0
    merged[only_b_idx] = warped_b[only_b_idx]
    merged[overlap_idx] = np.minimum(base[overlap_idx], warped_b[overlap_idx])

    debug_merged = merged
    debug_mask_a = mask_a
    debug_mask_b = mask_b
    debug_overlap = overlap_mask
    debug_limit_pixels = 12_000_000
    if canvas_h * canvas_w > debug_limit_pixels:
        scale = math.sqrt(debug_limit_pixels / float(canvas_h * canvas_w))
        scaled_w = max(1, int(canvas_w * scale))
        scaled_h = max(1, int(canvas_h * scale))
        debug_merged = cv2.resize(merged, (scaled_w, scaled_h), interpolation=cv2.INTER_AREA)
        debug_mask_a = cv2.resize(mask_a, (scaled_w, scaled_h), interpolation=cv2.INTER_NEAREST)
        debug_mask_b = cv2.resize(mask_b, (scaled_w, scaled_h), interpolation=cv2.INTER_NEAREST)
        debug_overlap = cv2.resize(overlap_mask, (scaled_w, scaled_h), interpolation=cv2.INTER_NEAREST)

    debug_h, debug_w = debug_merged.shape[:2]
    debug = np.full((debug_h, debug_w, 3), 255, dtype=np.uint8)
    debug[..., 2] = 255 - (debug_mask_a > 0).astype(np.uint8) * 180
    debug[..., 1] = 255 - (debug_mask_b > 0).astype(np.uint8) * 180
    contours, _ = cv2.findContours(debug_overlap, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    debug = cv2.addWeighted(debug, 0.4, debug_merged, 0.6, 0.0)
    cv2.drawContours(debug, contours, -1, (0, 165, 255), 3)

    return merged, debug, shifted_matrix


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_report(path: Path, payload: Dict[str, Any]) -> None:
    lines = [
        "# Drawing Merge Report",
        "",
        f"- Status: `{payload['status']}`",
        f"- Adjacency: `{payload['adjacency']}`",
        f"- Confidence: `{payload['confidence']:.3f}`",
        f"- Structural score: `{payload['structural_score']:.3f}`",
        f"- Match count: `{payload['match_count']}`",
        f"- Inlier count: `{payload['inlier_count']}`",
        f"- Residual error: `{payload['residual_error']:.3f}`",
        f"- Overlap ratio: `{payload['overlap_ratio']:.3f}`",
        "",
        "## Inputs",
        "",
    ]
    for item in payload["inputs"]:
        lines.append(f"- `{item['path']}` ({item['width']}x{item['height']})")
    lines.extend(
        [
            "",
            "## Outputs",
            "",
            "- `merged.png`",
            "- `overlap_debug.png`",
            "- `transform.json`",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out)
    ensure_dir(out_dir)

    image_a = load_image(Path(args.inputs[0]))
    image_b = load_image(Path(args.inputs[1]))
    merged, debug, payload = merge_prepared_images(
        image_a,
        image_b,
        mode=args.mode,
        max_features=args.max_features,
        band_ratio=args.band_ratio,
        min_confidence=args.min_confidence,
    )

    cv2.imwrite(str(out_dir / "merged.png"), merged)
    cv2.imwrite(str(out_dir / "overlap_debug.png"), debug)
    write_json(out_dir / "transform.json", payload)
    write_report(out_dir / "report.md", payload)

    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
