# SheetWeave

An open-source Codex/agent skill for weaving tiled vector drawing PDFs into one larger vector PDF.

The skill is designed for PDFs that contain multiple local/detail drawing pages. If an overview or index page exists, the skill uses it to recover the sheet layout. If no overview exists, it falls back to traditional overlap-based neighbor matching. Raster rendering is used only for layout analysis and review images; the final merged PDF embeds the original source PDF pages to preserve vector content.

## Skill Layout

```text
sheetweave/
  SKILL.md
  agents/openai.yaml
  scripts/
    sheetweave.py
    merge_drawings.py
    merge_pdf_drawings.py
    vector_pdf_export.py
    requirements.txt
  references/
    overview_layout_prompt.md
```

## Runtime Requirements

- Python 3.10 or newer.
- Poppler command-line tools in `PATH`: `pdfinfo`, `pdftoppm`, `pdftotext`.
- A LaTeX distribution with `pdflatex` in `PATH`.
- Python dependencies from `scripts/requirements.txt`.

Install Python dependencies:

```bash
pip install -r scripts/requirements.txt
```

## Usage

From the skill directory:

```bash
python scripts/sheetweave.py \
  --pdf path/to/drawings.pdf \
  --out output/run \
  --mode review
```

With a manual or VLM-produced overview mapping:

```bash
python scripts/sheetweave.py \
  --pdf path/to/drawings.pdf \
  --overview-layout-json path/to/overview-layout.json \
  --out output/run \
  --mode review
```

## Outputs

- `summary.json`: run summary, page mapping, graph edges, and final output paths.
- `final/full-merged.pdf`: merged vector PDF when the layout resolves into one component.
- `final/full-merged.tex`: generated LaTeX/TikZ source used for vector assembly.
- `final/full-merged.png`: raster preview for review only.
- `final/layout-contact.png`: overview-guided contact sheet when overview mode succeeds.
- `vlm-request.json`: ambiguity handoff data when automatic overview matching is weak.

If the drawing set splits into disconnected groups, outputs are written under `groups/group-XX/`.

## Publishing Notes

This repository intentionally excludes sample PDFs and generated outputs. Before publishing to GitHub, add only public fixtures with clear licenses, if any. The default license is MIT; change `LICENSE` if you need a different open-source license.
