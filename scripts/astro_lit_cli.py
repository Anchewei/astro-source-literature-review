#!/usr/bin/env python3
"""CLI tool for astro-source-literature-review.

Provides rate-limited access to SIMBAD TAP, the SciXplorer (SciX/ADS) API,
and arXiv paper downloads (PDF/HTML), plus triage summary and paper text/alias
extraction helpers, using Python 3 standard library modules only (optional
`pypdf` in the local `.venv` enables PDF text extraction when `pdftotext` is
absent). All data outputs are written via --output.
"""

import argparse
from datetime import datetime, timezone
try:
  import fcntl
except ImportError:
  fcntl = None
try:
  import msvcrt
except ImportError:
  msvcrt = None
from html.parser import HTMLParser
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOKEN_FILE = SKILL_ROOT / "scix_token.txt"

ARXIV_NEW_RE = re.compile(r"arXiv:(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE)
ARXIV_OLD_RE = re.compile(r"arXiv:([a-z-]+/\d{7}(?:v\d+)?)", re.IGNORECASE)


class RateLimitError(Exception):
  """Raised when an external API rate limit (HTTP 429) is exceeded."""


class _HTMLTextExtractor(HTMLParser):
  """Strips HTML/MathML tags while preserving readable text, math scripts, and table cells."""

  _SKIP_TAGS = frozenset(("script", "style", "noscript", "annotation", "annotation-xml", "svg"))
  _BLOCK_TAGS = frozenset(("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "caption"))
  _END_NEWLINE_TAGS = frozenset(("h1", "h2", "h3", "h4", "h5", "h6", "tr", "figcaption", "caption"))

  def __init__(self):
    super().__init__()
    self._skip_depth = 0
    self._chunks = []
    self._tag_stack = []
    self._script_frames = []

  def handle_starttag(self, tag, attrs):
    t = tag.lower()
    if self._skip_depth == 0 and self._script_frames and len(self._tag_stack) == self._script_frames[-1][2]:
      self._script_frames[-1][1] += 1
      if self._script_frames[-1][1] == 2:
        self._chunks.append("^" if self._script_frames[-1][0] == "msup" else "_")
    self._tag_stack.append(t)
    if t in self._SKIP_TAGS:
      self._skip_depth += 1
    elif self._skip_depth == 0:
      if t in ("msup", "msub"):
        self._script_frames.append([t, 0, len(self._tag_stack)])
      elif t in self._BLOCK_TAGS:
        self._chunks.append("\n")
      elif t in ("td", "th"):
        self._chunks.append(" | ")

  def handle_endtag(self, tag):
    t = tag.lower()
    if self._tag_stack:
      self._tag_stack.pop()
    if self._script_frames and len(self._tag_stack) < self._script_frames[-1][2]:
      self._script_frames.pop()
    if t in self._SKIP_TAGS and self._skip_depth > 0:
      self._skip_depth -= 1
    elif self._skip_depth == 0 and t in self._END_NEWLINE_TAGS:
      self._chunks.append("\n")

  def handle_data(self, data):
    if self._skip_depth == 0:
      self._chunks.append(data)

  def get_text(self):
    text = "".join(self._chunks)
    return re.sub(r"[ \t]+", " ", text)


class RateLimitedClient:
  """Base HTTP client with cross-process file-lock rate limiting and retries."""

  BASE_URL = ""
  REQUESTS_PER_SECOND = 1.0
  LOCK_PREFIX = "astro_lit_rate"

  def __init__(self):
    self.delay = 1.0 / self.REQUESTS_PER_SECOND
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    self.lock_file = Path("literature_review") / ".locks" / f"{self.LOCK_PREFIX}_{uid}.lock"

  def _open_lock_file(self):
    try:
      self.lock_file.parent.mkdir(parents=True, exist_ok=True)
      return open(self.lock_file, "a+", encoding="utf-8")
    except OSError:
      fallback = Path(tempfile.gettempdir()) / self.lock_file.name
      fallback.parent.mkdir(parents=True, exist_ok=True)
      return open(fallback, "a+", encoding="utf-8")

  def _wait_for_rate_limit(self):
    """Blocks across processes until enough time has passed since last call."""
    with self._open_lock_file() as lock_fp:
      if fcntl is not None:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
      elif msvcrt is not None:
        lock_fp.seek(0)
        msvcrt.locking(lock_fp.fileno(), msvcrt.LK_LOCK, 1)
      try:
        lock_fp.seek(0)
        raw = lock_fp.read().strip()
        last_ts = float(raw) if raw else 0.0
        now = time.monotonic()
        elapsed = now - last_ts
        if 0 <= elapsed < self.delay:
          time.sleep(self.delay - elapsed)
        lock_fp.seek(0)
        lock_fp.truncate()
        lock_fp.write(f"{time.monotonic():.6f}")
        lock_fp.flush()
      finally:
        if fcntl is not None:
          fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
          lock_fp.seek(0)
          msvcrt.locking(lock_fp.fileno(), msvcrt.LK_UNLCK, 1)

  def fetch_bytes(
      self,
      url,
      method="GET",
      headers=None,
      body_bytes=None,
      retries=3,
      timeout=30,
  ):
    """Executes an HTTP request with rate limiting and returns raw bytes."""
    req_headers = dict(headers or {})
    for attempt in range(retries):
      self._wait_for_rate_limit()
      req = urllib_request.Request(
          url, data=body_bytes, headers=req_headers, method=method
      )
      try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
          return resp.read()
      except urllib_error.HTTPError as e:
        err_body = ""
        try:
          err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
          err_body = "<unable to read error body>"

        if e.code == 429:
          retry_after = e.headers.get("Retry-After") if e.headers else None
          if retry_after and retry_after.isdigit():
            wait = min(int(retry_after), 60)
          else:
            wait = 2**attempt
          print(
              f"Rate limited (429) on {url}, retrying in {wait}s "
              f"(attempt {attempt + 1}/{retries})...",
              file=sys.stderr,
          )
          if attempt == retries - 1:
            raise RateLimitError(
                f"HTTP 429 Too Many Requests from {url} "
                f"(limit: {self.REQUESTS_PER_SECOND} req/s) after {retries} "
                f"retries. Response: {err_body}"
            ) from e
          time.sleep(wait)
          continue

        if e.code >= 500:
          wait = 2**attempt
          print(
              f"Server error {e.code} on {url}, retrying in {wait}s "
              f"(attempt {attempt + 1}/{retries})...",
              file=sys.stderr,
          )
          if attempt == retries - 1:
            raise RuntimeError(
                f"HTTP {e.code} from {url} after {retries} retries. "
                f"Response: {err_body}"
            ) from e
          time.sleep(wait)
          continue

        raise RuntimeError(
            f"HTTP {e.code} error from {url}: {err_body}"
        ) from e
      except (
          urllib_error.URLError,
          TimeoutError,
          http.client.HTTPException,
          OSError,
      ) as e:
        if attempt == retries - 1:
          raise RuntimeError(
              f"Network/transport error contacting {url}: {e}"
          ) from e
        wait = 2**attempt
        print(
            f"Transient error ({e}) on {url}, retrying in {wait}s "
            f"(attempt {attempt + 1}/{retries})...",
            file=sys.stderr,
        )
        time.sleep(wait)

    raise RuntimeError(f"Request to {url} failed after {retries} attempts.")

  def _request(
      self,
      url,
      method="GET",
      headers=None,
      body_bytes=None,
      retries=3,
      timeout=30,
  ):
    """Executes an HTTP request with rate limiting and returns UTF-8 text."""
    return self.fetch_bytes(
        url,
        method=method,
        headers=headers,
        body_bytes=body_bytes,
        retries=retries,
        timeout=timeout,
    ).decode("utf-8")


def _parse_json_response(raw_text, source_label):
  """Parses JSON text with contextual error reporting on non-JSON replies."""
  try:
    return json.loads(raw_text)
  except json.JSONDecodeError as e:
    preview = raw_text[:300].replace("\n", " ")
    raise RuntimeError(
        f"Invalid JSON response from {source_label}: {e}. "
        f"Response preview: {preview!r}"
    ) from e


class SimbadTapClient(RateLimitedClient):
  """Client for SIMBAD TAP synchronous ADQL queries with mirror fallback."""

  BASE_URL = "https://simbad.cds.unistra.fr/simbad/sim-tap/sync"
  MIRROR_URLS = (
      "https://simbad.cds.unistra.fr/simbad/sim-tap/sync",
      "https://simbad.cfa.harvard.edu/simbad/sim-tap/sync",
      "https://simbad.idia.ac.za/simbad/sim-tap/sync",
  )
  REQUESTS_PER_SECOND = 1.0
  LOCK_PREFIX = "simbad_tap_rate"

  def __init__(self):
    super().__init__()
    env_url = os.environ.get("SIMBAD_TAP_URL", "").strip()
    self._urls = [env_url] if env_url else list(self.MIRROR_URLS)
    self._active_url = self._urls[0]

  def query_adql(self, adql):
    """Runs a synchronous ADQL query and returns parsed JSON, with mirror fallback."""
    form_data = urllib_parse.urlencode({
        "REQUEST": "doQuery",
        "LANG": "ADQL",
        "FORMAT": "json",
        "QUERY": adql,
    }).encode("utf-8")
    candidates = [self._active_url] + [u for u in self._urls if u != self._active_url]
    last_err = None
    for url in candidates:
      try:
        raw = self._request(
            url,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body_bytes=form_data,
            timeout=45,
        )
        parsed = _parse_json_response(raw, f"SIMBAD TAP ({url})")
        self._active_url = url
        return parsed
      except Exception as exc:  # pylint: disable=broad-except
        last_err = exc
        print(
            f"Warning: SIMBAD TAP endpoint {url} failed ({exc}); trying next mirror...",
            file=sys.stderr,
        )
    raise last_err


class SciXplorerClient(RateLimitedClient):
  """Client for SciXplorer (SciX/ADS) API v1."""

  BASE_URL = "https://api.scixplorer.org/v1"
  REQUESTS_PER_SECOND = 2.0
  LOCK_PREFIX = "scixplorer_rate"

  def __init__(self, token):
    super().__init__()
    self._token = token

  def _auth_headers(self, extra=None):
    headers = {
        "Authorization": f"Bearer {self._token}",
        "Accept": "application/json",
    }
    if extra:
      headers.update(extra)
    return headers

  def search_bibcodes(self, bibcodes):
    """Batch-fetches metadata & abstracts for up to 50 bibcodes."""
    quoted = " OR ".join(f'"{b}"' for b in bibcodes)
    params = urllib_parse.urlencode({
        "q": f"bibcode:({quoted})",
        "fl": (
            "bibcode,title,author,year,pub,abstract,citation_count,"
            "doi,identifier,doctype"
        ),
        "rows": str(len(bibcodes)),
    })
    url = f"{self.BASE_URL}/search/query?{params}"
    raw = self._request(url, method="GET", headers=self._auth_headers())
    return _parse_json_response(raw, "SciXplorer /search/query")

  def export_bibtex(self, bibcodes):
    """Exports BibTeX entries for a list of bibcodes."""
    url = f"{self.BASE_URL}/export/bibtex"
    payload = json.dumps({"bibcode": bibcodes}).encode("utf-8")
    raw = self._request(
        url,
        method="POST",
        headers=self._auth_headers({"Content-Type": "application/json"}),
        body_bytes=payload,
    )
    return _parse_json_response(raw, "SciXplorer /export/bibtex")


class ArxivClient(RateLimitedClient):
  """Client for downloading arXiv PDF/HTML files respecting arXiv Terms of Use."""

  BASE_URL = "https://arxiv.org"
  REQUESTS_PER_SECOND = 1.0 / 3.0  # Max 1 request every 3 seconds
  LOCK_PREFIX = "arxiv_rate"

  def download_pdf(self, arxiv_id):
    """Downloads raw PDF bytes for an arXiv ID."""
    clean_id = arxiv_id.strip()
    url = f"{self.BASE_URL}/pdf/{clean_id}.pdf"
    content = self.fetch_bytes(
        url,
        method="GET",
        headers={"User-Agent": "astro-source-literature-review/1.0"},
        timeout=60,
    )
    if not content or not content.lstrip().startswith(b"%PDF"):
      raise RuntimeError(
          f"Downloaded file from {url} is empty or not a valid PDF."
      )
    return content, url

  def download_html(self, arxiv_id):
    """Downloads raw HTML bytes for an arXiv ID and validates non-empty HTML."""
    clean_id = arxiv_id.strip()
    url = f"{self.BASE_URL}/html/{clean_id}"
    content = self.fetch_bytes(
        url,
        method="GET",
        headers={"User-Agent": "astro-source-literature-review/1.0"},
        timeout=60,
    )
    if not content or not re.search(rb"<(?:!doctype|html)\b", content[:2048], re.IGNORECASE):
      raise RuntimeError(
          f"Downloaded response from {url} is empty or not valid HTML."
      )
    return content, url


def parse_ra_deg(value):
  """Parses RA in decimal degrees or sexagesimal (HH:MM:SS.s / HHhMMmSSs)."""
  text = str(value).strip()
  try:
    deg = float(text)
    if not 0.0 <= deg <= 360.0:
      raise ValueError(f"RA in degrees out of [0, 360]: {deg}")
    return deg
  except ValueError:
    pass

  if text.startswith("-"):
    raise ValueError(f"RA cannot be negative: {value}")
  cleaned = re.sub(r"[hmsHMS]", ":", text).rstrip(":")
  parts = [p for p in re.split(r"[:\s]+", cleaned) if p]
  if len(parts) not in (2, 3):
    raise ValueError(f"Unrecognized RA format: {value}")
  h = float(parts[0])
  m = float(parts[1])
  s = float(parts[2]) if len(parts) == 3 else 0.0
  if not (0.0 <= h < 24.0 and 0.0 <= m < 60.0 and 0.0 <= s < 60.0):
    raise ValueError(f"Sexagesimal RA components out of range: {value}")
  deg = (h + m / 60.0 + s / 3600.0) * 15.0
  if not 0.0 <= deg <= 360.0:
    raise ValueError(f"Parsed RA out of [0, 360]: {deg}")
  return deg


def parse_dec_deg(value):
  """Parses Dec in decimal degrees or sexagesimal (+/-DD:MM:SS.s / DDdMMmSSs)."""
  text = str(value).strip()
  try:
    deg = float(text)
    if not -90.0 <= deg <= 90.0:
      raise ValueError(f"Dec in degrees out of [-90, 90]: {deg}")
    return deg
  except ValueError:
    pass

  sign = -1.0 if text.startswith("-") else 1.0
  unsigned = text.lstrip("+-")
  cleaned = re.sub(r"[dmsDMS°′″'\"]", ":", unsigned).rstrip(":")
  parts = [p for p in re.split(r"[:\s]+", cleaned) if p]
  if len(parts) not in (2, 3):
    raise ValueError(f"Unrecognized Dec format: {value}")
  d = float(parts[0])
  m = float(parts[1])
  s = float(parts[2]) if len(parts) == 3 else 0.0
  if not (0.0 <= d <= 90.0 and 0.0 <= m < 60.0 and 0.0 <= s < 60.0):
    raise ValueError(f"Sexagesimal Dec components out of range: {value}")
  deg = sign * (d + m / 60.0 + s / 3600.0)
  if not -90.0 <= deg <= 90.0:
    raise ValueError(f"Parsed Dec out of [-90, 90]: {deg}")
  return deg


def _read_dotenv_token(dotenv_path):
  """Reads SCIX_API_TOKEN or ADS_DEV_KEY from a .env file if present."""
  if not dotenv_path.exists():
    return None, None
  for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, val = line.split("=", 1)
    key = key.strip()
    if key in ("SCIX_API_TOKEN", "ADS_DEV_KEY"):
      cleaned = val.strip().strip("'\"")
      if cleaned:
        return cleaned, f"dotenv:{dotenv_path}:{key}"
  return None, None


def resolve_scix_token(token_file=None):
  """Resolves SciXplorer/ADS token: env vars -> ~/.env / .env -> token_file."""
  for env_name in ("SCIX_API_TOKEN", "ADS_DEV_KEY"):
    val = os.environ.get(env_name, "").strip()
    if val:
      return val, f"env:{env_name}"

  for dotenv_path in (Path.home() / ".env", Path.cwd() / ".env"):
    val, src = _read_dotenv_token(dotenv_path)
    if val:
      return val, src

  candidates = (
      [Path(token_file)]
      if token_file
      else [Path.cwd() / "scix_token.txt", DEFAULT_TOKEN_FILE]
  )
  for path in candidates:
    if path.exists():
      content = path.read_text(encoding="utf-8").strip()
      lines = [
          ln.strip()
          for ln in content.splitlines()
          if ln.strip() and not ln.strip().startswith("#")
      ]
      if lines:
        return lines[0], f"file:{_format_rel_path(path)}"

  return None, None


def find_uv_binary():
  """Finds uv executable in PATH or standard install locations."""
  found = shutil.which("uv")
  if found:
    return found
  candidates = [
      Path.home() / ".local" / "bin" / "uv",
      Path.home() / ".cargo" / "bin" / "uv",
      Path("/opt/homebrew/bin/uv"),
      Path("/usr/local/bin/uv"),
  ]
  for cand in candidates:
    if cand.exists() and os.access(cand, os.X_OK):
      return str(cand)
  return None


def extract_arxiv_id(identifiers):
  """Extracts the first arXiv ID from a SciXplorer identifier list."""
  for item in identifiers or []:
    m = ARXIV_NEW_RE.search(item)
    if m:
      return m.group(1)
    m_old = ARXIV_OLD_RE.search(item)
    if m_old:
      return m_old.group(1)
  return None


def tap_rows_to_dicts(tap_json):
  """Converts SIMBAD TAP JSON output into (rows, overflow_detected)."""
  cols = [col["name"] for col in tap_json.get("metadata", [])]
  rows = [dict(zip(cols, row)) for row in tap_json.get("data", [])]
  status = str(tap_json.get("query_status", "")).upper()
  overflow = status == "OVERFLOW"
  return rows, overflow


def write_json(path, data):
  """Writes formatted JSON to disk, creating parent directories if needed."""
  out_path = Path(path)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  with open(out_path, "w", encoding="utf-8") as fp:
    json.dump(data, fp, indent=2, ensure_ascii=False)


def _validate_positive(name, value):
  if value <= 0:
    raise ValueError(f"--{name} must be > 0 (got {value}).")


def _format_rel_path(path_obj):
  """Returns a path relative to cwd if inside cwd, else POSIX string."""
  resolved = Path(path_obj).resolve()
  cwd = Path.cwd().resolve()
  try:
    return resolved.relative_to(cwd).as_posix()
  except ValueError:
    return str(path_obj)


def cmd_check_env(args):
  """Checks Python/Conda environment, local .venv, uv, pypdf, and API token."""
  token, token_source = resolve_scix_token(args.token_file)
  uv_path = find_uv_binary()
  pdftotext_path = shutil.which("pdftotext")
  conda_env = os.environ.get("CONDA_DEFAULT_ENV")
  local_venv_python = (
      Path(".venv/Scripts/python.exe")
      if os.name == "nt"
      else Path(".venv/bin/python")
  )
  local_venv_available = local_venv_python.exists()
  pypdf_available = importlib.util.find_spec("pypdf") is not None
  if not pypdf_available and local_venv_available:
    try:
      probe = subprocess.run(
          [str(local_venv_python), "-c", "import pypdf"],
          capture_output=True,
          check=False,
      )
      pypdf_available = probe.returncode == 0
    except OSError:
      pypdf_available = False
  token_path = Path(args.token_file) if args.token_file else DEFAULT_TOKEN_FILE

  status = {
      "python_version": sys.version.split()[0],
      "python_executable": sys.executable,
      "conda_default_env": conda_env,
      "is_agent_conda_env": conda_env == "agent",
      "local_venv_available": local_venv_available,
      "local_venv_python": local_venv_python.as_posix() if local_venv_available else None,
      "pypdf_available": pypdf_available,
      "uv_available": uv_path is not None,
      "uv_path": uv_path,
      "pdftotext_available": pdftotext_path is not None,
      "token_configured": token is not None,
      "token_source": token_source,
      "token_file_path": _format_rel_path(token_path),
  }
  write_json(args.output, status)
  print(f"Success! Environment check written to: {args.output}")
  if not status["token_configured"]:
    print(
        f"Warning: SciXplorer token is not configured. Set SCIX_API_TOKEN in "
        f"~/.env or paste your token into {status['token_file_path']}.",
        file=sys.stderr,
    )


def cmd_simbad_query(args):
  """Queries SIMBAD TAP for objects, aliases, and bibcodes in a cone."""
  _validate_positive("radius-arcsec", args.radius_arcsec)
  _validate_positive("limit", args.limit)
  ra_deg = parse_ra_deg(args.ra)
  dec_deg = parse_dec_deg(args.dec)
  radius_deg = args.radius_arcsec / 3600.0

  client = SimbadTapClient()
  basic_adql = f"""
    SELECT TOP {args.limit}
      b.oid,
      b.main_id,
      b.otype,
      b.ra,
      b.dec,
      DISTANCE(POINT('ICRS', b.ra, b.dec), POINT('ICRS', {ra_deg}, {dec_deg})) * 3600.0 AS sep_arcsec
    FROM basic AS b
    WHERE CONTAINS(
      POINT('ICRS', b.ra, b.dec),
      CIRCLE('ICRS', {ra_deg}, {dec_deg}, {radius_deg})
    ) = 1
    ORDER BY sep_arcsec ASC
  """.strip()

  objects, basic_overflow = tap_rows_to_dicts(client.query_adql(basic_adql))
  oids = [str(obj["oid"]) for obj in objects]

  aliases_by_oid = {oid: [] for oid in oids}
  bibs_by_oid = {oid: [] for oid in oids}
  all_bibcodes = set()
  ident_overflow = False
  bib_overflow = False

  # Chunk IN (...) clauses to stay well within TAP query limits.
  for i in range(0, len(oids), 100):
    oid_list = ", ".join(oids[i : i + 100])
    ident_adql = f"SELECT oidref, id FROM ident WHERE oidref IN ({oid_list})"
    ident_rows, ov1 = tap_rows_to_dicts(client.query_adql(ident_adql))
    ident_overflow = ident_overflow or ov1
    for row in ident_rows:
      oid_key = str(row["oidref"])
      if oid_key in aliases_by_oid and row.get("id"):
        aliases_by_oid[oid_key].append(row["id"])

    bib_adql = (
        f"SELECT DISTINCT h.oidref AS oidref, r.bibcode AS bibcode "
        f"FROM has_ref AS h JOIN ref AS r ON h.oidbibref = r.oidbib "
        f"WHERE h.oidref IN ({oid_list})"
    )
    bib_rows, ov2 = tap_rows_to_dicts(client.query_adql(bib_adql))
    bib_overflow = bib_overflow or ov2
    for row in bib_rows:
      oid_key = str(row["oidref"])
      bib = row.get("bibcode")
      if oid_key in bibs_by_oid and bib:
        bibs_by_oid[oid_key].append(bib)
        all_bibcodes.add(bib)

  for obj in objects:
    oid_key = str(obj["oid"])
    obj["aliases"] = sorted(set(aliases_by_oid.get(oid_key, [])))
    obj["bibcodes"] = sorted(set(bibs_by_oid.get(oid_key, [])))
    obj["bibcode_count"] = len(obj["bibcodes"])

  overflow_any = basic_overflow or ident_overflow or bib_overflow
  limit_reached = len(objects) >= args.limit
  if overflow_any:
    print(
        "Warning: SIMBAD TAP reported QUERY_STATUS=OVERFLOW; results may be "
        "truncated by server MAXREC.",
        file=sys.stderr,
    )
  if limit_reached:
    print(
        f"Warning: Returned {len(objects)} object(s) (reached --limit {args.limit}); "
        f"more objects may exist in the search radius. Re-run with a higher --limit.",
        file=sys.stderr,
    )

  result = {
      "query": {
          "ra_input": args.ra,
          "dec_input": args.dec,
          "ra_deg": ra_deg,
          "dec_deg": dec_deg,
          "radius_arcsec": args.radius_arcsec,
          "limit": args.limit,
      },
      "tap_overflow_warning": overflow_any,
      "limit_reached_warning": limit_reached,
      "object_count": len(objects),
      "total_unique_bibcodes": len(all_bibcodes),
      "objects": objects,
      "all_bibcodes": sorted(all_bibcodes),
  }
  write_json(args.output, result)
  print(
      f"Success! Found {len(objects)} SIMBAD object(s) and "
      f"{len(all_bibcodes)} unique bibcode(s). Written to: {args.output}"
  )
  if objects:
    print("\n| OID | Main ID | Type | Sep (\") | Bibcodes |")
    print("| :--- | :--- | :--- | ---: | ---: |")
    for obj in objects:
      sep_val = obj.get("sep_arcsec")
      sep_str = f"{sep_val:.2f}" if isinstance(sep_val, (int, float)) else str(sep_val)
      print(
          f"| {obj.get('oid')} | {obj.get('main_id')} | {obj.get('otype')} | "
          f"{sep_str} | {obj.get('bibcode_count', 0)} |"
      )


def cmd_scix_fetch(args):
  """Fetches metadata and abstracts from SciXplorer for SIMBAD bibcodes."""
  _validate_positive("limit", args.limit)
  token, _ = resolve_scix_token(args.token_file)
  if not token:
    raise RuntimeError(
        f"SciXplorer API token not found. Set SCIX_API_TOKEN in ~/.env or "
        f"paste your token into {args.token_file or _format_rel_path(DEFAULT_TOKEN_FILE)}."
    )

  with open(args.input, "r", encoding="utf-8") as fp:
    simbad_data = json.load(fp)

  selected_oids = set()
  if args.oid is not None:
    selected_oids.add(str(args.oid))
  if args.oids:
    selected_oids.update(o.strip() for o in args.oids.split(",") if o.strip())

  if selected_oids:
    matched = [
        obj for obj in simbad_data.get("objects", [])
        if str(obj.get("oid")) in selected_oids
    ]
    if not matched:
      raise RuntimeError(
          f"None of OIDs {sorted(selected_oids)} found in {args.input}."
      )
    raw_bibcodes = []
    for obj in matched:
      raw_bibcodes.extend(obj.get("bibcodes", []))
  else:
    raw_bibcodes = simbad_data.get("all_bibcodes", [])

  unique_bibcodes = sorted(set(raw_bibcodes), reverse=True)
  total_available = len(unique_bibcodes)
  truncated = total_available > args.limit
  if truncated:
    print(
        f"Warning: {total_available} bibcodes available, capping at "
        f"--limit {args.limit}. Older/classic papers may be excluded.",
        file=sys.stderr,
    )
  bibcodes_to_fetch = unique_bibcodes[: args.limit]

  client = SciXplorerClient(token)
  docs = []
  returned_set = set()
  batch_size = 50
  for i in range(0, len(bibcodes_to_fetch), batch_size):
    chunk = bibcodes_to_fetch[i : i + batch_size]
    resp = client.search_bibcodes(chunk)
    for doc in resp.get("response", {}).get("docs", []):
      bib = doc.get("bibcode")
      if bib:
        returned_set.add(bib)
      docs.append({
          "bibcode": bib,
          "title": (doc.get("title") or [""])[0],
          "authors": doc.get("author") or [],
          "year": doc.get("year"),
          "pub": doc.get("pub"),
          "doctype": doc.get("doctype"),
          "citation_count": doc.get("citation_count") or 0,
          "doi": (doc.get("doi") or [None])[0],
          "arxiv_id": extract_arxiv_id(doc.get("identifier")),
          "abstract": doc.get("abstract") or "",
      })

  missing_bibcodes = sorted(set(bibcodes_to_fetch) - returned_set)
  docs.sort(
      key=lambda d: (str(d.get("year") or ""), d.get("citation_count") or 0),
      reverse=True,
  )
  output = {
      "total_available_bibcodes": total_available,
      "requested_bibcodes": len(bibcodes_to_fetch),
      "returned_papers": len(docs),
      "truncated": truncated,
      "missing_bibcodes": missing_bibcodes,
      "papers": docs,
  }
  write_json(args.output, output)
  print(
      f"Success! Fetched {len(docs)}/{len(bibcodes_to_fetch)} paper(s) from "
      f"SciXplorer. Written to: {args.output}"
  )


def cmd_scix_triage(args):
  """Writes a compact Markdown summary table of scix-fetch papers for triage."""
  _validate_positive("limit", args.limit)
  with open(args.input, "r", encoding="utf-8") as fp:
    data = json.load(fp)

  papers = data.get("papers", [])[: args.limit]
  lines = [
      "| # | Bibcode | Year | Cites | Doctype | arXiv ID | Title |",
      "| :--- | :--- | :--- | ---: | :--- | :--- | :--- |",
  ]
  for idx, p in enumerate(papers, start=1):
    title = (p.get("title") or "").replace("|", "/").replace("\n", " ")
    lines.append(
        f"| {idx} | {p.get('bibcode', '')} | {p.get('year', '')} | "
        f"{p.get('citation_count', 0)} | {p.get('doctype', '')} | "
        f"{p.get('arxiv_id') or '-'} | {title} |"
    )

  out_path = Path(args.output)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
  print(f"Success! Triage table ({len(papers)} papers) written to: {args.output}")


def _extract_text_from_file(file_path):
  """Extracts plain text from .html/.htm or .pdf files."""
  path = Path(file_path)
  if not path.exists():
    raise RuntimeError(f"Paper file not found: {file_path}")

  suffix = path.suffix.lower()
  if suffix in (".html", ".htm", ".txt"):
    raw = path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".txt":
      return raw
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    return parser.get_text()

  if suffix == ".pdf":
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
      proc = subprocess.run(
          [pdftotext, str(path), "-"],
          capture_output=True,
          text=True,
          check=False,
      )
      if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout
    last_err = None
    for mod in ("pypdf", "fitz"):
      try:
        if mod == "pypdf":
          import pypdf  # pylint: disable=import-outside-toplevel
          reader = pypdf.PdfReader(str(path))
          return "\n".join(page.extract_text() or "" for page in reader.pages)
        import fitz  # pylint: disable=import-outside-toplevel
        doc = fitz.open(str(path))
        return "\n".join(page.get_text() for page in doc)
      except ImportError:
        continue
      except Exception as exc:  # noqa: BLE001 - fall back to next PDF extractor on parse/encryption errors
        last_err = exc
        continue
    if last_err is not None:
      raise RuntimeError(f"Failed to extract text from PDF {path}: {last_err}") from last_err
    raise RuntimeError(
        "No PDF text extractor available. Install `pypdf` into `.venv` "
        "(`uv pip install --python .venv/bin/python \"pypdf<7\"` for Python 3.9, "
        "or `pypdf` for Python >=3.10), install `pdftotext` (poppler), or "
        "download HTML via `arxiv-download --format html`."
    )

  raise RuntimeError(f"Unsupported paper file extension: {suffix}")


def _compile_keyword_regex(kw):
  """Compiles a keyword regex supporting hyphen/whitespace variants and word boundaries."""
  parts = [re.escape(p) for p in re.split(r"[\s-]+", kw.strip()) if p]
  body = r"[\s-]+".join(parts)
  if len(kw) <= 4 and kw.isalnum():
    return re.compile(rf"\b{body}\b", re.IGNORECASE)
  if len(parts) == 1 and parts[0].isalnum():
    return re.compile(rf"\b{body}", re.IGNORECASE)
  return re.compile(body, re.IGNORECASE)


def cmd_scix_filter(args):
  """Filters and ranks scix-fetch papers by keywords or bibcodes with abstracts."""
  _validate_positive("limit", args.limit)
  with open(args.input, "r", encoding="utf-8") as fp:
    data = json.load(fp)

  papers = data.get("papers", [])
  keywords = [
      k.strip().lower()
      for k in (args.keywords or "").split(",")
      if k.strip()
  ]
  kw_patterns = [(kw, _compile_keyword_regex(kw)) for kw in keywords]
  bibcode_filter = {
      b.strip()
      for b in (args.bibcodes or "").split(",")
      if b.strip()
  }

  if not keywords and not bibcode_filter:
    raise RuntimeError("Provide --keywords and/or --bibcodes for scix-filter.")

  scored = []
  for p in papers:
    if bibcode_filter and p.get("bibcode") not in bibcode_filter:
      continue
    title = p.get("title") or ""
    abstract = p.get("abstract") or ""
    matched_kw = []
    score = 0
    for kw, pat in kw_patterns:
      t_hits = len(pat.findall(title))
      a_hits = len(pat.findall(abstract))
      if t_hits or a_hits:
        matched_kw.append(kw)
        score += t_hits * 3 + a_hits
    if keywords and score == 0 and not bibcode_filter:
      continue
    entry = dict(p)
    entry["keyword_score"] = score
    entry["matched_keywords"] = matched_kw
    scored.append(entry)

  scored.sort(
      key=lambda d: (
          d.get("keyword_score") or 0,
          d.get("citation_count") or 0,
          str(d.get("year") or ""),
      ),
      reverse=True,
  )
  selected = scored[: args.limit]
  if not scored:
    print(
        f"Warning: 0 papers matched keywords {keywords}. Try broader keywords "
        f"or inspect `scix-triage` output.",
        file=sys.stderr,
    )
  output = {
      "keywords": keywords,
      "total_matched": len(scored),
      "returned_papers": len(selected),
      "papers": selected,
  }
  write_json(args.output, output)
  print(
      f"Success! Filtered {len(selected)}/{len(scored)} matching paper(s). "
      f"Written to: {args.output}"
  )


def expand_simbad_aliases(raw_aliases):
  """Derives common paper shorthand variants from formal SIMBAD identifiers."""
  expanded = set()
  for alias in raw_aliases or []:
    a = str(alias).strip()
    if not a:
      continue
    expanded.add(a)
    # Strip bracketed catalog prefixes like "[RJC2005] " or "NAME "
    stripped = re.sub(r"^\[[^\]]+\]\s*", "", a).strip()
    stripped = re.sub(r"^(?:NAME|AGAL|IRAS|2MASS|WISEA?|NVSS|MSX6C)\s+", "", stripped, flags=re.IGNORECASE).strip()
    if len(stripped) >= 3:
      expanded.add(stripped)
    # Extract Galactic coordinate tokens like G034.43+00.24 and zero-stripped G34.43+0.24
    for gm in re.finditer(r"G(\d{1,3}\.\d+)([+-]\d{1,2}\.\d+)", a):
      lon, lat = gm.group(1), gm.group(2)
      expanded.add(f"G{lon}{lat}")
      lon_short = lon.lstrip("0") or "0"
      if lon_short.startswith("."):
        lon_short = "0" + lon_short
      lat_sign = lat[0]
      lat_num = lat[1:].lstrip("0") or "0"
      if lat_num.startswith("."):
        lat_num = "0" + lat_num
      expanded.add(f"G{lon_short}{lat}")
      expanded.add(f"G{lon_short}{lat_sign}{lat_num}")
    # Extract core suffixes like "MM 1" -> "MM1"
    for cm in re.finditer(r"\b(MM|SMA|VLA|IRS|EGO|Core)\s*(\d+[A-Za-z]?)\b", a, re.IGNORECASE):
      expanded.add(f"{cm.group(1)} {cm.group(2)}")
      expanded.add(f"{cm.group(1)}{cm.group(2)}")
    # Expand common multi-wavelength catalog prefixes (NGC 1068 <-> NGC1068, M 87 <-> M87, etc.)
    for cat_m in re.finditer(r"\b(NGC|IC|M|3C|4C|HD|SN|GRB|Abell)\s*(\d+[A-Za-z]?)\b", a, re.IGNORECASE):
      prefix, num = cat_m.group(1), cat_m.group(2)
      expanded.add(f"{prefix} {num}")
      expanded.add(f"{prefix}{num}")
      if prefix.lower() == "abell":
        expanded.add(f"A{num}")
        expanded.add(f"A {num}")
  return sorted((x for x in expanded if len(x) >= 3), key=len, reverse=True)


_GRANT_CONTEXT_RE = re.compile(
    r"\b(?:grant|award|fellowship|foundation|supported|funded|NSF|NSFC|ERC|"
    r"NASA|STFC|JSPS|KAKENHI|DFG|Fondecyt|Pujiang|NKRDPC|CAS|MOST|NWO|SNSF|DFG)\b",
    re.IGNORECASE,
)


def extract_project_codes(text):
  """Extracts observatory project/program IDs (ALMA, JCMT, VLA, JWST/HST, X-ray, ESO, NOEMA)."""
  codes = set()
  observatory_patterns = (
      r"\b20\d{2}\.[0-9A-Z]+\.\d{5}(?:\.[A-Z])?\b",  # ALMA
      r"\bM\d{2}[A-Z]{2}\d{3}[A-Z]?\b",  # JCMT
      r"\b[12]\d[AB]-\d{3}\b",  # VLA / VLBA
      r"\b\d{3,4}\.[A-Z]-\d{4}(?:\([A-Z]\))?(?!\w)",  # ESO / VLT / APEX
      r"\b(?:HST|JWST)[-\s]+(?:PID|GO|GTO|ERS|DD|AR)[-\s#]*\d{3,5}\b",  # HST / JWST prefixed
      r"\b(?:PID|GO|GTO|ERS)[-\s#:]+\d{4,5}\b",  # PID / GO / GTO / ERS
      r"\b(?:Obs\.?\s*ID|ObsID)\s*[:#]?\s*\d{4,10}\b",  # Chandra / XMM / Swift / NuSTAR
  )
  for pat in observatory_patterns:
    codes.update(m.strip() for m in re.findall(pat, text, flags=re.IGNORECASE))

  noema_re = re.compile(
      r"\b(?:NOEMA|IRAM|PdBI)\s+(?:project|program|proposal)?\s*[:#]?\s*([SW]\d{2}[A-Z]{2}[0-9]{0,3})\b",
      re.IGNORECASE,
  )
  codes.update(noema_re.findall(text))

  # Strip Acknowledgements/Funding while preserving any subsequent Appendix
  ack_match = re.search(r"(?:^|\n)\s*(?:Acknowledgements?|Acknowledgments?|Funding)\b", text, flags=re.IGNORECASE)
  if ack_match:
    tail = text[ack_match.end():]
    app_match = re.search(r"(?:^|\n)\s*(?:Appendix(?:\s+[A-Z0-9]+)?)\b", tail, flags=re.IGNORECASE)
    body_text = text[:ack_match.start()] + (tail[app_match.start():] if app_match else "")
  else:
    body_text = text

  explicit_re = re.compile(
      r"\b(?:project|program|proposal)\s*(?:code|number|ID|#|:)?\s*"
      r"([A-Z0-9][A-Z0-9._/-]{4,21})\b",
      re.IGNORECASE,
  )
  for m in explicit_re.finditer(body_text):
    ctx_before = body_text[max(0, m.start() - 45) : m.start()]
    if _GRANT_CONTEXT_RE.search(ctx_before):
      continue
    clean = m.group(1).rstrip(".,;:")
    if any(ch.isdigit() for ch in clean) and not clean.isdigit():
      codes.add(clean)
  return sorted(codes)


def _extract_key_sections(text, max_section_chars=4000):
  """Extracts key scientific sections (Data, Observations, Analysis, Results, Conclusions)."""
  heading_re = re.compile(
      r"(?:^|\n)\s*(?:(?:[IVX]+|\d+(?:\.\d+)*)\.?\s+)?"
      r"("
      r"Abstract"
      r"|(?:[A-Za-z0-9/+-]+\s+){0,3}Data(?:\s+Acquisition|\s+Reduction|\s+and\s+Observations?)?"
      r"|(?:[A-Za-z0-9/+-]+\s+){0,4}Observations?(?:\s+and\s+Data\s+Reduction)?"
      r"|Analysis(?:\s+and\s+Results)?"
      r"|Methods?|Methodology|Model(?:s|ing)?"
      r"|Results?(?:\s+and\s+(?:Analysis|Discussion))?"
      r"|Discussion"
      r"|(?:Summary\s+and\s+)?Conclusions?"
      r"|Concluding\s+Remarks"
      r"|Summary"
      r")"
      r"\s*(?:\n|$)",
      re.IGNORECASE,
  )
  matches = list(heading_re.finditer(text))
  sections = []
  for idx, m in enumerate(matches):
    start = m.end()
    end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
    body = text[start:end].strip()[:max_section_chars]
    if body:
      sections.append({
          "heading": m.group(1).strip(),
          "char_offset": m.start(),
          "text": body,
      })
  return sections


def _compile_alias_regex(aliases):
  """Compiles alias regex tolerating whitespace/newlines and bounding short tokens."""
  parts = []
  for a in aliases:
    escaped = re.sub(r"\\ ", r"\\s+", re.escape(a))
    compact_len = len(a.replace(" ", ""))
    if compact_len <= 6 and re.fullmatch(r"[A-Za-z0-9_+ -]+", a):
      prefix = r"\b" if a[0].isalnum() else ""
      suffix = r"\b" if a[-1].isalnum() else ""
      parts.append(f"{prefix}{escaped}{suffix}")
    else:
      parts.append(escaped)
  return re.compile("|".join(parts), re.IGNORECASE)


def cmd_paper_grep(args):
  """Extracts text from a downloaded paper and locates SIMBAD alias matches."""
  _validate_positive("context-chars", args.context_chars)
  _validate_positive("max-matches", args.max_matches)
  _validate_positive("max-section-chars", args.max_section_chars)

  raw_aliases = []
  if args.aliases:
    raw_aliases.extend(a.strip() for a in args.aliases.split(",") if a.strip())
  if args.simbad:
    with open(args.simbad, "r", encoding="utf-8") as fp:
      sim_data = json.load(fp)
    for obj in sim_data.get("objects", []):
      if obj.get("main_id"):
        raw_aliases.append(obj["main_id"])
      raw_aliases.extend(obj.get("aliases", []))

  aliases = (
      expand_simbad_aliases(raw_aliases)
      if not args.no_expand_aliases
      else sorted({a for a in raw_aliases if len(a) >= 3}, key=len, reverse=True)
  )
  if not aliases:
    raise RuntimeError("No aliases provided via --simbad or --aliases.")

  text = _extract_text_from_file(args.file)
  pattern = _compile_alias_regex(aliases)

  matches = []
  for m in pattern.finditer(text):
    start = max(0, m.start() - args.context_chars)
    end = min(len(text), m.end() + args.context_chars)
    matches.append({
        "matched_alias": m.group(0),
        "char_offset": m.start(),
        "snippet": text[start:end].strip(),
    })
    if len(matches) >= args.max_matches:
      break

  if args.text_out:
    txt_path = Path(args.text_out)
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text(text, encoding="utf-8")

  output = {
      "file": args.file,
      "text_length_chars": len(text),
      "text_out": _format_rel_path(args.text_out) if args.text_out else None,
      "searched_aliases_count": len(aliases),
      "project_codes": extract_project_codes(text),
      "match_count": len(matches),
      "matches": matches,
  }
  if args.include_sections:
    output["sections"] = _extract_key_sections(
        text, max_section_chars=args.max_section_chars
    )

  write_json(args.output, output)
  print(
      f"Success! Found {len(matches)} alias match(es) in {args.file}. "
      f"Written to: {args.output}"
  )


def cmd_scix_bibtex(args):
  """Exports BibTeX entries from SciXplorer for specified bibcodes."""
  token, _ = resolve_scix_token(args.token_file)
  if not token:
    raise RuntimeError(
        f"SciXplorer API token not found. Set SCIX_API_TOKEN in ~/.env or "
        f"paste your token into {args.token_file or _format_rel_path(DEFAULT_TOKEN_FILE)}."
    )

  bibcodes = []
  if args.bibcodes:
    bibcodes.extend([b.strip() for b in args.bibcodes.split(",") if b.strip()])
  if args.bibcodes_file:
    raw = Path(args.bibcodes_file).read_text(encoding="utf-8").strip()
    if raw.startswith("[") or raw.startswith("{"):
      parsed = json.loads(raw)
      if isinstance(parsed, list):
        bibcodes.extend(str(b).strip() for b in parsed if str(b).strip())
      elif isinstance(parsed, dict) and "papers" in parsed:
        bibcodes.extend(
            p["bibcode"] for p in parsed["papers"] if p.get("bibcode")
        )
      elif isinstance(parsed, dict) and "tier_a" in parsed:
        bibcodes.extend(
            p["bibcode"] for p in parsed["tier_a"] if p.get("bibcode")
        )
    else:
      bibcodes.extend([ln.strip() for ln in raw.splitlines() if ln.strip()])

  bibcodes = sorted(set(bibcodes))
  if not bibcodes:
    raise RuntimeError("No bibcodes provided for BibTeX export.")

  client = SciXplorerClient(token)
  chunks = []
  for i in range(0, len(bibcodes), 50):
    resp = client.export_bibtex(bibcodes[i : i + 50])
    exported = (resp.get("export") or "").strip()
    if exported:
      chunks.append(exported)

  if not chunks:
    raise RuntimeError(
        f"SciXplorer returned empty BibTeX export for {len(bibcodes)} bibcode(s)."
    )

  out_path = Path(args.output)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  out_text = "\n\n".join(chunks) + "\n"
  out_path.write_text(out_text, encoding="utf-8")
  exported_count = len(re.findall(r"^@", out_text, flags=re.MULTILINE))
  print(
      f"Success! Exported {exported_count}/{len(bibcodes)} BibTeX entries to: "
      f"{args.output}"
  )


def _ensure_arxiv_license_notice():
  """Creates .licenses/literature_search_arxiv_LICENSE.txt if missing."""
  lic_path = Path(".licenses/literature_search_arxiv_LICENSE.txt")
  if not lic_path.exists():
    lic_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lic_path.write_text(
        "arXiv API Terms of Use Notification\n"
        f"Recorded: {ts}\n"
        "Please review arXiv API terms at https://info.arxiv.org/help/api/index.html "
        "and verify the license of each retrieved paper before redistribution.\n",
        encoding="utf-8",
    )
    print(
        "Notice: Review arXiv API Terms of Use at "
        "https://info.arxiv.org/help/api/index.html and verify individual "
        "paper licenses. Recorded in .licenses/literature_search_arxiv_LICENSE.txt"
    )
  return lic_path.as_posix()


def _strip_arxiv_version(arxiv_id):
  return re.sub(r"v\d+$", "", str(arxiv_id).strip(), flags=re.IGNORECASE)


def cmd_arxiv_download(args):
  """Downloads arXiv PDF and/or HTML papers into the project directory."""
  out_dir = Path(args.output_dir)
  out_dir.mkdir(parents=True, exist_ok=True)
  license_file = _ensure_arxiv_license_notice()

  idx_data = None
  slug_by_arxiv = {}
  if args.notes_index:
    idx_path = Path(args.notes_index)
    if not idx_path.exists():
      raise RuntimeError(f"Notes index not found: {args.notes_index}")
    idx_data = json.loads(idx_path.read_text(encoding="utf-8"))
    for entry in idx_data.get("tier_a", []):
      aid = (entry.get("arxiv_id") or "").strip()
      slug = entry.get("slug")
      if aid and slug:
        slug_by_arxiv[aid] = slug
        slug_by_arxiv[_strip_arxiv_version(aid)] = slug

  if args.id:
    raw_ids = [i.strip() for i in args.id.split(",") if i.strip()]
  elif idx_data is not None:
    raw_ids = [
        e["arxiv_id"]
        for e in idx_data.get("tier_a", [])
        if e.get("arxiv_id") and e.get("status") == "pending"
    ]
  else:
    raise RuntimeError("Provide --id or --notes-index for arxiv-download.")

  if not raw_ids:
    raise RuntimeError("No arXiv IDs to download.")

  scix_by_bib = {}
  if args.scix and Path(args.scix).exists():
    scix_json = json.loads(Path(args.scix).read_text(encoding="utf-8"))
    for p in scix_json.get("papers", []):
      if p.get("bibcode"):
        scix_by_bib[p["bibcode"]] = p

  client = ArxivClient()
  records = []
  succeeded = 0
  rec_by_arxiv = {}
  for raw_id in raw_ids:
    clean_id = re.sub(r"^arXiv:", "", raw_id, flags=re.IGNORECASE).strip()
    safe_name = (
        slug_by_arxiv.get(clean_id)
        or slug_by_arxiv.get(_strip_arxiv_version(clean_id))
        or clean_id.replace("/", "_")
    )
    rec = {
        "arxiv_id": clean_id,
        "abs_url": f"https://arxiv.org/abs/{clean_id}",
        "status": "ok",
        "pdf_path": None,
        "pdf_url": None,
        "html_path": None,
        "html_url": None,
        "errors": [],
    }

    want_html = args.format in ("html", "both", "auto")
    if want_html:
      html_file = out_dir / f"{safe_name}.html"
      if html_file.exists() and html_file.stat().st_size > 0 and not args.force:
        rec["html_path"] = _format_rel_path(html_file)
        rec["html_url"] = f"https://arxiv.org/html/{clean_id}"
      else:
        try:
          html_bytes, html_url = client.download_html(clean_id)
          html_file.write_bytes(html_bytes)
          rec["html_path"] = _format_rel_path(html_file)
          rec["html_url"] = html_url
        except (RuntimeError, RateLimitError) as exc:
          if args.fail_fast and args.format == "html":
            raise
          rec["errors"].append(f"html: {exc}")
          print(
              f"Warning: HTML unavailable for {clean_id} ({exc}).",
              file=sys.stderr,
          )

    want_pdf = args.format in ("pdf", "both") or (
        args.format == "auto" and not rec["html_path"]
    )
    if want_pdf:
      pdf_file = out_dir / f"{safe_name}.pdf"
      if pdf_file.exists() and pdf_file.stat().st_size > 0 and not args.force:
        rec["pdf_path"] = _format_rel_path(pdf_file)
        rec["pdf_url"] = f"https://arxiv.org/pdf/{clean_id}.pdf"
      else:
        try:
          pdf_bytes, pdf_url = client.download_pdf(clean_id)
          pdf_file.write_bytes(pdf_bytes)
          rec["pdf_path"] = _format_rel_path(pdf_file)
          rec["pdf_url"] = pdf_url
        except (RuntimeError, RateLimitError) as exc:
          if args.fail_fast:
            raise
          rec["errors"].append(f"pdf: {exc}")
          print(f"Warning: PDF download failed for {clean_id}: {exc}", file=sys.stderr)

    has_any = bool(rec["pdf_path"] or rec["html_path"])
    if not has_any:
      rec["status"] = "failed"
    elif rec["errors"]:
      rec["status"] = "partial"
      succeeded += 1
    else:
      succeeded += 1

    records.append(rec)
    rec_by_arxiv[clean_id] = rec
    rec_by_arxiv[_strip_arxiv_version(clean_id)] = rec

  if idx_data is not None:
    for entry in idx_data.get("tier_a", []):
      aid = (entry.get("arxiv_id") or "").strip()
      r = rec_by_arxiv.get(aid) or rec_by_arxiv.get(_strip_arxiv_version(aid))
      if not r:
        continue
      preferred_path = r["html_path"] or r["pdf_path"]
      if preferred_path:
        entry["paper_path"] = preferred_path
      else:
        entry["paper_path"] = None
        entry["status"] = "abstract_only"
        bib = entry.get("bibcode")
        paper_obj = scix_by_bib.get(bib, {})
        note_file = Path(entry["note_path"])
        if not note_file.exists():
          _write_abstract_stub(
              note_file,
              bib=bib,
              author_year=entry.get("author_year", ""),
              slug=entry.get("slug", ""),
              wikilink=entry.get("wikilink", ""),
              title=entry.get("title") or paper_obj.get("title") or "",
              abstract=paper_obj.get("abstract") or "No abstract available (arXiv download failed).",
          )
    write_json(args.notes_index, idx_data)

  manifest = {
      "license_file": license_file,
      "output_dir": _format_rel_path(out_dir),
      "requested_count": len(records),
      "downloaded_count": succeeded,
      "papers": records,
  }
  write_json(args.output, manifest)
  if succeeded == 0 and idx_data is None:
    raise RuntimeError(
        f"All {len(records)} arXiv download(s) failed. Details written to: {args.output}"
    )
  print(
      f"Success! Downloaded {succeeded}/{len(records)} paper(s) to {out_dir}. "
      f"Manifest written to: {args.output}"
  )


def _extract_surname(author_str):
  """Extracts surname from 'Last, First' or 'First Last', stripping '|'."""
  clean = str(author_str or "").replace("|", "").strip()
  if not clean:
    return ""
  if "," in clean:
    return clean.split(",", 1)[0].strip()
  parts = clean.split()
  return parts[-1] if parts else ""


def _first_author_surname(authors):
  """Extracts ASCII-folded surname of the first non-empty author."""
  for author in authors or []:
    surname = _extract_surname(author)
    if surname:
      folded = unicodedata.normalize("NFKD", surname).encode("ascii", "ignore").decode("ascii")
      cleaned = re.sub(r"[^A-Za-z0-9]+", "", folded)
      if cleaned:
        return cleaned
  return "Unknown"


def make_slug(authors, year, id_or_bibcode):
  """Creates a deterministic, cross-OS/OneDrive-safe slug (<=48 chars)."""
  surname = _first_author_surname(authors)[:16]
  yr = re.sub(r"[^0-9]", "", str(year or ""))[:4] or "0000"
  raw_id = re.sub(r"^arXiv:", "", str(id_or_bibcode or ""), flags=re.IGNORECASE).strip()
  safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_id)
  safe_id = re.sub(r"[._]{2,}", "_", safe_id).strip("._-") or "paper"
  prefix = f"{surname}_{yr}_"
  max_id_len = max(8, 48 - len(prefix))
  slug = f"{prefix}{safe_id[:max_id_len]}"[:48].rstrip("._-")
  return slug or f"{surname}_{yr}"


def format_author_year(authors, year):
  """Formats citation label as 'Surname (Year)', 'A & B (Year)', or 'A et al. (Year)'."""
  yr = str(year or "").strip() or "????"
  surnames = [s for s in (_extract_surname(a) for a in (authors or [])) if s]
  if not surnames:
    return f"Unknown ({yr})"
  if len(surnames) == 1:
    return f"{surnames[0]} ({yr})"
  if len(surnames) == 2:
    return f"{surnames[0]} & {surnames[1]} ({yr})"
  return f"{surnames[0]} et al. ({yr})"


def _parse_classification_map(cls_data):
  """Normalizes {bibcode: 'A'} or {'A'|'Tier_A': [bibcodes]} into {bibcode: 'A'|'B'|'C'}."""
  mapping = {}
  if not isinstance(cls_data, dict):
    raise RuntimeError("Classification JSON must be an object.")
  for k, v in cls_data.items():
    norm_key = str(k).strip().upper().replace("TIER_", "").replace("TIER ", "")
    if norm_key in ("A", "B", "C") and isinstance(v, list):
      for bib in v:
        if str(bib).strip():
          mapping[str(bib).strip()] = norm_key
    elif isinstance(v, str):
      tier = v.strip().upper().replace("TIER_", "").replace("TIER ", "")
      if tier not in ("A", "B", "C"):
        raise RuntimeError(f"Invalid tier {v!r} for bibcode {k!r}.")
      mapping[str(k).strip()] = tier
    else:
      raise RuntimeError(f"Unrecognized classification entry: {k!r}: {v!r}")
  return mapping


def _write_abstract_stub(note_file, bib, author_year, slug, wikilink, title, abstract):
  """Writes an [Abstract Only] note stub so wikilinks resolve without full text."""
  clean_title = str(title or "").replace("\n", " ").strip()
  clean_abs = str(abstract or "No abstract available.").strip()
  stub = (
      "---\n"
      f"bibcode: \"{bib}\"\n"
      f"author_year: \"{author_year}\"\n"
      f"slug: \"{slug}\"\n"
      f"wikilink: \"{wikilink}\"\n"
      "status: \"abstract_only\"\n"
      "---\n\n"
      f"# {author_year}: {clean_title}\n\n"
      "> **Source**: ADS/SciXplorer Abstract Only (No Full Text)\n\n"
      "## Key Takeaways & Relevance\n"
      f"- {clean_abs}\n"
  )
  note_file.parent.mkdir(parents=True, exist_ok=True)
  note_file.write_text(stub, encoding="utf-8")


def _write_tier_catalog(path, tier_label, papers):
  """Writes a single aggregate Markdown catalog for Tier B or Tier C papers."""
  lines = [f"# {tier_label} Literature Catalog", ""]
  if not papers:
    lines.append("_No papers classified in this tier._")
  else:
    for idx, p in enumerate(papers, start=1):
      ay = format_author_year(p.get("authors") or [], p.get("year"))
      title = (p.get("title") or "").replace("\n", " ").strip()
      bib = p.get("bibcode", "")
      abs_txt = (p.get("abstract") or "No abstract available.").replace("\n", " ").strip()
      lines.extend([
          f"## {idx}. {ay} — {title}",
          f"- **Bibcode**: `{bib}`",
          f"- **DOI**: `{p.get('doi') or '-'}` | **arXiv**: `{p.get('arxiv_id') or '-'}`",
          f"- **Abstract**: {abs_txt}",
          "",
      ])
  out = Path(path)
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_init_notes(args):
  """Initializes Tier A flat directory, [Abstract Only] stubs, Tier B/C catalogs, and notes_index.json."""
  scix_data = json.loads(Path(args.scix).read_text(encoding="utf-8"))
  cls_data = json.loads(Path(args.classification).read_text(encoding="utf-8"))
  tier_map = _parse_classification_map(cls_data)

  papers_dir = Path(args.papers_dir)
  tier_a_dir = papers_dir / "Tier_A"
  tier_a_dir.mkdir(parents=True, exist_ok=True)

  out_index_path = Path(args.output)
  catalog_dir = out_index_path.parent

  existing_by_bib = {}
  if out_index_path.exists():
    prev_idx = json.loads(out_index_path.read_text(encoding="utf-8"))
    for entry in prev_idx.get("tier_a", []):
      if entry.get("bibcode"):
        existing_by_bib[entry["bibcode"]] = entry

  scix_papers = scix_data.get("papers", [])
  scix_bibs = {p.get("bibcode") for p in scix_papers if p.get("bibcode")}
  unknown_bibcodes = sorted(set(tier_map.keys()) - scix_bibs)
  unclassified_bibcodes = sorted(scix_bibs - set(tier_map.keys()))

  if unknown_bibcodes:
    print(
        f"Warning: {len(unknown_bibcodes)} bibcode(s) in classification.json "
        f"were not found in scix_papers.json: {unknown_bibcodes[:5]}",
        file=sys.stderr,
    )
  if unclassified_bibcodes:
    print(
        f"Warning: {len(unclassified_bibcodes)} paper(s) in scix_papers.json "
        f"were omitted from classification.json.",
        file=sys.stderr,
    )

  tier_a_entries = []
  tier_b_papers = []
  tier_c_papers = []
  seen_slugs = set()

  for p in scix_papers:
    bib = p.get("bibcode")
    tier = tier_map.get(bib)
    if tier == "B":
      tier_b_papers.append(p)
      continue
    if tier == "C":
      tier_c_papers.append(p)
      continue
    if tier != "A":
      continue

    arxiv_id = p.get("arxiv_id")
    base_slug = make_slug(p.get("authors") or [], p.get("year"), arxiv_id or bib)
    slug = base_slug
    if slug in seen_slugs:
      slug = make_slug(p.get("authors") or [], p.get("year"), bib)
    counter = 2
    while slug in seen_slugs:
      suffix = f"_{counter}"
      slug = f"{base_slug[:48 - len(suffix)].rstrip('._-')}{suffix}"
      counter += 1
    seen_slugs.add(slug)

    author_year = format_author_year(p.get("authors") or [], p.get("year"))
    note_name = f"{slug}_note"
    note_file = tier_a_dir / f"{note_name}.md"
    wikilink = f"[[{note_name}|{author_year}]]"
    wikilink_table = f"[[{note_name}\\|{author_year}]]"

    prev = existing_by_bib.get(bib, {})
    if arxiv_id:
      status = prev.get("status") if prev.get("status") in ("completed", "abstract_only") else "pending"
      paper_path = prev.get("paper_path")
      if not paper_path:
        html_cand = tier_a_dir / f"{slug}.html"
        pdf_cand = tier_a_dir / f"{slug}.pdf"
        if html_cand.exists():
          paper_path = _format_rel_path(html_cand)
        elif pdf_cand.exists():
          paper_path = _format_rel_path(pdf_cand)
    else:
      status = "abstract_only"
      paper_path = None
      if not note_file.exists():
        _write_abstract_stub(
            note_file,
            bib=bib,
            author_year=author_year,
            slug=slug,
            wikilink=wikilink,
            title=p.get("title") or "",
            abstract=p.get("abstract") or "No abstract available.",
        )

    tier_a_entries.append({
        "bibcode": bib,
        "arxiv_id": arxiv_id,
        "author_year": author_year,
        "title": p.get("title") or "",
        "slug": slug,
        "paper_path": paper_path,
        "note_path": _format_rel_path(note_file),
        "wikilink": wikilink,
        "wikilink_table": wikilink_table,
        "status": status,
    })

  tier_b_path = catalog_dir / "Tier_B_catalog.md"
  tier_c_path = catalog_dir / "Tier_C_catalog.md"
  _write_tier_catalog(tier_b_path, "Tier B — Secondary & Background", tier_b_papers)
  _write_tier_catalog(tier_c_path, "Tier C — Catalog & Survey Mentions", tier_c_papers)

  index_payload = {
      "tier_a_dir": _format_rel_path(tier_a_dir),
      "tier_b_catalog": _format_rel_path(tier_b_path),
      "tier_c_catalog": _format_rel_path(tier_c_path),
      "counts": {
          "tier_a": len(tier_a_entries),
          "tier_b": len(tier_b_papers),
          "tier_c": len(tier_c_papers),
          "unclassified": len(unclassified_bibcodes),
          "unknown": len(unknown_bibcodes),
      },
      "unclassified_bibcodes": unclassified_bibcodes,
      "unknown_bibcodes": unknown_bibcodes,
      "tier_a": tier_a_entries,
  }
  write_json(out_index_path, index_payload)
  print(
      f"Success! Initialized {len(tier_a_entries)} Tier A note target(s), "
      f"{len(tier_b_papers)} Tier B, and {len(tier_c_papers)} Tier C entries. "
      f"Index written to: {args.output}"
  )


def cmd_verify_notes(args):
  """Verifies Tier A note files exist/non-empty and updates notes_index.json statuses."""
  idx_path = Path(args.notes_index)
  if not idx_path.exists():
    raise RuntimeError(f"Notes index not found: {args.notes_index}")
  idx_data = json.loads(idx_path.read_text(encoding="utf-8"))

  completed = 0
  failed = 0
  for entry in idx_data.get("tier_a", []):
    note_file = Path(entry["note_path"])
    if entry.get("status") == "abstract_only" and note_file.exists() and note_file.stat().st_size > 0:
      continue
    if note_file.exists() and note_file.read_text(encoding="utf-8").strip():
      entry["status"] = "completed"
      completed += 1
    else:
      entry["status"] = "failed"
      failed += 1

  write_json(args.output, idx_data)
  print(
      f"Success! Verified Tier A notes: {completed} completed, {failed} failed. "
      f"Updated index written to: {args.output}"
  )
  if failed:
    sys.exit(1)


def build_parser():
  """Builds the CLI argument parser."""
  parser = argparse.ArgumentParser(
      description="SIMBAD TAP & SciXplorer CLI for astro-source-literature-review"
  )
  sub = parser.add_subparsers(dest="command", required=True)

  p_env = sub.add_parser("check-env", help="Check Python/Conda, uv, and API token")
  p_env.add_argument("--token-file", default=None, help="Path to token file")
  p_env.add_argument("--output", required=True, help="Output JSON path")
  p_env.set_defaults(func=cmd_check_env)

  p_sim = sub.add_parser("simbad-query", help="Cone search SIMBAD TAP")
  p_sim.add_argument("--ra", required=True, help="RA (deg or HH:MM:SS.s)")
  p_sim.add_argument("--dec", required=True, help="Dec (deg or +/-DD:MM:SS.s)")
  p_sim.add_argument(
      "--radius-arcsec", type=float, required=True, help="Search radius in arcsec"
  )
  p_sim.add_argument(
      "--limit", type=int, required=True, help="Max SIMBAD objects to return"
  )
  p_sim.add_argument("--output", required=True, help="Output JSON path")
  p_sim.set_defaults(func=cmd_simbad_query)

  p_fetch = sub.add_parser("scix-fetch", help="Fetch metadata/abstracts via SciXplorer")
  p_fetch.add_argument("--input", required=True, help="Input JSON from simbad-query")
  p_fetch.add_argument("--oid", type=int, default=None, help="Filter to a single SIMBAD oid")
  p_fetch.add_argument("--oids", default=None, help="Comma-separated SIMBAD oids to merge")
  p_fetch.add_argument(
      "--limit", type=int, required=True, help="Max bibcodes to fetch"
  )
  p_fetch.add_argument("--token-file", default=None, help="Path to token file")
  p_fetch.add_argument("--output", required=True, help="Output JSON path")
  p_fetch.set_defaults(func=cmd_scix_fetch)

  p_tri = sub.add_parser("scix-triage", help="Write compact Markdown table for triage")
  p_tri.add_argument("--input", required=True, help="Input JSON from scix-fetch")
  p_tri.add_argument("--limit", type=int, required=True, help="Max papers to list")
  p_tri.add_argument("--output", required=True, help="Output Markdown path")
  p_tri.set_defaults(func=cmd_scix_triage)

  p_flt = sub.add_parser("scix-filter", help="Filter and rank scix-fetch papers with abstracts")
  p_flt.add_argument("--input", required=True, help="Input JSON from scix-fetch")
  p_flt.add_argument("--keywords", default=None, help="Comma-separated keywords")
  p_flt.add_argument("--bibcodes", default=None, help="Comma-separated bibcodes")
  p_flt.add_argument("--limit", type=int, required=True, help="Max papers to return")
  p_flt.add_argument("--output", required=True, help="Output JSON path")
  p_flt.set_defaults(func=cmd_scix_filter)

  p_grep = sub.add_parser("paper-grep", help="Search downloaded PDF/HTML for SIMBAD aliases")
  p_grep.add_argument("--file", required=True, help="Path to downloaded PDF or HTML")
  p_grep.add_argument("--simbad", default=None, help="Path to simbad-query JSON")
  p_grep.add_argument("--aliases", default=None, help="Comma-separated aliases")
  p_grep.add_argument("--context-chars", type=int, default=600, help="Context chars around match")
  p_grep.add_argument("--max-matches", type=int, default=20, help="Max matches to return")
  p_grep.add_argument("--no-expand-aliases", action="store_true", help="Disable shorthand alias expansion")
  p_grep.add_argument("--include-sections", action="store_true", help="Also extract key paper sections/tables")
  p_grep.add_argument("--max-section-chars", type=int, default=4000, help="Max chars per extracted section")
  p_grep.add_argument("--text-out", default=None, help="Optional path to save extracted plain text")
  p_grep.add_argument("--output", required=True, help="Output JSON path")
  p_grep.set_defaults(func=cmd_paper_grep)

  p_init = sub.add_parser(
      "init-notes",
      help="Initialize flat Tier_A note targets, Tier B/C catalogs, and notes_index.json",
  )
  p_init.add_argument("--scix", required=True, help="Input JSON from scix-fetch")
  p_init.add_argument("--classification", required=True, help="JSON mapping bibcodes to A/B/C")
  p_init.add_argument("--papers-dir", default="./literature_review/papers", help="Base papers directory")
  p_init.add_argument("--output", required=True, help="Output notes_index.json path")
  p_init.set_defaults(func=cmd_init_notes)

  p_ver = sub.add_parser(
      "verify-notes",
      help="Verify Tier A note files and update notes_index.json statuses",
  )
  p_ver.add_argument("--notes-index", required=True, help="Path to notes_index.json")
  p_ver.add_argument("--output", required=True, help="Output notes_index.json path")
  p_ver.set_defaults(func=cmd_verify_notes)

  p_bib = sub.add_parser("scix-bibtex", help="Export BibTeX via SciXplorer")
  p_bib.add_argument("--bibcodes", default=None, help="Comma-separated bibcodes")
  p_bib.add_argument(
      "--bibcodes-file", default=None, help="File with bibcodes, scix-fetch JSON, or notes_index.json"
  )
  p_bib.add_argument("--token-file", default=None, help="Path to token file")
  p_bib.add_argument("--output", required=True, help="Output .bib path")
  p_bib.set_defaults(func=cmd_scix_bibtex)

  p_dl = sub.add_parser(
      "arxiv-download",
      help="Download arXiv PDF/HTML into project directory (1 req / 3s)",
  )
  p_dl.add_argument("--id", default=None, help="Comma-separated arXiv ID(s) (optional if --notes-index is set)")
  p_dl.add_argument(
      "--format",
      choices=["pdf", "html", "both", "auto"],
      default="pdf",
      help="Download format: pdf, html, both, or auto (html first, pdf fallback) (default: pdf)",
  )
  p_dl.add_argument(
      "--output-dir",
      default="./literature_review/papers",
      help="Project directory for downloaded papers",
  )
  p_dl.add_argument("--notes-index", default=None, help="Optional notes_index.json to name files by <slug> and sync status")
  p_dl.add_argument("--scix", default=None, help="Optional scix_papers.json for [Abstract Only] fallback stubs")
  p_dl.add_argument("--force", action="store_true", help="Re-download even if file already exists")
  p_dl.add_argument("--fail-fast", action="store_true", help="Abort immediately on first download error")
  p_dl.add_argument("--output", required=True, help="Output manifest JSON path")
  p_dl.set_defaults(func=cmd_arxiv_download)

  return parser


def _normalize_negative_coord_args(argv):
  """Converts `--dec -19:43:51` to `--dec=-19:43:51` so argparse accepts it."""
  normalized = []
  i = 0
  while i < len(argv):
    if argv[i] == "--dec" and i + 1 < len(argv):
      normalized.append(f"--dec={argv[i + 1]}")
      i += 2
    else:
      normalized.append(argv[i])
      i += 1
  return normalized


def main():
  parser = build_parser()
  args = parser.parse_args(_normalize_negative_coord_args(sys.argv[1:]))
  try:
    args.func(args)
  except Exception as exc:
    print(f"Error: {exc}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
  main()
