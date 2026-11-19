# Astro Source Literature Review

An [Antigravity](https://antigravity.google/) agent skill for performing structured, coordinate-based astronomical literature reviews using **SIMBAD TAP** (Table Access Protocol), the **SciXplorer (SciX/ADS) API**, and **arXiv**.

Given sky coordinates (RA, Dec), a search radius, and a research focus, the agent runs a Python CLI to resolve target identifiers, fetch paper metadata and abstracts, triage references into three tiers, extract key data and sections from open-access full texts into per-paper Markdown notes, and synthesize an English-only two-layer review report with a BibTeX bibliography.

---

## Features

- **Coordinate-Based Target Disambiguation**: Queries SIMBAD TAP for objects, aliases, and linked bibcodes with automatic fallback across Strasbourg, Harvard, and IDIA (Inter-University Institute for Data Intensive Astronomy) mirrors, displaying an `OID | Main ID | Type | Sep (") | Bibcodes` table (where `OID` is SIMBAD's internal Object ID) so you can confirm the target before proceeding.
- **Metadata & Abstract Triage**: Batch-queries SciXplorer by bibcode (50 bibcodes per request), scores candidates against research-focus keywords, and organizes papers into:
  - **Tier A**: Core / dedicated studies (full-text extraction + per-paper note).
  - **Tier B**: Relevant background studies (recorded in `Tier_B_catalog.md`).
  - **Tier C**: Survey or catalog mentions (recorded in `Tier_C_catalog.md`).
- **Isolated Per-Paper Note-Taking**: Pre-extracts plain text, expanded SIMBAD aliases, and observatory project codes (`ALMA`, `JCMT`, `VLA`/`VLBA`, `ESO`/`VLT`, `JWST`/`HST`, `Chandra`/`XMM` `ObsID`, `NOEMA`/`IRAM`) locally, then writes one English note per Tier A paper (`<slug>_note.md`, where `<slug>` is `<FirstAuthor>_<Year>_<arXivOrBibcode>`) in an inverted-pyramid structure (key takeaways first, followed by setup, results, background, and discussion).
- **Standard Markdown & Optional Obsidian Wikilinks**:
  - **Layer 1**: Source profile, baseline parameters/assumptions table, multi-scale spatial hierarchy and sub-source cross-ID table, observational coverage matrix, thematic synthesis, and objective consensus/discrepancies/author-stated future work.
  - **Layer 2**: Structured comparison table aligned to the research focus, Tier B/C summaries, and arXiv attributions (source URLs and local paths).
  - All generated files are standard Markdown readable in any editor; citations use `[[<slug>_note|Author et al. (Year)]]` wikilinks so they also work as clickable links in [Obsidian](https://obsidian.md/).
- **Automated BibTeX Export**: Exports BibTeX entries directly from SciXplorer into `<source_slug>_literature_review.bib`.

---

## Installation

Place the `astro-source-literature-review` folder into one of Antigravity's discovered skill directories:

- **Workspace-scoped (for per-project use)**:
  - `<your-workspace>/skills/astro-source-literature-review/`
  - or `<your-workspace>/.agent/skills/astro-source-literature-review/`
- **User-global (available across workspaces)**:
  - macOS / Linux: `~/.gemini/config/skills/astro-source-literature-review/`
  - Windows: `%USERPROFILE%\.gemini\config\skills\astro-source-literature-review\`

---

## Prerequisites & Setup

### 1. Python Environment & `uv`

The CLI (`scripts/astro_lit_cli.py`) uses Python 3 standard library modules only. [`uv`](https://docs.astral.sh/uv/) (Astral's Python package and environment manager) is required on `PATH` to manage the workspace-local `.venv` and invoke the CLI.

For PDF full-text extraction (used only when arXiv HTML is unavailable and system `pdftotext` from Poppler is not installed), install `pypdf` into `.venv` inside `<your-workspace>`:

- **Python >= 3.10**: `pypdf` (any recent release).
- **Python 3.9** (e.g., default `/usr/bin/python3` on macOS): pin `"pypdf<7"`, because `pypdf >= 7.0.0` requires Python `>= 3.10`.

Run these commands from `<your-workspace>` (or let the agent guide setup during its initial `check-env` pre-flight check):

**macOS / Linux:**
```bash
uv venv --no-managed-python --python /usr/bin/python3 .venv || uv venv --no-managed-python --python python3 .venv
uv pip install --python .venv/bin/python "pypdf<7"   # use `pypdf` if .venv uses Python >= 3.10
```

**Windows (PowerShell):**
```powershell
uv venv --no-managed-python --python python .venv
uv pip install --python .venv\Scripts\python.exe pypdf
```

(`--no-managed-python` keeps `uv` from probing restricted Conda or external Python directories in sandboxed environments.)

### 2. SciXplorer / ADS API Token

You need a free account and personal API token from [SciXplorer](https://scixplorer.org/) or [NASA ADS](https://ui.adsabs.harvard.edu/user/settings/token). Configure it using **one** of the following methods (listed in lookup priority order):

1. **Environment variable** (`SCIX_API_TOKEN` or `ADS_DEV_KEY`):
   - macOS / Linux: `export SCIX_API_TOKEN="your_token_here"`
   - Windows PowerShell: `$env:SCIX_API_TOKEN = "your_token_here"`
2. **User/project `.env` file** (`~/.env` or `<your-workspace>/.env`):
   ```env
   SCIX_API_TOKEN=your_token_here
   ```
3. **Token file fallback** (`<your-workspace>/scix_token.txt` or `scix_token.txt` inside the skill folder, gitignored):
   Paste **only the raw token string** on a single line (do **not** include a `SCIX_API_TOKEN=` prefix).

Never commit API tokens to version control or print them in logs.

---

## Quick Usage Examples

Once the skill is installed in your workspace or global skills directory, start a chat in `<your-workspace>` and ask the agent to review a source (or invoke the skill directly via `/astro-source-literature-review` if your client uses slash-command triggers). Provide ICRS `RA`, `Dec`, a search radius, and a research focus.

### Example 1: Extragalactic / AGN & X-ray Study

> Review the literature for **NGC 1068** around `RA = 02:42:40.71`, `Dec = -00:00:47.8` (radius `15 arcsec`). Focus on **Chandra/XMM-Newton X-ray reflection spectroscopy, ALMA molecular torus kinematics, and VLBI nuclear disk geometry**.

### Example 2: Galactic Star-Forming Region

> Perform a literature review for the massive star-forming core at `RA = 18:53:18.0`, `Dec = +01:25:25.0` within a `10 arcsec` radius, focusing on **multi-scale magnetic field measurements (dust polarization / DCF), outflows, and dense core fragmentation**.

### Input Checklist

| Parameter | Format / Guidance |
| :--- | :--- |
| **RA / Dec (ICRS)** | Sexagesimal (`"18:53:18.0"`, `"+01:25:25.0"`) or decimal degrees |
| **Search Radius** | Arcseconds (e.g., `5`–`30"` for compact cores/nuclei; larger for extended complexes) |
| **Research Focus** | Wavelengths, tracers, physical quantities, or open questions to prioritize in triage and Layer 2 columns |

---

## Workflow Overview

The agent combines deterministic CLI database/indexing steps (`check-env` pre-flight verification plus Stages 1–4) with LLM-assisted triage, note-taking, and synthesis:

1. **Pre-flight & Stage 1 — Target Resolution (`check-env`, `simbad-query`)**: Verifies `.venv`, `uv`, PDF tools, and `SCIX_API_TOKEN`, then searches SIMBAD around `(RA, Dec)` and prints candidate objects. If multiple SIMBAD OIDs match, you choose whether to isolate one OID or merge sub-sources.
2. **Stage 2 — Metadata Retrieval & Triage (`scix-fetch`, `scix-triage`, `scix-filter`, `init-notes`)**: Fetches titles, years, citations, abstracts, and arXiv IDs from SciXplorer; optionally generates a compact triage table (`scix-triage`); scores abstracts against your research focus; and writes `classification.json` and `notes_index.json`.
3. **Stage 3 — Retrieval & Isolated Note-Taking (`arxiv-download`, `paper-grep`, `verify-notes`)**: Downloads Tier A open-access papers (`--format auto` prefers arXiv HTML and falls back to PDF), extracts section text, aliases, and observatory project codes, writes `<slug>_note.md` per paper (citing `Sec. X.Y` / `Table T`), and validates note completion.
4. **Stage 4 — Two-Layer Synthesis & BibTeX (`scix-bibtex`)**: Synthesizes `./<source_slug>_literature_review.md` (where `<source_slug>` is derived from the primary SIMBAD `main_id`) and exports `./<source_slug>_literature_review.bib`.

---

## Generated Output Layout

All working files stay inside the current research workspace:

```text
<your-workspace>/
├── <source_slug>_literature_review.md          # Two-layer English report with [[...|Author et al. (Year)]] wikilinks
├── <source_slug>_literature_review.bib         # BibTeX entries for cited references
├── .licenses/
│   └── literature_search_arxiv_LICENSE.txt     # Recorded arXiv API Terms of Use notice
└── literature_review/
    ├── env.json                                # Pre-flight environment & token check
    ├── simbad.json                             # SIMBAD OIDs, aliases, and bibcodes
    ├── scix_papers.json                        # SciXplorer metadata & abstracts
    ├── candidates.json                         # Keyword-ranked candidates
    ├── classification.json                     # Tier A / B / C assignments
    ├── arxiv_downloads.json                    # arXiv download manifest and status
    ├── notes_index.json                        # Index mapping bibcodes to <slug>_note.md
    ├── Tier_B_catalog.md                       # Aggregate background reference catalog
    ├── Tier_C_catalog.md                       # Aggregate survey/catalog mentions
    └── papers/
        └── Tier_A/
            ├── <slug>.html / .pdf              # Downloaded arXiv full text
            ├── <slug>.txt                      # Extracted plain text
            ├── <slug>_matches.json             # Alias matches, project codes, key sections
            └── <slug>_note.md                  # Structured inverted-pyramid note
```

To browse clickable `[[<slug>_note|Author et al. (Year)]]` links, open `<your-workspace>` as a vault in Obsidian (optional; all `.md` files are standard Markdown).

---

## Known Limitations & Operational Notes

- **SIMBAD & SciXplorer Limits**: Both `simbad-query` and `scix-fetch` require an explicit `--limit` (the workflow recommends `100` for SIMBAD objects and `500` for SciXplorer papers). Famous, heavily cited targets (e.g., M87, Sgr A*, NGC 1068) may exceed these caps or hit SIMBAD TAP overflow limits; if the agent reports a truncation warning, ask it in chat to re-run with a higher limit. Sources with zero SIMBAD catalog entries (e.g., uncataloged recent transients) are not resolved automatically by `simbad-query`.
- **Non-arXiv & Paywalled Papers**: Papers without an arXiv identifier (or where arXiv retrieval fails) receive an `[Abstract Only]` stub note generated from SciXplorer metadata rather than full-text extraction. Missing observational parameters are left unpopulated rather than guessed.
- **API Rate Limits & Daily Quotas**:
  - **SciXplorer**: 5,000 requests/day quota; CLI enforces `2 req/s` across processes.
  - **SIMBAD TAP**: Enforces `1 req/s` across mirrors.
  - **arXiv**: Enforces `1 req/3s` (`1 req / 3s`) per arXiv Terms of Use; `--format auto` fetches HTML first and skips PDF download when HTML succeeds to halve arXiv traffic.
- **Cross-Process Lock Files**: Rate-limit locks are stored under `./literature_review/.locks/` in the workspace (falling back to the system temp directory only if workspace lock creation fails).

---

## Acknowledgments & License

- **Data & Literature Services**: Uses public astronomical databases and APIs provided by [SIMBAD (CDS, Strasbourg)](https://simbad.cds.unistra.fr/simbad/), [SciXplorer / NASA ADS](https://scixplorer.org/), and [arXiv](https://arxiv.org/) (subject to [arXiv API Terms of Use](https://info.arxiv.org/help/api/index.html)).
- **Author**: Wei-An Chen (Jodrell Bank Centre for Astrophysics, The University of Manchester, Manchester)
- **License**: [MIT License](./LICENSE)
