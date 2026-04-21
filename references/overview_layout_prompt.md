# Overview Layout Prompt

Use this prompt when the PDF contains an overview/index page but automatic page-to-region matching is ambiguous.

## Goal

Read the overview page image and the candidate detail-page previews, then output strict JSON describing:

- which page is the overview page
- where each detail region sits in the overview layout
- which PDF detail page maps to which overview region

## Prompt

```text
You are reading an architectural drawing overview page and a set of detail sheet previews.

Task:
1. Identify the overview/index page number.
2. Identify each detail sheet region shown inside the overview.
3. For each region, output a stable code.
4. Infer a coarse layout position using integer row and col values.
5. Map each detail PDF page_number to the matching overview region code.

Output strict JSON only.

Schema:
{
  "kind": "manual_vlm",
  "overview_page": 39,
  "placements": [
    { "code": "R01", "row": 0, "col": 0 },
    { "code": "R02", "row": 0, "col": 1 }
  ],
  "page_matches": [
    { "page_number": 1, "code": "R01", "source_text": "manual_vlm" },
    { "page_number": 2, "code": "R02", "source_text": "manual_vlm" }
  ]
}

Rules:
- Return JSON only, no markdown fences.
- row and col are zero-based integers.
- code must be stable and unique.
- If you are unsure about a mapping, omit it instead of guessing.
- Prefer the large detail-sheet regions, not title text, dimensions, or axis labels.
```
