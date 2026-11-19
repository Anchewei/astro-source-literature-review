---
name: astro-source-literature-review
description: >-
  Perform a comprehensive literature review for an astronomical source given sky
  coordinates (RA, Dec), search radius, and a user-specified research focus.
  Queries SIMBAD TAP for object IDs, aliases, and bibcodes, fetches metadata
  and abstracts from SciXplorer (SciX/ADS), triages catalog/survey papers from
  dedicated studies, downloads arXiv PDFs/HTML into the project directory for
  core papers, and produces an English-only two-layer report (narrative
  synthesis + structured comparison table + BibTeX).
---

# Astronomical Source Literature Review

## Overview

Automates background literature reviews for astronomical targets by combining
deterministic database queries (**SIMBAD TAP** + **SciXplorer API**) with
research-focus-driven triage, project-directory full-text retrieval
(**arXiv**), and an **English-only** two-layer synthesis report.

> **Portability & Relative Paths Rule**: Always use paths relative to the
> current project working directory (replace `<skill_dir>` with this skill's
> relative path, e.g., `.agent/skills/astro-source-literature-review` or
> `skills/astro-source-literature-review`). Store all working artifacts and
> downloaded papers under `./literature_review/` in the project root — **never**
> inside hidden `.agent/` directories or `/tmp/`.

## Dependencies

- **`uv`**: Required to manage the workspace-local `.venv` and invoke all CLI
  scripts via `uv run --no-project --no-managed-python --python .venv` (or
  `--python .venv/bin/python` on POSIX / `.venv/Scripts/python.exe` on Windows).
- **`literature-search-arxiv`**: Referenced for arXiv Terms of Use compliance
  (`https://info.arxiv.org/help/api/index.html`, max 1 req / 3s, license notice
  at `./.licenses/literature_search_arxiv_LICENSE.txt`, and listing retrieved
  arXiv URLs in the final report). Stage 3 uses the built-in `arxiv-download`
  subcommand so papers always save directly into `./literature_review/papers/`.
- **`credentials`**: Follow its Safe Credentials Protocol for storing and
  verifying `SCIX_API_TOKEN` in `~/.env` or `./.env` (preferred).
  `scix_token.txt` (gitignored) is supported as a local fallback. Never print,
  log, or echo the token value.

## Environment & Token Pre-flight Check

### 1. Create / Verify Workspace-Local `.venv`
To prevent `uv` from probing restricted external Conda or
`~/.local/share/uv/python` directories in sandboxed environments, create a
project-local `.venv` inside the workspace using system Python when available
(on Windows use `.venv/Scripts/python.exe` instead of `.venv/bin/python`):

```bash
uv venv --no-managed-python --python /usr/bin/python3 .venv || uv venv --no-managed-python --python python3 .venv
```

If PDF text extraction (`pypdf`) is needed and `pdftotext` is unavailable,
install `pypdf` into the local `.venv` (requires network / outside sandbox if
not cached; use `"pypdf<7"` when `.venv` uses Python 3.9, since `pypdf >= 7`
requires Python `>= 3.10`):

```bash
uv pip install --python .venv/bin/python "pypdf<7"
```

### 2. Run Pre-flight Check
Always run `check-env` from the project root before any literature query:

```bash
uv run --no-project --no-managed-python --python .venv/bin/python \
  <skill_dir>/scripts/astro_lit_cli.py \
  check-env --output ./literature_review/env.json
```

Inspect `./literature_review/env.json` and **stop immediately to notify the user** if:
1. `token_configured` is `false`: Follow the `credentials` skill to set
   `SCIX_API_TOKEN` in `~/.env` or `./.env` (preferred), or instruct the user to
   paste their SciXplorer / ADS Personal API Token into `<skill_dir>/scix_token.txt`
   (gitignored).
2. `local_venv_available` is `false`: Create `.venv` in the workspace root via
   `uv venv --no-managed-python --python /usr/bin/python3 .venv || uv venv --no-managed-python --python python3 .venv`.
3. `uv_available` is `false`: Stop and guide the user to set up `uv` via the
   `uv` skill.
4. `pdftotext_available` and `pypdf_available` are both `false`: Install `pypdf`
   into `.venv` (`uv pip install --python .venv/bin/python "pypdf<7"` for
   Python 3.9, or `pypdf` for Python `>= 3.10`) or use `--format html` when HTML
   is available on arXiv.

## Quick Start

```bash
# 0. Create project-local .venv if not already present
uv venv --no-managed-python --python /usr/bin/python3 .venv || uv venv --no-managed-python --python python3 .venv

# 1. Pre-flight check
uv run --no-project --no-managed-python --python .venv/bin/python \
  <skill_dir>/scripts/astro_lit_cli.py \
  check-env --output ./literature_review/env.json

# 2. Cone search SIMBAD TAP (auto-falls back to Harvard/IDIA mirrors)
uv run --no-project --no-managed-python --python .venv/bin/python \
  <skill_dir>/scripts/astro_lit_cli.py \
  simbad-query --ra "18:53:18.0" --dec "01:25:25.0" \
  --radius-arcsec 10 --limit 100 --output ./literature_review/simbad.json

# 3. Fetch metadata & abstracts from SciXplorer (use high --limit before filtering)
uv run --no-project --no-managed-python --python .venv/bin/python \
  <skill_dir>/scripts/astro_lit_cli.py \
  scix-fetch --input ./literature_review/simbad.json --limit 500 \
  --output ./literature_review/scix_papers.json

# 4. Filter by user keywords & rank candidate abstracts
uv run --no-project --no-managed-python --python .venv/bin/python \
  <skill_dir>/scripts/astro_lit_cli.py \
  scix-filter --input ./literature_review/scix_papers.json \
  --keywords "star formation,magnetic field,outflow" --limit 15 \
  --output ./literature_review/candidates.json
```

## Utility Scripts

All commands live in `<skill_dir>/scripts/astro_lit_cli.py` and write outputs to
`--output` using relative paths.

- **`check-env`**: Verifies Python, local `.venv`, `pypdf`, `uv`, `pdftotext`,
  and SciXplorer token availability (`SCIX_API_TOKEN` / `ADS_DEV_KEY` env
  vars, `~/.env`, `./.env`, `./scix_token.txt`, or `<skill_dir>/scix_token.txt`).
  - Required: `--output <path>`
  - Optional: `--token-file <path>`
- **`simbad-query`**: ADQL cone search on SIMBAD TAP (`basic`, chunked `ident`,
  and `has_ref JOIN ref`), with automatic mirror fallback (`SIMBAD_TAP_URL`
  override supported), `QUERY_STATUS=OVERFLOW` detection, and a stdout Markdown
  disambiguation table (`OID | Main ID | Type | Sep (") | Bibcodes`).
  - Required: `--ra`, `--dec`, `--radius-arcsec` (>0), `--limit` (>0), `--output`
- **`scix-fetch`**: Batch-queries `https://api.scixplorer.org/v1/search/query`
  (50 bibcodes per request) for SIMBAD-linked bibcodes, reporting `truncated`
  and `missing_bibcodes`.
  - Required: `--input <simbad.json>`, `--limit` (>0), `--output`
  - Optional: `--oid <int>` or `--oids <id1,id2,...>`, `--token-file`
- **`scix-triage`**: Generates a compact Markdown table of `scix-fetch` results.
  - Required: `--input <scix_papers.json>`, `--limit` (>0), `--output <triage.md>`
- **`scix-filter`**: Scores and filters `scix-fetch` papers by `--keywords`
  (comma-separated) and/or `--bibcodes`, outputting the top `--limit` candidates
  with abstracts, `keyword_score`, and `matched_keywords`.
  - Required: `--input <scix_papers.json>`, `--limit` (>0), `--output <candidates.json>`
  - Optional: `--keywords`, `--bibcodes`
- **`arxiv-download`**: Downloads PDF and/or HTML full texts for Tier A arXiv
  IDs directly into `./literature_review/papers/Tier_A/` (named by `<slug>` when
  `--notes-index` is provided, skipping already-downloaded files unless `--force`
  is set, and updating `notes_index.json` with actual `paper_path` or
  `[Abstract Only]` stubs on failure) while enforcing arXiv's 1 req / 3s limit
  and creating `./.licenses/literature_search_arxiv_LICENSE.txt`.
  - Required: `--output <manifest.json>`, plus `--id <id1,id2,...>` or
    `--notes-index <notes_index.json>`
  - Optional: `--format <pdf|html|both>` (default `pdf`), `--output-dir`
    (default `./literature_review/papers`), `--scix <scix_papers.json>`,
    `--force`, `--fail-fast`
- **`init-notes`**: Persists Tier A/B/C classification into
  `./literature_review/notes_index.json`, generates flat
  `./literature_review/papers/Tier_A/` targets (plus `[Abstract Only]` note
  stubs for non-arXiv Tier A papers), tracks `unclassified_bibcodes` /
  `unknown_bibcodes`, and writes single aggregate catalogs
  `./literature_review/Tier_B_catalog.md` and
  `./literature_review/Tier_C_catalog.md`.
  - Required: `--scix <scix_papers.json>`, `--classification <classification.json>`,
    `--output <notes_index.json>`
  - Optional: `--papers-dir` (default `./literature_review/papers`)
- **`verify-notes`**: Checks every Tier A note file in `notes_index.json` is
  present and non-empty, updating `status` to `"completed"` (or `"failed"`).
  - Required: `--notes-index <notes_index.json>`, `--output <notes_index.json>`
- **`paper-grep`**: Extracts text from a downloaded `.pdf` or `.html` paper,
  automatically expands SIMBAD `aliases` into common paper shorthands (e.g.,
  `G034.43+00.24` $\rightarrow$ `G34.43+00.24`, `MM 1` $\rightarrow$ `MM1`),
  extracts observatory `project_codes` (ALMA, JCMT, etc.), and optionally
  extracts key data/analysis/results/conclusion sections (`--include-sections`)
  and plain text (`--text-out`).
  - Required: `--file <paper.pdf|html>`, `--output <matches.json>`, plus
    `--simbad <simbad.json>` or `--aliases`
  - Optional: `--context-chars` (default 600), `--max-matches` (default 20),
    `--no-expand-aliases`, `--include-sections`, `--max-section-chars`
    (default 4000), `--text-out <paper.txt>`
- **`scix-bibtex`**: Exports BibTeX entries via
  `https://api.scixplorer.org/v1/export/bibtex`.
  - Required: `--output <.bib>`, plus `--bibcodes` or `--bibcodes-file`

## Workflow

### Stage 1: Target Resolution & Disambiguation (SIMBAD)
1. Run `simbad-query` outputting to `./literature_review/simbad.json`.
2. Review the printed Markdown disambiguation table and `tap_overflow_warning`:
   - **0 objects**: Stop and ask the user to verify ICRS coordinates or widen
     the search radius.
   - **>1 objects**: First present the printed `OID | Main ID | Type | Sep (") | Bibcodes`
     table and a concise summary of candidate groups in chat, then use the
     `ask_question` tool (`is_multi_select: false`) so the user can click a
     choice directly instead of typing a reply (fall back to plain-text options
     only if `ask_question` is unavailable in the host harness):
     - Put the recommended strategy first, prefixed with `(Recommended)` (e.g.,
       `(Recommended) Merge primary source & associated sub-sources (N unique bibcodes)`).
     - Include alternatives such as merging all objects in the search radius or
       focusing on a single primary `OID`.
     - Do **not** add a manual `"Other"` option—`ask_question` provides one
       automatically for custom `--oid` / `--oids`.

### Stage 2: Metadata Retrieval & Triage (SciXplorer)
1. Run `scix-fetch` with a high `--limit` (e.g., `>= total_unique_bibcodes` or
   `500`) outputting to `./literature_review/scix_papers.json`.
2. Run `scix-filter` with keywords derived from the user's **research focus**
   (or `scix-triage` for a full overview) to inspect candidate abstracts.
3. **Persist Classification & Initialize Tiered Structure**: Save your
   classification for **all** papers in `scix_papers.json` to
   `./literature_review/classification.json` (either `{"bibcode": "A"|"B"|"C"}`
   or `{"A": [...], "B": [...], "C": [...]}`) and run `init-notes`:
   ```bash
   uv run --no-project --no-managed-python --python .venv/bin/python \
     <skill_dir>/scripts/astro_lit_cli.py init-notes \
     --scix ./literature_review/scix_papers.json \
     --classification ./literature_review/classification.json \
     --papers-dir ./literature_review/papers \
     --output ./literature_review/notes_index.json
   ```
   - **Tier A — Core / Dedicated Studies**: Tracked in `notes_index.json` with
     deterministic `<slug>` and globally unique `<slug>_note.md` paths inside flat
     `./literature_review/papers/Tier_A/`. Non-arXiv Tier A papers automatically
     receive an `[Abstract Only]` stub note (`status: "abstract_only"`).
   - **Tier B & Tier C**: Not downloaded; automatically written to single
     aggregate catalogs `./literature_review/Tier_B_catalog.md` and
     `./literature_review/Tier_C_catalog.md` (title, author/year, bibcode,
     abstract).

### Stage 3: Full-Text Extraction & Isolated Subagent Note-Taking
1. **Dynamic Schema**: Based on the user's research focus, define the exact
   observational and physical columns to extract (e.g., *Telescope/Array,
   Band/Freq, Beam Size, RMS Sensitivity, Targeted Molecules/Transitions,
   Derived Parameters, Key Conclusions*).
2. **Download Tier A Papers into Flat `Tier_A/`**: Download all `pending` Tier A
   arXiv papers into `./literature_review/papers/Tier_A/` (omitting `--id`
   automatically downloads all `pending` entries from `notes_index.json`, and
   updates `paper_path` or creates `[Abstract Only]` stubs on failure):
   ```bash
   uv run --no-project --no-managed-python --python .venv/bin/python \
     <skill_dir>/scripts/astro_lit_cli.py arxiv-download \
     --format auto \
     --output-dir ./literature_review/papers/Tier_A \
     --notes-index ./literature_review/notes_index.json \
     --scix ./literature_review/scix_papers.json \
     --output ./literature_review/arxiv_downloads.json
   ```
   (`--format auto` prefers HTML and only falls back to PDF when HTML is
   unavailable, halving arXiv requests; use `--format both` only if PDF copies
   are explicitly needed alongside HTML.)
3. **Pre-Extract Text & Matches**: For each `pending` Tier A entry in
   `notes_index.json`, run `paper-grep` on its `paper_path` (preferring `.html`
   when available, else `.pdf`) with `--simbad ./literature_review/simbad.json`,
   `--include-sections`, and `--text-out ./literature_review/papers/Tier_A/<slug>.txt`
   outputting to `./literature_review/papers/Tier_A/<slug>_matches.json`.
4. **Isolated Subagent Note-Taking (Inverted Pyramid, English Only)**:
   - To avoid exhausting the main context window, define a lightweight
     `paper-note-writer` subagent via `define_subagent` (`EnableWriteTools: true`,
     `EnableMCPTools: false`, `EnableSubagentTools: false`) and invoke it with
     `Model: "inherit"` (Hydrogen Medium/High) in bounded batches (max 3–5
     concurrent). **Fallback**: If subagent tools are unavailable, process Tier A
     papers sequentially one at a time.
   - **Required Subagent Prompt Inputs**: Pass each entry's `bibcode`,
     `author_year`, `slug`, `wikilink`, `research_focus`, `dynamic_schema`,
     `./literature_review/papers/Tier_A/<slug>.txt`, `<slug>_matches.json`, and
     target `note_path` (`./literature_review/papers/Tier_A/<slug>_note.md`).
   - **Single-Writer Rule**: Subagents **never** edit `notes_index.json` or any
     shared file. Each subagent reads ONLY its assigned `<slug>.txt` /
     `<slug>_matches.json` and writes ONLY `<slug>_note.md` in **English only**
     with YAML frontmatter (`bibcode`, `author_year`, `slug`, `wikilink`,
     `status: "completed"`) and an **inverted-pyramid structure**:
     1. **Key Takeaways & Relevance** (top summary aligned to research focus)
     2. **Observational Setup & Model Assumptions** (`project_codes`, telescope,
        band/freq, beam, RMS, $d$, $\kappa_\nu$, DCF factor, etc.)
     3. **Data Analysis & Quantitative Results** (sub-source IDs, measurements)
     4. **Background, Motivation & Source Description**
     5. **Author Discussion, Caveats & Future Work**
   - Every claim and measurement **must** cite its location in the paper
     (`Sec. X.Y` / `p. P` / `Table T` / `Fig. F`).
   - After subagents finish, run `verify-notes` to validate all note files and
     update `notes_index.json` statuses:
     ```bash
     uv run --no-project --no-managed-python --python .venv/bin/python \
       <skill_dir>/scripts/astro_lit_cli.py verify-notes \
       --notes-index ./literature_review/notes_index.json \
       --output ./literature_review/notes_index.json
     ```

### Stage 4: Two-Layer Synthesis Report & BibTeX (Report `.md` English Only + Obsidian Wikilinks)
**CRITICAL**: Derive `<source_slug>` as a short filesystem-safe name from the
primary SIMBAD `main_id` (e.g., `G34.43_MM1`). Write the generated literature
review report file (`./<source_slug>_literature_review.md`) and `.bib` file
(`./<source_slug>_literature_review.bib`) in **English only** (regardless of the
user's prompt language, unless explicitly overridden). Your **chat response**
summarizing completion to the user may follow the user's conversation language.

1. **Export BibTeX**: Run `scix-bibtex` for all cited papers (pass
   `--bibcodes-file ./literature_review/notes_index.json` for Tier A, or
   `--bibcodes <comma-separated-list>` / a combined bibcode file to also include
   any Tier B/C papers cited in Layer 2):
   ```bash
   uv run --no-project --no-managed-python --python .venv/bin/python \
     <skill_dir>/scripts/astro_lit_cli.py scix-bibtex \
     --bibcodes-file ./literature_review/notes_index.json \
     --output ./<source_slug>_literature_review.bib
   ```
2. **Synthesize from Notes & Indexes**: Read `./literature_review/simbad.json`,
   `./literature_review/arxiv_downloads.json`, `./literature_review/notes_index.json`,
   `./literature_review/papers/Tier_A/*_note.md`, `./literature_review/Tier_B_catalog.md`,
   and `./literature_review/Tier_C_catalog.md` (re-opening source papers by
   location tag only when a specific number needs verification).

- **Obsidian Wikilink Citation Format**: Preserve `Author et al. (Year)` display
  text while linking to the globally unique per-paper note:
  - In prose: use `wikilink`, e.g., `[[Pravash_2025_2501.11634_note|Pravash et al. (2025)]]`
  - Inside Markdown tables: use `wikilink_table` (escaped pipe `\|` so columns
    do not split), e.g., `[[Pravash_2025_2501.11634_note\|Pravash et al. (2025)]]`

1. **Layer 1 — Comprehensive Source Narrative (English)**:
   - **1.1 Source Profile & Baseline Physical Parameters**: `main_id`, ICRS
     coordinates, `otype`, SIMBAD disambiguation table, and a **Baseline
     Parameters & Assumptions Table** tailored to the source regime (e.g.,
     distance $d$ or redshift $z$, systemic velocity $V_{\text{sys}}$ /
     $V_{\text{LSR}}$, bolometric/X-ray/IR luminosity $L$, mass $M$,
     temperature $T$, column density $N_{\text{H}}$, flux densities /
     magnitudes, alongside each paper's adopted model assumptions such as $d$,
     extinction/opacity $\kappa_\nu$, cosmology, or spectral model).
   - **1.2 Multi-Scale Spatial Hierarchy & Sub-Source Cross-Identification**:
     Physical picture across scales (e.g., host galaxy / parsec cloud $\rightarrow$
     sub-kpc / clump $\rightarrow$ core / nuclear region $\rightarrow$
     disk / jet / compact components), plus a **Sub-Source Cross-ID Table**
     aligning component names and coordinates across different-resolution
     studies.
   - **1.3 Observational Coverage Matrix & Archival Data Map**: Cross-study
     matrix of **Wavelength / Band / Tracer Category** (e.g., X-ray, Optical/IR,
     Radio/Sub-mm Continuum, Polarization, Spectroscopy/Lines) $\times$
     **Angular Resolution / Spatial Scale** showing observed vs. uncovered
     regimes, plus a list of **Public Surveys & Archival Project Codes** (from
     `paper-grep`'s `project_codes`).
   - **1.4 Thematic Physical Synthesis & Methodology Tracking**: Synthesize
     findings by research theme using Obsidian wikilink citations
     (`[[<slug>_note|Author et al. (Year)]]`), explicitly recording the
     **analysis methods, theoretical frameworks, and author-stated caveats**
     used in each derivation (e.g., SED/spectral fitting, radiative transfer,
     kinematic/dynamical modeling, Virial/Jeans or DCF magnetic-field analysis,
     variability/timing analysis).
   - **1.5 Objective Consensus, Discrepancies & Author-Stated Future Work**:
     Strictly objective list of cross-study consensus, quantitative or
     interpretive discrepancies across papers, and **only the limitations and
     future work explicitly stated by the authors** (do **not** invent
     speculative root-cause diagnoses or ungrounded observing proposals).
2. **Layer 2 — Structured Comparison Table & Appendices (English)**:
   - **Core Studies Table** using the dynamic schema from Stage 3.
   - **Tier B Background Papers** (short summary table).
   - **Tier C Catalog/Survey Mentions** (compact appendix list: survey +
     `bibcode`).
   - **arXiv Attribution & URLs**: Note arXiv Terms of Use
     (`https://info.arxiv.org/help/api/index.html`) and list all retrieved
     arXiv paper URLs and local downloaded PDF paths (`./literature_review/papers/`).

## Rate Limiting

- **SciXplorer API**: Daily quota is 5,000 requests/day. `astro_lit_cli.py`
  enforces **2 requests/second** via UID-scoped cross-process file lock using
  `time.monotonic()`, batches up to 50 bibcodes per query, honors `Retry-After`
  headers, raises `RateLimitError` on HTTP 429, and retries 429/5xx/network
  errors with exponential backoff.
- **SIMBAD TAP**: Enforces **1 request/second** via UID-scoped lock across
  Strasbourg/Harvard/IDIA mirrors with retry/backoff on 429/5xx/network errors.
- **arXiv**: `arxiv-download` enforces **1 request per 3 seconds** via
  UID-scoped lock (`arxiv_rate`) in compliance with arXiv Terms of Use.

## Common Mistakes

- **Saving papers inside hidden `.agent/` directories or `/tmp/`**: Always save
  PDFs and working JSONs to `./literature_review/papers/` and `./literature_review/`.
- **Letting `uv` probe external Conda or `~/.local/share/uv/python`**: Always
  use a project-local `.venv` and invoke `uv run --no-managed-python --python .venv/bin/python`.
- **Using `--limit 5` on `scix-fetch`**: Never cap `scix-fetch` to a small
  number when testing a topic; fetch all bibcodes first, then use `scix-filter`
  with `--keywords` and `--limit`.
- **Writing non-English `.md` literature review reports**: Always write the
  generated `./<source_slug>_literature_review.md` report in English only
  (chat replies may match the user's language).
- **Guessing missing details**: If an older paper lacks an arXiv full text,
  mark `[Abstract Only]` rather than inferring telescope beam sizes or RMS
  values.
