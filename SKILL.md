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

1. Run:

```bash
python scripts/sheetweave.py \
  --pdf path/to/drawings.pdf \
  --out path/to/output-dir \
  --mode review
```

2. Inspect `summary.json`, `final/full-merged.png`, and `final/layout-contact.png` when present.
3. Use `final/full-merged.pdf` when `summary.json` shows a single connected component.
4. If multiple groups are produced, inspect `groups/group-XX/` and `summary.json` to see which pages failed to bridge.
5. If `vlm-request.json` is produced, read `references/overview_layout_prompt.md`, create an overview layout JSON, and rerun with `--overview-layout-json`.

## Behavior

- The script renders low-resolution page images only for layout recovery.
- It prefers overview/index layout when one is detected.
- It uses sheet-code or numeric labels when available.
- It falls back to visual matching for unlabeled overview regions.
- It falls back to neighbor overlap matching when no overview is usable.
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
