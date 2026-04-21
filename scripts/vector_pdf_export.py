#!/usr/bin/env python3
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from pypdf import PdfReader, PdfWriter


@dataclass
class PagePlacement:
    page_number: int
    page_width_pts: float
    page_height_pts: float
    crop_left_pts: float
    crop_bottom_pts: float
    crop_right_pts: float
    crop_top_pts: float
    transform: list[list[float]]
    label: str = ""

    @property
    def crop_width_pts(self) -> float:
        return self.crop_right_pts - self.crop_left_pts

    @property
    def crop_height_pts(self) -> float:
        return self.crop_top_pts - self.crop_bottom_pts


def normalize_pdf_rotations(source_pdf: Path, out_pdf: Path) -> tuple[Path, bool]:
    reader = PdfReader(str(source_pdf))
    rotations = [int(page.rotation or 0) % 360 for page in reader.pages]
    if all(rotation == 0 for rotation in rotations):
        return source_pdf, False

    writer = PdfWriter()
    for page in reader.pages:
        if int(page.rotation or 0) % 360 != 0:
            page.transfer_rotation_to_content()
        writer.add_page(page)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with out_pdf.open("wb") as handle:
        writer.write(handle)
    return out_pdf, True


def read_page_sizes(pdf_path: Path) -> list[tuple[float, float]]:
    reader = PdfReader(str(pdf_path))
    sizes: list[tuple[float, float]] = []
    for page in reader.pages:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        rotation = int(page.rotation or 0) % 360
        if rotation in (90, 270):
            width, height = height, width
        sizes.append((width, height))
    return sizes


def _transform_point(matrix: Sequence[Sequence[float]], x: float, y: float) -> tuple[float, float]:
    return (
        float(matrix[0][0] * x + matrix[0][1] * y + matrix[0][2]),
        float(matrix[1][0] * x + matrix[1][1] * y + matrix[1][2]),
    )


def compute_canvas_bounds(placements: Iterable[PagePlacement]) -> tuple[float, float, float, float]:
    min_x = math.inf
    min_y = math.inf
    max_x = -math.inf
    max_y = -math.inf
    for placement in placements:
        w = placement.crop_width_pts
        h = placement.crop_height_pts
        for x, y in ((0.0, 0.0), (w, 0.0), (0.0, h), (w, h)):
            px, py = _transform_point(placement.transform, x, y)
            min_x = min(min_x, px)
            min_y = min(min_y, py)
            max_x = max(max_x, px)
            max_y = max(max_y, py)
    if not math.isfinite(min_x):
        raise RuntimeError("No placements were provided for vector export.")
    return min_x, min_y, max_x, max_y


def _latex_escape_path(path: Path) -> str:
    return path.as_posix().replace(" ", "\\space ")


def _format_num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def build_tikz_document(
    source_pdf: Path,
    placements: Sequence[PagePlacement],
    *,
    canvas_width: float,
    canvas_height: float,
    min_x: float,
    min_y: float,
) -> str:
    src_path = _latex_escape_path(source_pdf)
    lines = [
        r"\pdfminorversion=7",
        r"\documentclass{article}",
        rf"\usepackage[paperwidth={_format_num(canvas_width)}bp,paperheight={_format_num(canvas_height)}bp,margin=0bp]{{geometry}}",
        r"\usepackage{graphicx}",
        r"\usepackage{tikz}",
        r"\pagestyle{empty}",
        r"\begin{document}",
        r"\noindent",
        r"\begin{tikzpicture}[x=1bp,y=1bp]",
    ]
    for placement in placements:
        a = placement.transform[0][0]
        b = placement.transform[0][1]
        c = placement.transform[1][0]
        d = placement.transform[1][1]
        tx = placement.transform[0][2] - min_x
        ty = placement.transform[1][2] - min_y
        trim_right = placement.page_width_pts - placement.crop_right_pts
        trim_top = placement.page_height_pts - placement.crop_top_pts
        node = (
            r"\begin{scope}[cm={"
            + ",".join(
                [
                    _format_num(a),
                    _format_num(c),
                    _format_num(b),
                    _format_num(d),
                    f"({_format_num(tx)}bp,{_format_num(ty)}bp)",
                ]
            )
            + r"}]"
        )
        lines.append(node)
        lines.append(
            r"\node[anchor=south west,inner sep=0,outer sep=0] at (0,0) {"
            + rf"\includegraphics[page={placement.page_number},trim={_format_num(placement.crop_left_pts)}bp {_format_num(placement.crop_bottom_pts)}bp {_format_num(trim_right)}bp {_format_num(trim_top)}bp,clip,width={_format_num(placement.crop_width_pts)}bp,height={_format_num(placement.crop_height_pts)}bp]{{{src_path}}}"
            + r"};"
        )
        lines.append(r"\end{scope}")
    lines.extend([r"\end{tikzpicture}", r"\end{document}", ""])
    return "\n".join(lines)


def export_vector_pdf(
    source_pdf: Path,
    placements: Sequence[PagePlacement],
    out_pdf: Path,
    *,
    work_dir: Path,
    job_name: str,
) -> tuple[Path, Path]:
    bounds = compute_canvas_bounds(placements)
    min_x, min_y, max_x, max_y = bounds
    canvas_width = max_x - min_x
    canvas_height = max_y - min_y
    tex_path = work_dir / f"{job_name}.tex"
    log_dir = work_dir
    tex_path.write_text(
        build_tikz_document(
            source_pdf,
            placements,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            min_x=min_x,
            min_y=min_y,
        ),
        encoding="utf-8",
    )
    command = [
        "pdflatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-output-directory",
        str(log_dir),
        str(tex_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    built_pdf = log_dir / f"{job_name}.pdf"
    if not built_pdf.exists():
        raise RuntimeError(f"Expected LaTeX output was not created: {built_pdf}")
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    if built_pdf.resolve() != out_pdf.resolve():
        out_pdf.write_bytes(built_pdf.read_bytes())
    return out_pdf, tex_path
