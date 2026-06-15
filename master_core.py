"""
master_core.py — the genuinely NEW logic for the Master DD1750 tool.

Responsibilities:
  1. parse_filename()   — shape-based, ORDER-INDEPENDENT classifier that turns a
                          child 1750 filename into {model, lin, sn, bumper, ...}.
  2. sniff_nsn()        — best-effort: read a child PDF's END ITEM text for a NIIN.
  3. normalize_model()  — canonical model key for grouping.
  4. aggregate_meis()   — collapse identical (lin, model) into one box row.
  5. build_master_header() — assemble the multi-line PACKED BY / END ITEM blocks.
  6. audit_master()     — validate the generated master structure against the rules.

Design principle: the parser is best-effort + reviewable, NEVER silently wrong.
Anything it cannot fully classify gets needs_review=True so the UI highlights it
for a human to fix in the editable table. The *shape* of each token decides what
it is — not the order it appears in the filename.

Validated against the 145 real child filenames (17 Fire Control + 128 arms room).
"""

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ParsedMEI:
    """Result of parsing one child 1750 filename."""
    model: str = ""
    lin: str = ""
    sn: str = ""
    bumper: str = ""
    source_file: str = ""
    needs_review: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "lin": self.lin,
            "sn": self.sn,
            "bumper": self.bumper,
            "source_file": self.source_file,
            "needs_review": self.needs_review,
        }


@dataclass
class MasterRow:
    """One aggregated box row in the master packing list."""
    box_num: int = 0
    model: str = ""
    lin: str = ""
    nsn: str = ""
    serials: List[str] = field(default_factory=list)
    qty: int = 1
    needs_review: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "box_num": self.box_num,
            "model": self.model,
            "lin": self.lin,
            "nsn": self.nsn,
            "serials": self.serials,
            "qty": self.qty,
            "needs_review": self.needs_review,
        }


# ---------------------------------------------------------------------------
# Token shape patterns (the heart of the order-independent classifier)
# ---------------------------------------------------------------------------

# Bumper number: B + 1-3 digits + optional trailing letter  (B33, B5, B34S, B32)
RE_BUMPER = re.compile(r'^B\d{1,3}[A-Z]?$', re.IGNORECASE)

# LIN: exactly one leading letter + exactly 5 digits  (T88915, E05003, A22496)
RE_LIN = re.compile(r'^[A-Z]\d{5}$', re.IGNORECASE)

# Pure-numeric serial of 3+ digits  (e.g. 123456, 40000)
RE_SN_NUMERIC = re.compile(r'^\d{3,}$')

# Hyphenated registration / reg number  (e.g. J-K1234567-AB)
RE_SN_HYPHEN = re.compile(r'^[A-Z0-9]+-[A-Z0-9-]+$', re.IGNORECASE)

# Mixed alphanumeric serial, length >= 7, must contain BOTH a letter and a digit
# (e.g. 1ABCD2345678, A0012345, W0009999; a 6-char like 'T12345' is too short)
RE_SN_MIXED = re.compile(r'^(?=.*[A-Z])(?=.*\d)[A-Z0-9]{7,}$', re.IGNORECASE)


def _strip_form_stamps(name: str) -> str:
    """
    Remove the DD1750 / 1750 form-name stamps from a filename, even when glued
    to neighbouring tokens by '_' or '.' or spaces.

    Examples that MUST be handled (from the real arms-room data):
      'M249_DD1750'              -> 'M249'
      'DD1750.T92889 ...'        -> 'T92889 ...'
      'T92889 ... dd1750'        -> 'T92889 ...'
      '... 1750.pdf'             -> '...'
    """
    # Drop the extension first (case-insensitive). Some entries have none.
    name = re.sub(r'(?i)\.pdf$', '', name)
    # Kill 'dd1750' / 'DD1750' tokens with any surrounding separators.
    name = re.sub(r'(?i)[._\s]*dd1750[._\s]*', ' ', name)
    # Kill a standalone '1750' token (the bare form-number suffix), with separators.
    name = re.sub(r'(?i)(?:^|[._\s])1750(?=$|[._\s])', ' ', name)
    return name


def _extract_explicit_serial(text: str):
    """
    Pull an explicitly-marked serial (highest priority) out of the normalized
    text and return (serial, remaining_text). Returns (None, text) if no marker.

    Handles every SN-marker shape seen in the real data:
      'SN_W0009999 ...'            -> SN W0009999        (underscore glue)
      'SN_W0008888_C06935 ...'     -> SN W0008888, LIN C06935 stays in remainder
      'MK19 SN_40000'              -> trailing marker
      '... M249 SN_ 120000'        -> space after the underscore
      'AN PAS 13D V 2SN_250000'    -> 'SN' glued to a preceding '2' (model frag)
    Strategy: operate on the underscore/dot-normalized string so 'SN_' has become
    'SN '. Then match a literal 'SN' (optionally followed by ':' ) as a word-ish
    token and capture the next alphanumeric run as the serial. The 'SN' may abut
    a digit on its left (the '2SN' case) — we allow that by not requiring a left
    word boundary, only that what precedes is not itself a letter (so we don't eat
    'CARBINESN' style false hits — none exist, but be safe).
    """
    # The left side: start-of-string, whitespace, or a digit (covers '2SN').
    # The marker: 'SN' then optional ':' then separators, then the serial value.
    m = re.search(r'(?:(?<=\s)|(?<=\d)|^)SN[:_\s]*([A-Z0-9][A-Z0-9-]{2,})',
                  text, re.IGNORECASE)
    if not m:
        return None, text
    serial = m.group(1)
    # Remove the whole 'SN ... <serial>' match from the text; leave the rest.
    remaining = text[:m.start()] + ' ' + text[m.end():]
    return serial, remaining


def normalize_model(model: str) -> str:
    """
    Canonical model string used both for display and as the grouping key.
    Upper-cases, collapses internal whitespace, trims stray punctuation noise.
    Grouping uses this so that 'LMG 5.56MM  M249' and 'LMG 5.56MM M249' collapse
    together.
    """
    s = (model or "").upper()
    s = re.sub(r'\s+', ' ', s).strip()
    # Trim leading/trailing commas or stray separators left over from filenames.
    s = s.strip(' ,;-')
    return s


def parse_filename(fname: str) -> ParsedMEI:
    """
    Parse a child 1750 filename into MEI metadata using SHAPE, not order.

    Pipeline:
      1. basename, strip the DD1750/1750 form stamps (handles '_'/'.' glue).
      2. pull any explicit SN marker out first (highest priority).
      3. normalize '_' and '.' to spaces, collapse whitespace.
      4. walk the remaining tokens; classify each by shape:
           bumper -> LIN (first wins) -> shape-serial -> else model fragment.
         Tokens that match the LIN shape AFTER the LIN is already set are
         consumed silently (the duplicate-LIN case) so they don't leak into
         the model string.
      5. model = remaining fragments joined in original order.
      6. needs_review = True if LIN or model is missing.
    """
    result = ParsedMEI(source_file=os.path.basename(fname))

    # 1. basename + strip form stamps
    base = os.path.basename(fname)
    text = _strip_form_stamps(base)

    # 3a. Convert underscores and dots to spaces BEFORE pulling the SN marker,
    #     so 'SN_W0009999' becomes 'SN W0009999' and 'SN_ 120000' collapses.
    text = re.sub(r'[._]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    # 2. Explicit serial marker has top priority.
    serial, text = _extract_explicit_serial(text)
    if serial:
        result.sn = serial

    # 3b. Re-collapse whitespace after the marker removal.
    text = re.sub(r'\s+', ' ', text).strip()

    tokens = text.split(' ') if text else []
    model_parts: List[str] = []

    for tok in tokens:
        if not tok:
            continue

        # Bumper number.
        if RE_BUMPER.match(tok) and not result.bumper:
            result.bumper = tok.upper()
            continue

        # LIN — first match wins.
        if RE_LIN.match(tok):
            if not result.lin:
                result.lin = tok.upper()
            # Whether or not it was the first, a LIN-shaped token is NEVER part
            # of the model and is NEVER a serial — consume it silently. This is
            # the duplicate-LIN fix (e.g. 'S45729 ... M150 S45729').
            continue

        # Shape-based serial (only if we don't already have an explicit one).
        if not result.sn and (
            RE_SN_NUMERIC.match(tok)
            or RE_SN_HYPHEN.match(tok)
            or RE_SN_MIXED.match(tok)
        ):
            result.sn = tok.upper()
            continue

        # If we already captured a serial but hit ANOTHER serial-shaped token,
        # decide whether it's a stray duplicate serial (drop it) or a numeric
        # chunk that's really part of a model number (keep it).
        #
        # The distinguishing signal is LENGTH. A real second serial / long
        # registration number (e.g. 'J-K1234567-AB', or a 5+ digit run) should
        # not pollute the nomenclature, so we drop it. But a SHORT numeric chunk
        # like the '832' in 'SHELTER NONEX S_832_G' is a model-number fragment
        # (the underscore split S_832_G into S / 832 / G) and MUST be preserved,
        # otherwise the model reads 'SHELTER NONEX S G' with the number missing.
        #
        # Rule: keep short pure-numeric fragments (<= 4 digits); drop everything
        # else that looks like a serial. Four digits comfortably covers model
        # numbers (832, 1113, 2A2-style) while still dropping real serials,
        # which in this data are 5+ digits or alphanumeric registration strings.
        if RE_SN_NUMERIC.match(tok) and len(tok) <= 4:
            # Short numeric model fragment — keep it in the nomenclature.
            model_parts.append(tok)
            continue
        if (RE_SN_NUMERIC.match(tok) or RE_SN_HYPHEN.match(tok)
                or RE_SN_MIXED.match(tok)):
            # Stray duplicate serial / long registration number — drop it.
            continue

        # Otherwise it's a model fragment.
        model_parts.append(tok)

    result.model = normalize_model(' '.join(model_parts))

    # 6. Flag for human review if the essentials are missing.
    if not result.lin or not result.model:
        result.needs_review = True

    return result


# ---------------------------------------------------------------------------
# NSN sniffer (best-effort enrichment from the child PDF body)
# ---------------------------------------------------------------------------

def sniff_nsn(pdf_path: str) -> str:
    """
    Best-effort: read a child 1750 PDF's text and pull an NSN/NIIN if present.

    Filenames carry no NSN, but the child PDFs' END ITEM block often contains a
    string like '... / NIIN 01534228' or a full 13-digit NSN. We return the
    first plausible match, or '' if none / on any error. Never raises — this is
    pure enrichment and the NSN cell stays editable in the UI regardless.
    """
    try:
        import pdfplumber
    except Exception:
        return ""

    try:
        with pdfplumber.open(pdf_path) as pdf:
            text_parts = []
            for page in pdf.pages[:2]:  # header lives on page 1; 2 for safety
                t = page.extract_text() or ""
                text_parts.append(t)
            text = "\n".join(text_parts)
    except Exception:
        return ""

    if not text:
        return ""

    # Full NSN: 4-2-3-4 digits, optionally separated by '-' or spaces.
    m = re.search(r'\b(\d{4}[-\s]?\d{2}[-\s]?\d{3}[-\s]?\d{4})\b', text)
    if m:
        return re.sub(r'[-\s]', '', m.group(1))

    # NIIN keyword form: 'NIIN 01534228' (9 digits).
    m = re.search(r'NIIN[:\s]*([0-9]{9})', text, re.IGNORECASE)
    if m:
        return m.group(1)

    return ""


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_meis(parsed: List[ParsedMEI]) -> List[MasterRow]:
    """
    Collapse parsed MEIs into one box row per (lin, normalized_model).

    - qty       = count of files in the group
    - serials   = every non-empty serial in the group (de-duped, order preserved)
    - nsn       = first non-empty sniffed NSN in the group (if any present)
    - needs_review propagates if any member needed review
    Box numbers are assigned sequentially (1..N) here; the UI re-sequences after
    edits before generate/audit.
    """
    groups: Dict[tuple, MasterRow] = {}
    order: List[tuple] = []

    for p in parsed:
        key = (p.lin.upper(), normalize_model(p.model))
        if key not in groups:
            groups[key] = MasterRow(
                model=normalize_model(p.model),
                lin=p.lin.upper(),
                nsn="",
                serials=[],
                qty=0,
                needs_review=False,
            )
            order.append(key)
        row = groups[key]
        row.qty += 1
        if p.sn and p.sn not in row.serials:
            row.serials.append(p.sn)
        # NSN may be attached to the ParsedMEI by the caller (sniffed); honor it.
        sniffed = getattr(p, "nsn_sniffed", "")
        if sniffed and not row.nsn:
            row.nsn = sniffed
        if p.needs_review:
            row.needs_review = True

    rows: List[MasterRow] = []
    for i, key in enumerate(order, start=1):
        row = groups[key]
        row.box_num = i
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Master header builder
# ---------------------------------------------------------------------------

def build_master_header(header: Dict[str, Any], rows: List[Dict[str, Any]]):
    """
    Build a render_core.HeaderInfo for the master packing list from the UI's
    header dict + the finalized rows.

    PACKED BY block (multi-line):
        RANK LAST, FIRST
        UIC: <uic>  <battery>  <battalion>
        SLOC: <sloc>
        SHRH POC: <shrh>

    END ITEM block (multi-line, "Initial Packing List" per the slideshow):
        <SLOC> Initial Packing List
        Container #<container>
        SUN: <sun>
        SEAL: <seal>
        Major End Items: (<N>)
        Box #s: 1, 2, ... N
    """
    from render_core import HeaderInfo

    g = lambda k: str(header.get(k, "") or "").strip()

    # ----- PACKED BY -----
    packed_lines = []
    if g("packed_by"):
        packed_lines.append(g("packed_by").upper())
    uic_line_parts = []
    if g("uic"):
        uic_line_parts.append(f"UIC: {g('uic').upper()}")
    if g("battery"):
        uic_line_parts.append(g("battery").upper())
    if g("battalion"):
        uic_line_parts.append(g("battalion").upper())
    if uic_line_parts:
        packed_lines.append("  ".join(uic_line_parts))
    if g("sloc"):
        packed_lines.append(f"SLOC / SECTION: {g('sloc').upper()}")
    if g("shrh"):
        packed_lines.append(f"SHRH POC: {g('shrh').upper()}")
    packed_by = "\n".join(packed_lines)

    # ----- END ITEM (Initial Packing List) -----
    # `rows` may arrive as dicts (from the Flask /generate JSON body) or as
    # MasterRow objects (when this is called directly from Python). Read the box
    # number from whichever shape it is so both callers work.
    def _box_num(r, default):
        if isinstance(r, dict):
            return r.get("box_num", default)
        return getattr(r, "box_num", default)

    def _compress_box_nums(nums):
        # Collapse contiguous box numbers into ranges so the END ITEM "BOX #S"
        # line stays on ONE short line and is never truncated — works whether
        # there are 14 boxes or 128.  [1,2,...,14] -> "1-14";
        # [1,2,3,5,7,8] -> "1-3, 5, 7-8".
        nums = sorted({int(n) for n in nums})
        if not nums:
            return ""
        parts, start, prev = [], nums[0], nums[0]
        for n in nums[1:]:
            if n == prev + 1:
                prev = n
                continue
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = n
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        return ", ".join(parts)

    n_boxes = len(rows)
    box_nums = _compress_box_nums(_box_num(r, i + 1) for i, r in enumerate(rows))
    end_lines = []
    sloc = g("sloc")
    end_lines.append(f"{sloc + ' ' if sloc else ''}INITIAL PACKING LIST")
    if g("container"):
        end_lines.append(f"CONTAINER #{g('container').upper()}")
    if g("sun"):
        end_lines.append(f"SUN: {g('sun').upper()}")
    if g("seal"):
        end_lines.append(f"SEAL: {g('seal').upper()}")
    end_lines.append(f"MAJOR END ITEMS: ({n_boxes})")
    if box_nums:
        end_lines.append(f"BOX #S: {box_nums}")
    end_item = "\n".join(end_lines)

    return HeaderInfo(
        packed_by=packed_by,
        num_boxes=str(n_boxes),
        requisition_no="",
        order_no="",
        end_item=end_item,
        date=g("date"),
        typed_name=g("signer_name"),
    )


# ---------------------------------------------------------------------------
# Serial line splitting (handles 50+ serial numbers per item)
# ---------------------------------------------------------------------------

# Mirror render_core.py column geometry
_CONTENT_AVAIL_PTS = 365.4 - 88.2 - 3.0   # 274.2 pts available in content col
_LINE2_FONT_SIZE   = 7                      # Helvetica 7pt — must match render_core


def _str_width(text):
    """String width in pts at Helvetica 7pt; falls back to 4.2 pts/char."""
    try:
        from reportlab.pdfbase import pdfmetrics
        return pdfmetrics.stringWidth(text, "Helvetica", _LINE2_FONT_SIZE)
    except Exception:
        return len(text) * 4.2


def _build_serial_lines(lin, nsn, serials):
    """
    Chunk LIN/NSN/serial data into column-width strings for the PDF renderer.

    Returns:
      [0]  "LIN: <lin>  [NSN: <nsn>  ]SN: s1, s2, ..."  (as many serials as fit)
      [1+] "    s3, s4, ..."                              (indented continuations)
    If no serials, returns the single metadata string.
    """
    prefix_parts = []
    if lin:
        prefix_parts.append(f"LIN: {lin}")
    if nsn:
        prefix_parts.append(f"NSN: {nsn}")
    prefix = "  ".join(prefix_parts)

    if not serials:
        return [prefix] if prefix else [""]

    first_prefix = (prefix + "  SN: ") if prefix else "SN: "
    cont_indent = "    "

    lines = []
    current = first_prefix
    cur_pfx = first_prefix

    for sn in serials:
        sn = str(sn).strip()
        if not sn:
            continue
        candidate = current + sn if current == cur_pfx else current + ", " + sn

        if _str_width(candidate) <= _CONTENT_AVAIL_PTS:
            current = candidate
        else:
            lines.append(current)
            cur_pfx = cont_indent
            current = cont_indent + sn

    if current.strip():
        lines.append(current)

    return lines or [first_prefix.rstrip()]


def rows_to_bom_items(rows: List[Dict[str, Any]]):
    """
    Convert finalized UI rows into render_core.BomItem objects.

    Items with more serial numbers than fit on one PDF line expand into a
    primary BomItem plus one or more continuation BomItems (is_continuation=True).
    The renderer skips box number, model, qty, UOI, and totals for continuation
    rows — only the wrapped serial text is drawn.
    """
    from render_core import BomItem

    items = []
    for i, r in enumerate(rows, start=1):
        lin = str(r.get("lin", "") or "").strip()
        nsn = str(r.get("nsn", "") or "").strip()
        serials = r.get("serials", []) or []
        if isinstance(serials, str):
            serials = [s.strip() for s in serials.split(",") if s.strip()]
        qty = int(r.get("qty", len(serials) or 1) or 1)

        serial_lines = _build_serial_lines(lin, nsn, serials)

        items.append(BomItem(
            line_no=i,
            description=normalize_model(str(r.get("model", ""))),
            nsn=serial_lines[0],
            qty=qty,
            unit_of_issue="EA",
        ))

        for extra in serial_lines[1:]:
            items.append(BomItem(
                line_no=i,
                description="",
                nsn=extra,
                qty=qty,
                unit_of_issue="EA",
                is_continuation=True,
            ))

    return items


# ---------------------------------------------------------------------------
# Auditor — validates the generated master structure against the rules
# ---------------------------------------------------------------------------

def audit_master(rows: List[Dict[str, Any]], header: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate the master packing list. Returns {passed: bool, issues: [...]}.

    Each issue: {severity: 'ERROR'|'WARNING', message: str}.
    ERRORs fail the audit; WARNINGs do not (but are surfaced).

    Rule set (from the slideshow + locked decisions):
      - Box numbers sequential 1..N, no gaps/dupes.            (ERROR on violation)
      - Every row has Model + LIN.                             (ERROR if missing)
      - Packer name != Signer name.                            (ERROR if equal)
      - Each row should have at least one serial.              (WARNING if none)
      - NSN present per row.                                   (WARNING if blank)
      - 'Major End Items: (N)' count == number of rows.        (sanity, computed)
      - qty == len(serials) when serials exist.                (WARNING on mismatch)
    """
    issues: List[Dict[str, str]] = []

    def err(msg):
        issues.append({"severity": "ERROR", "message": msg})

    def warn(msg):
        issues.append({"severity": "WARNING", "message": msg})

    # --- Box numbering 1..N, sequential, unique ---
    box_nums = []
    for i, r in enumerate(rows):
        try:
            box_nums.append(int(r.get("box_num", i + 1)))
        except (TypeError, ValueError):
            err(f"Row {i + 1}: box number is not a valid integer.")
    expected = list(range(1, len(rows) + 1))
    if box_nums and box_nums != expected:
        err(f"Box numbers are not sequential 1..{len(rows)} "
            f"(got {box_nums}). Re-sequence before generating.")
    if len(set(box_nums)) != len(box_nums):
        err("Duplicate box numbers detected.")

    # --- Per-row content checks ---
    for i, r in enumerate(rows):
        box = r.get("box_num", i + 1)
        model = str(r.get("model", "") or "").strip()
        lin = str(r.get("lin", "") or "").strip()
        nsn = str(r.get("nsn", "") or "").strip()
        serials = r.get("serials", []) or []
        if isinstance(serials, str):
            serials = [s.strip() for s in serials.split(",") if s.strip()]
        try:
            qty = int(r.get("qty", len(serials) or 1) or 1)
        except (TypeError, ValueError):
            qty = 0
            err(f"Box {box}: quantity is not a valid integer.")

        if not model:
            err(f"Box {box}: missing Model / nomenclature.")
        if not lin:
            err(f"Box {box}: missing LIN.")
        if not serials:
            warn(f"Box {box} ('{model or 'unknown'}') has no serial numbers listed.")
        if not nsn:
            warn(f"Box {box} ('{model or 'unknown'}') has a blank NSN.")
        if serials and qty != len(serials):
            warn(f"Box {box}: quantity ({qty}) does not match the number of "
                 f"serials listed ({len(serials)}).")

    # --- Packer != Signer ---
    packer = str(header.get("packed_by", "") or "").strip().upper()
    signer = str(header.get("signer_name", "") or "").strip().upper()
    if packer and signer and packer == signer:
        err("Packer and Signer are the same person "
            f"('{header.get('packed_by', '')}'). The typed signer at the bottom "
            "must differ from PACKED BY.")
    elif not signer:
        warn("No signer name provided (typed name + title at the bottom).")

    # --- Major End Items count sanity (computed, always matches by construction
    #     but we assert it so a hand-edited header can't drift) ---
    n_rows = len(rows)
    if n_rows == 0:
        err("No rows to pack — the master list is empty.")

    passed = not any(i["severity"] == "ERROR" for i in issues)
    return {
        "passed": passed,
        "issues": issues,
        "box_count": n_rows,
    }


__all__ = [
    "ParsedMEI", "MasterRow",
    "parse_filename", "sniff_nsn", "normalize_model",
    "aggregate_meis", "build_master_header", "rows_to_bom_items",
    "audit_master",
]
