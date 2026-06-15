"""
bom_parser.py — Extract end-item metadata + component list from a GCSS-Army BOM AcroForm PDF.

GCSS-Army BOMs are AcroForm PDFs where all content lives in form field annotations,
not in the text stream. Data is encoded in field tooltips (/TU) in document order:

    MATERIAL field  - tooltip starts with 9-char NIIN
    WTY/DESC field  - tooltip starts with WTY_, 9_, or "Description "
    OH Qty field    - name contains "OH Qty"; value is the on-hand count

Ports the core state machine from v25/dd1750_core.py extract_items_from_form_fields().
"""

import os
import re
from dataclasses import dataclass, field
from typing import List

from pypdf import PdfReader
from master_core import parse_filename


@dataclass
class ComponentItem:
    nsn: str
    description: str
    oh_qty: int


@dataclass
class BomRecord:
    lin: str
    niin: str
    desc: str
    serial: str
    sloc: str
    source_file: str
    components: List[ComponentItem] = field(default_factory=list)

    def to_dict(self):
        return {
            "lin": self.lin,
            "niin": self.niin,
            "desc": self.desc,
            "serial": self.serial,
            "sloc": self.sloc,
            "source_file": self.source_file,
            "component_count": len(self.components),
            "components": [
                {"nsn": c.nsn, "description": c.description, "oh_qty": c.oh_qty}
                for c in self.components
            ],
        }


# Regex for NIIN at start of tooltip (9 digits, or alphanumeric NIIN variants)
_NIIN_RE = re.compile(
    r'^(\d{9}|\d{2}[A-Z]\d{6}|\d{2}[A-Z]{2}\d{5}|\d{3}[A-Z]\d{5})\b'
)
# Category header pattern — COEI-XXXXX or BII-XXXXX tooltips should be skipped
_IS_CATEGORY_RE = re.compile(r'\b(COEI|BII)-\d', re.IGNORECASE)


def parse_bom_pdf(pdf_path: str) -> BomRecord:
    """
    Extract end-item metadata and component list from a GCSS-Army BOM PDF.
    Components with OH Qty == 0 are filtered out.
    """
    basename = os.path.basename(pdf_path)

    # Filename gives us LIN + description fallback
    parsed = parse_filename(basename)
    lin = parsed.lin
    desc = parsed.model
    serial_from_filename = parsed.sn

    try:
        reader = PdfReader(pdf_path)
        fields = reader.get_fields() or {}
    except Exception:
        fields = {}

    pending_nsn = ""
    components: List[ComponentItem] = []
    last_item_idx = -1
    niin = ""
    serial = ""
    sloc = ""

    for fname, field_obj in fields.items():
        tooltip = str(field_obj.get('/TU', '') or '').strip()
        fvalue = str(field_obj.get('/V', '') or '').strip()
        fname = str(fname).strip()

        # --- Metadata fields: grab serial + SLOC, skip the rest ---
        # Serial number lives in the 'undefined' field value
        if fname == 'undefined' and fvalue:
            serial = fvalue
            continue
        # SLOC
        if fname == 'SLOC' and fvalue:
            sloc = fvalue
            continue
        # COEI{NIIN} field name → end item NIIN (9 chars after "COEI")
        if fname.upper().startswith('COEI') and len(fname) >= 13:
            niin = fname[4:13]
            continue
        # Skip other known metadata field names
        if fname in {'TO', 'FROM', 'DATE', 'GRADE', 'SIGNATURE', 'PUB NUM', 'PUB/BOM', 'EA'}:
            continue

        # --- Skip category header fields ---
        if _IS_CATEGORY_RE.search(tooltip) or _IS_CATEGORY_RE.search(fname):
            pending_nsn = ""
            continue

        # --- MATERIAL field: tooltip/name starts with NIIN ---
        mat = _NIIN_RE.match(tooltip) or _NIIN_RE.match(fname)
        if mat:
            pending_nsn = mat.group(1)
            continue

        # Part-number-only fields (no NIIN): reset pending NSN, skip
        if re.match(r'^[A-Z0-9][\w\-]+\s*:\s*C[_ ]\w+', tooltip) or \
           re.match(r'^[A-Z0-9][\w\-]+\s*:\s*C[_ ]\w+', fname):
            pending_nsn = ""
            continue

        # --- Description fields: WTY_ / 9_ / Description prefix ---
        desc_prefix = None
        if tooltip.startswith('WTY_'):
            desc_prefix = 'WTY_'
        elif tooltip.startswith('9_'):
            desc_prefix = '9_'
        elif tooltip.lower().startswith('description '):
            desc_prefix = tooltip[:len('description ')]  # preserve original case length

        if desc_prefix is not None:
            raw = tooltip[len(desc_prefix):].strip()
            cleaned = _clean_desc(raw)
            if not cleaned or len(cleaned) < 3:
                continue
            if _IS_CATEGORY_RE.search(cleaned):
                continue

            components.append(ComponentItem(nsn=pending_nsn, description=cleaned, oh_qty=0))
            last_item_idx = len(components) - 1
            pending_nsn = ""
            continue

        # --- OH Qty field: attach quantity to most recent component ---
        if ('OH Qty' in fname or 'OH Qty' in tooltip or
                'oh_qty' in fname.lower() or 'oh qty' in tooltip.lower()):
            if last_item_idx >= 0 and fvalue:
                try:
                    qty = int(float(fvalue)) if fvalue.strip() else 0
                    components[last_item_idx].oh_qty = qty
                except (ValueError, TypeError):
                    pass
            continue

    # Only keep components that are actually on-hand
    components = [c for c in components if c.oh_qty > 0]

    # Serial fallback to filename parse
    if not serial:
        serial = serial_from_filename

    return BomRecord(
        lin=lin,
        niin=niin,
        desc=desc,
        serial=serial,
        sloc=sloc,
        source_file=basename,
        components=components,
    )


def _clean_desc(text: str) -> str:
    """
    Clean description text: collapse whitespace, strip duplicate halves.
    GCSS tooltips often encode 'SHORT,NAME SHORT NAME...' doubling the text.
    """
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r',+', ',', text)

    # Remove duplicate-first-word prefix (v25 pattern)
    parts = text.split()
    if len(parts) > 1:
        first_word = parts[0].replace(',', '').upper()
        for i, part in enumerate(parts[1:], 1):
            if part.replace(',', '').upper() == first_word:
                text = ' '.join(parts[:i])
                break

    return text.strip()
