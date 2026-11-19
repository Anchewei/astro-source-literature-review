#!/usr/bin/env python3
"""Unit tests for astro_lit_cli.py (TDD)."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import astro_lit_cli as cli  # noqa: E402


class TestSimbadMirrorAndSchema(unittest.TestCase):
  """Tests SIMBAD TAP mirror fallback and has_ref/ref ADQL schema."""

  def test_mirror_fallback_on_404(self):
    client = cli.SimbadTapClient()
    calls = []

    def fake_request(url, **kwargs):
      calls.append(url)
      if "unistra.fr" in url:
        raise RuntimeError("HTTP 404 error from Strasbourg")
      return json.dumps({"metadata": [], "data": []})

    with mock.patch.object(client, "_request", side_effect=fake_request):
      res = client.query_adql("SELECT 1")

    self.assertEqual(res, {"metadata": [], "data": []})
    self.assertEqual(len(calls), 2)
    self.assertIn("cfa.harvard.edu", calls[1])
    self.assertEqual(client._active_url, calls[1])

  def test_simbad_query_uses_has_ref_join_ref_and_prints_summary(self):
    adqls = []

    def fake_query_adql(adql):
      adqls.append(adql)
      if "FROM basic" in adql:
        return {
            "metadata": [
                {"name": "oid"},
                {"name": "main_id"},
                {"name": "otype"},
                {"name": "ra"},
                {"name": "dec"},
                {"name": "sep_arcsec"},
            ],
            "data": [[12345, "[RJC2005] G034.43+00.24 MM 1", "YSO", 283.3, 1.4, 1.23]],
        }
      if "FROM ident" in adql:
        return {
            "metadata": [{"name": "oidref"}, {"name": "id"}],
            "data": [[12345, "AGAL G034.411+00.234"]],
        }
      if "FROM has_ref" in adql:
        return {
            "metadata": [{"name": "oidref"}, {"name": "bibcode"}],
            "data": [[12345, "2020ApJ...900..100A"]],
        }
      raise AssertionError(f"Unexpected ADQL: {adql}")

    with tempfile.TemporaryDirectory() as tmpdir:
      out_path = Path(tmpdir) / "simbad.json"
      args = mock.Mock(
          ra="18:53:18.0",
          dec="01:25:25.0",
          radius_arcsec=10.0,
          limit=10,
          output=str(out_path),
      )
      with mock.patch.object(cli.SimbadTapClient, "query_adql", side_effect=fake_query_adql):
        with mock.patch("sys.stdout") as fake_stdout:
          cli.cmd_simbad_query(args)
          stdout_text = "".join(
              call.args[0] for call in fake_stdout.write.call_args_list
          )

      self.assertTrue(any("has_ref" in q and "JOIN ref" in q for q in adqls))
      self.assertFalse(any("FROM bibref" in q for q in adqls))
      self.assertIn("12345", stdout_text)
      self.assertIn("[RJC2005] G034.43+00.24 MM 1", stdout_text)
      data = json.loads(out_path.read_text(encoding="utf-8"))
      self.assertEqual(data["objects"][0]["bibcode_count"], 1)


class TestSciXFilter(unittest.TestCase):
  """Tests scix-filter keyword scoring and abstract extraction."""

  def test_scix_filter_ranks_by_keywords_and_respects_limit(self):
    papers = {
        "papers": [
            {
                "bibcode": "2026A&A...700....1X",
                "year": "2026",
                "citation_count": 1,
                "doctype": "article",
                "arxiv_id": "2601.00001",
                "title": "Sulfur chemistry in diffuse clouds",
                "abstract": "We study CS and SO abundances.",
            },
            {
                "bibcode": "2014ApJ...792..116Z",
                "year": "2014",
                "citation_count": 120,
                "doctype": "article",
                "arxiv_id": "1407.0001",
                "title": "Magnetic field and outflow in G34.43+0.24 MM1",
                "abstract": "Star formation regulated by magnetic field and bipolar outflow.",
            },
            {
                "bibcode": "2019ApJ...880...50K",
                "year": "2019",
                "citation_count": 45,
                "doctype": "article",
                "arxiv_id": "1906.0002",
                "title": "Outflow kinematics in massive star formation",
                "abstract": "CO outflow observations.",
            },
        ]
    }
    with tempfile.TemporaryDirectory() as tmpdir:
      in_path = Path(tmpdir) / "scix.json"
      out_path = Path(tmpdir) / "filtered.json"
      in_path.write_text(json.dumps(papers), encoding="utf-8")

      parser = cli.build_parser()
      args = parser.parse_args([
          "scix-filter",
          "--input", str(in_path),
          "--keywords", "star formation,magnetic field,outflow",
          "--limit", "2",
          "--output", str(out_path),
      ])
      args.func(args)

      res = json.loads(out_path.read_text(encoding="utf-8"))
      self.assertEqual(len(res["papers"]), 2)
      self.assertEqual(res["papers"][0]["bibcode"], "2014ApJ...792..116Z")
      self.assertGreater(res["papers"][0]["keyword_score"], res["papers"][1]["keyword_score"])
      self.assertIn("abstract", res["papers"][0])


class TestPaperGrepEnhancements(unittest.TestCase):
  """Tests alias expansion and section/table extraction in paper-grep."""

  def test_expand_simbad_aliases(self):
    expanded = cli.expand_simbad_aliases(["[RJC2005] G034.43+00.24 MM 1"])
    self.assertIn("G034.43+00.24", expanded)
    self.assertIn("G34.43+00.24", expanded)
    self.assertIn("MM1", expanded)

  def test_paper_grep_extracts_expanded_aliases_and_sections(self):
    html_text = (
        "<html><body>"
        "<h2>1. Introduction</h2><p>Background on star formation.</p>"
        "<h2>3. Results</h2><p>In G34.43+00.24, the MM1 core shows strong outflow.</p>"
        "<table><tr><th>Source</th><th>B_pos</th></tr>"
        "<tr><td>MM1</td><td>1.2 mG</td></tr></table>"
        "<h2>5. Conclusions</h2><p>Magnetic field dominates over turbulence.</p>"
        "</body></html>"
    )
    simbad_data = {
        "objects": [{
            "main_id": "[RJC2005] G034.43+00.24 MM 1",
            "aliases": [],
        }]
    }
    with tempfile.TemporaryDirectory() as tmpdir:
      html_path = Path(tmpdir) / "paper.html"
      sim_path = Path(tmpdir) / "simbad.json"
      out_path = Path(tmpdir) / "grep.json"
      html_path.write_text(html_text, encoding="utf-8")
      sim_path.write_text(json.dumps(simbad_data), encoding="utf-8")

      parser = cli.build_parser()
      args = parser.parse_args([
          "paper-grep",
          "--file", str(html_path),
          "--simbad", str(sim_path),
          "--include-sections",
          "--output", str(out_path),
      ])
      args.func(args)

      res = json.loads(out_path.read_text(encoding="utf-8"))
      self.assertGreaterEqual(res["match_count"], 1)
      self.assertIn("sections", res)
      self.assertTrue(any("Conclusions" in s["heading"] for s in res["sections"]))

  def test_paper_grep_extracts_project_codes_and_data_sections(self):
    sample_text = (
        "Data from ALMA project 2019.1.00685.S, 2013.1.00879.S, "
        "and JCMT program M16AL004 (proposal ID 21A-123)."
    )
    codes = cli.extract_project_codes(sample_text)
    self.assertIn("2019.1.00685.S", codes)
    self.assertIn("2013.1.00879.S", codes)
    self.assertIn("M16AL004", codes)
    self.assertIn("21A-123", codes)

    html_text = (
        "<html><body>"
        "<h2>2. Data Acquisition</h2><p>ALMA project 2019.1.00685.S data.</p>"
        "<h2>III. Analysis and Results</h2><p>Core G34.43+0.24 MM1 mass is 100 Msun.</p>"
        "</body></html>"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
      html_path = Path(tmpdir) / "paper.html"
      out_path = Path(tmpdir) / "grep.json"
      html_path.write_text(html_text, encoding="utf-8")

      parser = cli.build_parser()
      args = parser.parse_args([
          "paper-grep",
          "--file", str(html_path),
          "--aliases", "G34.43+0.24",
          "--include-sections",
          "--output", str(out_path),
      ])
      args.func(args)

      res = json.loads(out_path.read_text(encoding="utf-8"))
      self.assertIn("2019.1.00685.S", res["project_codes"])
      headings = [s["heading"] for s in res["sections"]]
      self.assertIn("Data Acquisition", headings)
      self.assertIn("Analysis and Results", headings)


class TestArxivDownloadAndLocalEnv(unittest.TestCase):
  """Tests project-directory arXiv PDF download and local .venv env check."""

  def test_arxiv_download_pdf_to_project_dir(self):
    orig_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
      try:
        os.chdir(tmpdir)
        parser = cli.build_parser()
        args = parser.parse_args([
            "arxiv-download",
            "--id", "2501.11634",
            "--format", "pdf",
            "--output-dir", "./literature_review/papers",
            "--output", "./literature_review/arxiv_downloads.json",
        ])
        with mock.patch.object(
            cli.ArxivClient,
            "fetch_bytes",
            return_value=b"%PDF-1.4 mock pdf content",
        ):
          args.func(args)

        pdf_file = Path("./literature_review/papers/2501.11634.pdf")
        lic_file = Path("./.licenses/literature_search_arxiv_LICENSE.txt")
        manifest_file = Path("./literature_review/arxiv_downloads.json")
        self.assertTrue(pdf_file.exists())
        self.assertTrue(lic_file.exists())
        self.assertTrue(manifest_file.exists())
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest["papers"][0]["pdf_path"], "literature_review/papers/2501.11634.pdf")
      finally:
        os.chdir(orig_cwd)

  def test_check_env_reports_local_venv_and_pypdf(self):
    orig_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
      try:
        os.chdir(tmpdir)
        venv_python = Path(".venv/bin/python")
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("#!/bin/sh\n", encoding="utf-8")

        out_path = Path("./literature_review/env.json")
        parser = cli.build_parser()
        args = parser.parse_args(["check-env", "--output", str(out_path)])
        args.func(args)

        status = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertTrue(status["local_venv_available"])
        self.assertIn("pypdf_available", status)
        self.assertEqual(status["local_venv_python"], ".venv/bin/python")
      finally:
        os.chdir(orig_cwd)

  def test_arxiv_download_continues_on_single_failure_and_writes_manifest(self):
    orig_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
      try:
        os.chdir(tmpdir)
        parser = cli.build_parser()
        args = parser.parse_args([
            "arxiv-download",
            "--id", "2501.11634,9999.99999",
            "--format", "pdf",
            "--output-dir", "./literature_review/papers",
            "--output", "./literature_review/arxiv_downloads.json",
        ])

        def fake_fetch(url, **kwargs):
          if "9999.99999" in url:
            raise RuntimeError("HTTP 404 Not Found")
          return b"%PDF-1.4 mock pdf content"

        with mock.patch.object(cli.ArxivClient, "fetch_bytes", side_effect=fake_fetch):
          args.func(args)

        manifest = json.loads(
            Path("./literature_review/arxiv_downloads.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["requested_count"], 2)
        self.assertEqual(manifest["downloaded_count"], 1)
        self.assertEqual(manifest["papers"][0]["status"], "ok")
        self.assertEqual(manifest["papers"][1]["status"], "failed")
        self.assertTrue(manifest["papers"][1]["errors"])
      finally:
        os.chdir(orig_cwd)

  def test_paper_grep_html_table_cells_and_compound_headings(self):
    html_text = (
        "<html><body>"
        "<h2>II. ALMA Observations</h2><p>Observed at 1.3 mm.</p>"
        "<h2>3. Results and Discussion</h2>"
        "<p>Table 1 shows the core properties.</p>"
        "<table><tr><td>G34.43 MM1</td><td>18:53:18.0</td><td>1.2 mG</td></tr></table>"
        "<h2>4. Summary and Conclusions</h2><p>Done.</p>"
        "</body></html>"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
      html_path = Path(tmpdir) / "paper.html"
      html_path.write_text(html_text, encoding="utf-8")
      extracted = cli._extract_text_from_file(str(html_path))
      self.assertIn("G34.43 MM1 | 18:53:18.0 | 1.2 mG", extracted)
      sections = cli._extract_key_sections(extracted)
      headings = [s["heading"] for s in sections]
      self.assertIn("ALMA Observations", headings)
      self.assertIn("Results and Discussion", headings)
      self.assertIn("Summary and Conclusions", headings)
      res_sec = next(s for s in sections if s["heading"] == "Results and Discussion")
      self.assertIn("1.2 mG", res_sec["text"])

  def test_scix_filter_handles_null_citation_count_and_word_boundaries(self):
    papers = {
        "papers": [
            {
                "bibcode": "2020ApJ...900....1A",
                "year": "2020",
                "citation_count": None,
                "title": "Dense core kinematics",
                "abstract": "No carbon monoxide here.",
            },
            {
                "bibcode": "2021ApJ...910....2B",
                "year": "2021",
                "citation_count": 5,
                "title": "CO outflow in star-formation region",
                "abstract": "CO (2-1) trace.",
            },
        ]
    }
    with tempfile.TemporaryDirectory() as tmpdir:
      in_path = Path(tmpdir) / "scix.json"
      out_path = Path(tmpdir) / "filtered.json"
      in_path.write_text(json.dumps(papers), encoding="utf-8")
      parser = cli.build_parser()
      args = parser.parse_args([
          "scix-filter",
          "--input", str(in_path),
          "--keywords", "co,star formation",
          "--limit", "5",
          "--output", str(out_path),
      ])
      args.func(args)
      res = json.loads(out_path.read_text(encoding="utf-8"))
      self.assertEqual(len(res["papers"]), 1)
      self.assertEqual(res["papers"][0]["bibcode"], "2021ApJ...910....2B")


class TestTieredNotesAndSlug(unittest.TestCase):
  """Tests deterministic slug generation, init-notes, and Tier B/C catalogs."""

  def test_make_slug_is_deterministic_and_path_safe(self):
    s1 = cli.make_slug(["Pravash, R.", "Bhatt, H."], "2025", "2501.11634")
    s2 = cli.make_slug(["Müller, S."], "2006", "astro-ph/0601234")
    s3 = cli.make_slug(["Shepherd, D. S."], "2007", "2007ApJ...669..464S")
    for s in (s1, s2, s3):
      self.assertRegex(s, r"^[A-Za-z0-9._-]+$")
      self.assertLessEqual(len(s), 48)
    self.assertTrue(s1.startswith("Pravash_2025_"))
    self.assertNotIn("/", s2)

  def test_init_notes_creates_index_tier_catalogs_and_abstract_stub(self):
    orig_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
      try:
        os.chdir(tmpdir)
        scix_data = {
            "papers": [
                {
                    "bibcode": "2025ApJ...981..128P",
                    "title": "BALLAD-POL III",
                    "authors": ["Pravash, R.", "Soam, A."],
                    "year": "2025",
                    "arxiv_id": "2501.11634",
                    "abstract": "Dust polarization in G34.43.",
                },
                {
                    "bibcode": "2007ApJ...669..464S",
                    "title": "CO outflows in G34.4",
                    "authors": ["Shepherd, D. S."],
                    "year": "2007",
                    "arxiv_id": None,
                    "abstract": "Two perpendicular outflows in MM1.",
                },
                {
                    "bibcode": "2014ApJS..213...13H",
                    "title": "TADPOL Survey",
                    "authors": ["Hull, C. L. H.", "Plambeck, R. L.", "Kwon, W."],
                    "year": "2014",
                    "arxiv_id": "1310.6653",
                    "abstract": "CARMA polarization survey.",
                },
                {
                    "bibcode": "2018MNRAS.473.1059U",
                    "title": "ATLASGAL Catalog",
                    "authors": ["Urquhart, J. S.", "Konig, C."],
                    "year": "2018",
                    "arxiv_id": None,
                    "abstract": "Galactic plane clump catalog.",
                },
            ]
        }
        classification = {
            "2025ApJ...981..128P": "A",
            "2007ApJ...669..464S": "A",
            "2014ApJS..213...13H": "B",
            "2018MNRAS.473.1059U": "C",
        }
        Path("literature_review").mkdir(parents=True, exist_ok=True)
        scix_path = Path("literature_review/scix_papers.json")
        cls_path = Path("literature_review/classification.json")
        idx_path = Path("literature_review/notes_index.json")
        scix_path.write_text(json.dumps(scix_data), encoding="utf-8")
        cls_path.write_text(json.dumps(classification), encoding="utf-8")

        parser = cli.build_parser()
        args = parser.parse_args([
            "init-notes",
            "--scix", str(scix_path),
            "--classification", str(cls_path),
            "--output", str(idx_path),
        ])
        args.func(args)

        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        self.assertEqual(len(idx["tier_a"]), 2)
        p_arxiv = next(x for x in idx["tier_a"] if x["bibcode"] == "2025ApJ...981..128P")
        p_abs = next(x for x in idx["tier_a"] if x["bibcode"] == "2007ApJ...669..464S")
        self.assertEqual(p_arxiv["status"], "pending")
        self.assertEqual(p_abs["status"], "abstract_only")
        self.assertIn("|", p_arxiv["wikilink"])
        self.assertIn("\\|", p_arxiv["wikilink_table"])
        self.assertTrue(p_arxiv["note_path"].startswith("literature_review/papers/Tier_A/"))
        self.assertTrue(Path(p_abs["note_path"]).exists())
        self.assertIn("Abstract Only", Path(p_abs["note_path"]).read_text(encoding="utf-8"))
        self.assertTrue(Path("literature_review/Tier_B_catalog.md").exists())
        self.assertTrue(Path("literature_review/Tier_C_catalog.md").exists())
        self.assertIn("TADPOL Survey", Path("literature_review/Tier_B_catalog.md").read_text(encoding="utf-8"))
        self.assertIn("ATLASGAL Catalog", Path("literature_review/Tier_C_catalog.md").read_text(encoding="utf-8"))
      finally:
        os.chdir(orig_cwd)

  def test_author_edge_cases_and_slug_collisions(self):
    self.assertEqual(cli.format_author_year([""], "2025"), "Unknown (2025)")
    self.assertEqual(cli.format_author_year(["John Smith", "Jane Doe"], "2024"), "Smith & Doe (2024)")
    self.assertEqual(cli.format_author_year(["A|B, C."], "2023"), "AB (2023)")
    s = cli.make_slug([""], "2025", "2025A&A...640A..10S.")
    self.assertFalse(s.endswith(("_", ".", "-")))

  def test_init_notes_tier_list_keys_unclassified_and_verify_notes(self):
    orig_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
      try:
        os.chdir(tmpdir)
        scix_data = {
            "papers": [
                {
                    "bibcode": "2025ApJ...981..128P",
                    "title": "Paper 1",
                    "authors": ["Pravash, R."],
                    "year": "2025",
                    "arxiv_id": "2501.11634",
                    "abstract": "Abs 1.",
                },
                {
                    "bibcode": "2025ApJ...981..999P",
                    "title": "Paper 2",
                    "authors": ["Pravash, R."],
                    "year": "2025",
                    "arxiv_id": None,
                    "abstract": "Abs 2.",
                },
            ]
        }
        classification = {"Tier_A": ["2025ApJ...981..128P", "9999UNKNOWN"]}
        Path("literature_review").mkdir(parents=True, exist_ok=True)
        Path("literature_review/scix_papers.json").write_text(json.dumps(scix_data), encoding="utf-8")
        Path("literature_review/classification.json").write_text(json.dumps(classification), encoding="utf-8")

        parser = cli.build_parser()
        args = parser.parse_args([
            "init-notes",
            "--scix", "literature_review/scix_papers.json",
            "--classification", "literature_review/classification.json",
            "--output", "literature_review/notes_index.json",
        ])
        args.func(args)
        idx = json.loads(Path("literature_review/notes_index.json").read_text(encoding="utf-8"))
        self.assertIn("2025ApJ...981..999P", idx["unclassified_bibcodes"])
        self.assertIn("9999UNKNOWN", idx["unknown_bibcodes"])

        # Simulate subagent writing note file, then run verify-notes
        note_path = Path(idx["tier_a"][0]["note_path"])
        note_path.write_text("# Note\nCompleted.", encoding="utf-8")
        v_args = parser.parse_args([
            "verify-notes",
            "--notes-index", "literature_review/notes_index.json",
            "--output", "literature_review/notes_index.json",
        ])
        v_args.func(v_args)
        idx2 = json.loads(Path("literature_review/notes_index.json").read_text(encoding="utf-8"))
        self.assertEqual(idx2["tier_a"][0]["status"], "completed")
      finally:
        os.chdir(orig_cwd)

  def test_arxiv_download_syncs_notes_index_on_html_and_failure(self):
    orig_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
      try:
        os.chdir(tmpdir)
        scix_data = {
            "papers": [
                {
                    "bibcode": "2025ApJ...981..128P",
                    "title": "Paper 1",
                    "authors": ["Pravash, R."],
                    "year": "2025",
                    "arxiv_id": "2501.11634",
                    "abstract": "Abs 1.",
                },
                {
                    "bibcode": "2024ApJ...970..001X",
                    "title": "Paper 2",
                    "authors": ["Smith, J."],
                    "year": "2024",
                    "arxiv_id": "2401.99999",
                    "abstract": "Abs 2 fallback.",
                },
            ]
        }
        classification = {"2025ApJ...981..128P": "A", "2024ApJ...970..001X": "A"}
        Path("literature_review").mkdir(parents=True, exist_ok=True)
        Path("literature_review/scix_papers.json").write_text(json.dumps(scix_data), encoding="utf-8")
        Path("literature_review/classification.json").write_text(json.dumps(classification), encoding="utf-8")

        parser = cli.build_parser()
        parser.parse_args([
            "init-notes",
            "--scix", "literature_review/scix_papers.json",
            "--classification", "literature_review/classification.json",
            "--output", "literature_review/notes_index.json",
        ]).func(parser.parse_args([
            "init-notes",
            "--scix", "literature_review/scix_papers.json",
            "--classification", "literature_review/classification.json",
            "--output", "literature_review/notes_index.json",
        ]))

        def fake_fetch(url, **kwargs):
          if "2501.11634" in url and "/html/" in url:
            return b"<html><body><h1>Title</h1></body></html>"
          raise RuntimeError("HTTP 404 Not Found")

        dl_args = parser.parse_args([
            "arxiv-download",
            "--format", "both",
            "--output-dir", "./literature_review/papers/Tier_A",
            "--notes-index", "./literature_review/notes_index.json",
            "--scix", "./literature_review/scix_papers.json",
            "--output", "./literature_review/arxiv_downloads.json",
        ])
        with mock.patch.object(cli.ArxivClient, "fetch_bytes", side_effect=fake_fetch):
          dl_args.func(dl_args)

        idx = json.loads(Path("literature_review/notes_index.json").read_text(encoding="utf-8"))
        e1 = next(x for x in idx["tier_a"] if x["bibcode"] == "2025ApJ...981..128P")
        e2 = next(x for x in idx["tier_a"] if x["bibcode"] == "2024ApJ...970..001X")
        self.assertTrue(e1["paper_path"].endswith(".html"))
        self.assertEqual(e2["status"], "abstract_only")
        self.assertTrue(Path(e2["note_path"]).exists())
      finally:
        os.chdir(orig_cwd)


class TestHtmlMathMlAndExtractionGeneralizability(unittest.TestCase):
  """Tests SIMBAD limit warning, HTML/MathML extraction, multi-wavelength project codes, aliases, and rate locks."""

  def test_simbad_query_warns_when_limit_reached(self):
    def fake_query_adql(adql):
      if "FROM basic" in adql:
        return {
            "metadata": [
                {"name": "oid"}, {"name": "main_id"}, {"name": "otype"},
                {"name": "ra"}, {"name": "dec"}, {"name": "sep_arcsec"},
            ],
            "data": [
                [1, "Obj1", "Y*O", 283.3, 1.4, 0.5],
                [2, "Obj2", "cor", 283.3, 1.4, 1.2],
            ],
        }
      return {"metadata": [{"name": "oidref"}, {"name": "id"}], "data": []}

    with tempfile.TemporaryDirectory() as tmpdir:
      out_path = Path(tmpdir) / "simbad.json"
      args = mock.Mock(
          ra="18:53:18.0", dec="01:25:25.0", radius_arcsec=10.0,
          limit=2, output=str(out_path),
      )
      with mock.patch.object(cli.SimbadTapClient, "query_adql", side_effect=fake_query_adql):
        cli.cmd_simbad_query(args)
      data = json.loads(out_path.read_text(encoding="utf-8"))
      self.assertTrue(data["limit_reached_warning"])

  def test_html_extractor_skips_mathml_annotation_svg_and_preserves_scripts(self):
    html = (
        "<html><body><p>RMS noise is "
        "<math><mn>8</mn><annotation encoding=\"application/x-tex\">8</annotation></math>"
        " mJy and column density is "
        "<math><msup><mn>10</mn><mn>22</mn></msup></math>"
        " cm^-2 with "
        "<math><msub><mi>N</mi><mi>H</mi></msub></math>."
        "<svg><text>SVG_NOISE</text></svg>"
        "</p></body></html>"
    )
    parser = cli._HTMLTextExtractor()
    parser.feed(html)
    txt = parser.get_text()
    self.assertIn("8", txt)
    self.assertNotIn("88", txt)
    self.assertIn("10^22", txt)
    self.assertNotIn("1022", txt)
    self.assertIn("N_H", txt)
    self.assertNotIn("SVG_NOISE", txt)

  def test_extract_project_codes_multi_observatory_and_ignores_grants(self):
    text = (
        "We use archival ALMA data 2015.1.00369, VLA 19A-123, JWST GO-12345, "
        "Chandra ObsID 0821240101, ESO 0104.C-0123(A), and NOEMA S20AB.\n"
        "This analysis was supported by Shanghai Pujiang Program 20PJ1415500.\n"
        "Acknowledgements\n"
        "Supported by Fondecyt project 1180350 and JWST PID 4321.\n"
        "Appendix A\n"
        "Additional program 2013.1.00960 and archival project L18B-01.\n"
    )
    codes = cli.extract_project_codes(text)
    for expected in (
        "2015.1.00369",
        "19A-123",
        "JWST GO-12345",
        "ObsID 0821240101",
        "0104.C-0123(A)",
        "S20AB",
        "PID 4321",
        "2013.1.00960",
        "L18B-01",
    ):
      self.assertIn(expected, codes)
    self.assertNotIn("1180350", codes)
    self.assertNotIn("20PJ1415500", codes)

  def test_expand_aliases_and_regex_multi_wavelength(self):
    aliases = cli.expand_simbad_aliases(["NAME M 87", "NGC 1068", "Abell 2744"])
    for expected in ("M 87", "M87", "NGC 1068", "NGC1068", "Abell 2744", "A2744"):
      self.assertIn(expected, aliases)
    pat = cli._compile_alias_regex(["M 87"])
    self.assertIsNotNone(pat.search("Jet in M 87."))
    self.assertIsNone(pat.search("Catalog entry AM 870."))

  def test_extract_key_sections_ignores_table_captions_and_multiword_obs(self):
    text = (
        "2.1. Chandra X-ray Observations\n"
        "Chandra observed the source for 50 ks.\n"
        "3. Results\n"
        "Table 1: Source fluxes\n"
        "We measure a luminosity of 1e42 erg/s.\n"
    )
    sections = cli._extract_key_sections(text)
    headings = [s["heading"] for s in sections]
    self.assertIn("Chandra X-ray Observations", headings)
    results = next(s for s in sections if s["heading"] == "Results")
    self.assertIn("1e42 erg/s", results["text"])

  def test_rate_limited_client_lock_path_avoids_tmp_by_default(self):
    client = cli.SimbadTapClient()
    self.assertEqual(client.lock_file.parent, Path("literature_review") / ".locks")

  def test_arxiv_download_auto_skips_pdf_when_html_succeeds(self):
    with tempfile.TemporaryDirectory() as tmpdir:
      orig_cwd = os.getcwd()
      os.chdir(tmpdir)
      try:
        parser = cli.build_parser()
        out_path = Path("literature_review/arxiv_downloads.json")
        dl_args = parser.parse_args([
            "arxiv-download",
            "--id", "2501.11634,2401.00001",
            "--format", "auto",
            "--output-dir", "./literature_review/papers",
            "--output", str(out_path),
        ])
        called_urls = []

        def fake_fetch(url, **kwargs):
          called_urls.append(url)
          if "2501.11634" in url and "/html/" in url:
            return b"<html><body><h1>Title</h1></body></html>"
          if "2401.00001" in url and "/html/" in url:
            raise RuntimeError("HTTP 404 Not Found")
          if "2401.00001" in url and "/pdf/" in url:
            return b"%PDF-1.4 fake"
          raise AssertionError(f"Unexpected URL fetch: {url}")

        with mock.patch.object(cli.ArxivClient, "fetch_bytes", side_effect=fake_fetch):
          dl_args.func(dl_args)

        self.assertFalse(any("2501.11634" in u and "/pdf/" in u for u in called_urls))
        self.assertTrue(any("2401.00001" in u and "/pdf/" in u for u in called_urls))
      finally:
        os.chdir(orig_cwd)


if __name__ == "__main__":
  unittest.main()
