---
name: sheetweave
description: Merge tiled local/detail drawing PDFs into one larger vector PDF. Use when a PDF contains multiple partial drawing sheets, may include an overview/index page, and the user wants faster layout recovery plus final vector output rather than raster-only stitching.
---

# SheetWeave

Use this skill to merge one PDF containing tiled drawing sheets into a larger drawing while preserving vector output.

## Prerequisites

- Install Python dependencies with `pip install -r scripts/requirements.txt`.
- Ensure `pdfinfo`, `pdftoppm`, `pdftotext`, and `pdflatex` are available in `PATH`.
- Work from this skill directory, or pass absolute paths to the scripts.

## Workflow

1. Decide whether the PDF has an overview/thumbnail page. It is usually the first page or the last page.
2. If the overview has machine-readable labels, let the script parse sheet codes or numeric labels automatically.
3. If labels are visible to a person but code cannot extract them reliably, use `references/overview_layout_prompt.md` with a VLM or human to create an overview layout JSON, then run with `--overview-layout-json`.
4. If there is no usable overview/thumbnail page, or if the overview truly has no labels, run the traditional overlap-neighbor route.
5. Run:

```bash
python scripts/sheetweave.py \
  --pdf path/to/drawings.pdf \
  --out path/to/output-dir \
  --mode review
```

6. Inspect `summary.json`, `final/full-merged.png`, and `final/layout-contact.png` when present.
7. Use `final/full-merged.pdf` when `summary.json` shows a single connected component.
8. If multiple groups are produced, inspect `groups/group-XX/` and `summary.json` to see which pages failed to bridge.
9. If `vlm-request.json` is produced, read `references/overview_layout_prompt.md`, create an overview layout JSON, and rerun with `--overview-layout-json`.

## Behavior

- The script renders low-resolution page images only for layout recovery.
- It first looks for overview/index/thumbnail layout candidates, normally on the first or last PDF page.
- It uses sheet-code or numeric labels when code can extract them.
- If labels are visible but not machine-readable, the intended workflow is VLM/human layout mapping rather than blind guessing.
- It falls back to neighbor overlap matching when no overview/thumbnail page is usable or when the overview has no labels.
- It can recover disconnected components with selective high-DPI bridge matching.
- It writes the final merged PDF by embedding original PDF pages into a LaTeX/TikZ canvas, so vector source content stays vector.

## Important Options

- `--render-dpi`: low DPI used for fast layout recovery. Default: `42`.
- `--bridge-render-dpi`: higher DPI used only for cross-component bridge recovery. Default: `110`.
- `--mode review`: write extra diagnostics; recommended for new drawing formats.
- `--overview-layout-json`: use a manual or VLM-produced overview mapping.

## Output Files

- `summary.json`: main run summary and diagnostics.
- `final/full-merged.pdf`: final vector output when there is one component.
- `final/full-merged.png`: raster review preview.
- `final/full-merged.tex`: generated vector assembly source.
- `vlm-request.json`: ambiguity handoff for manual/VLM overview mapping.
