"""
render_core.py — DD1750 PDF rendering (copied/adapted from v25's dd1750_core.py)

This module is lifted almost verbatim from the PROVEN v25 renderer. It draws
two-line table rows onto a flattened blank DD1750 template and paginates at
18 rows/page. The ONLY new piece is the `draw_master_header_fn` hook + the
`draw_master_header()` function, which lets the master tool draw its
multi-line PACKED BY / END ITEM header blocks (the master layout has more
lines than v25's single-item layout).

Everything else here is v25's code, unchanged, so rendering stays a known
quantity. The coordinates were tuned against the real blank_1750.pdf template.
"""

import io
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas


# DD1750 Form Layout Constants (Letter size: 612 x 792 points)
# These measurements are from the official DD FORM 1750, SEP 70 (EG).
# Copied verbatim from v25 — proven coordinates.
ROWS_PER_PAGE = 18
PAGE_W, PAGE_H = 612.0, 792.0

# Column boundaries (x coordinates in points from left edge)
X_BOX_L, X_BOX_R = 45.0, 88.2           # Box Number column
X_CONTENT_L, X_CONTENT_R = 88.2, 365.4  # Contents (Stock # and Nomenclature)
X_UOI_L, X_UOI_R = 365.4, 408.6         # Unit of Issue
X_INIT_L, X_INIT_R = 408.6, 453.6       # Initial Operation
X_SPARES_L, X_SPARES_R = 453.6, 514.8   # Running Spares
X_TOTAL_L, X_TOTAL_R = 514.8, 567.0     # Total

# Row layout (PDF coordinates: 0 at bottom)
Y_TABLE_TOP = 616.0      # Top of table content area
Y_TABLE_BOTTOM = 89.1    # Bottom of table content area
ROW_H = (Y_TABLE_TOP - Y_TABLE_BOTTOM) / ROWS_PER_PAGE  # ~29.27 points
PAD_X = 3.0  # Horizontal padding from column edge


@dataclass
class BomItem:
    """
    One renderable table row.

    NOTE: in the master tool we REPURPOSE the `nsn` field to carry the full
    second-line text ("LIN: ...  NSN: ...  SN: ...") so we can reuse v25's
    two-line row renderer without touching its drawing logic.
    """
    line_no: int = 0
    description: str = ""    # Line 1 of the row (model/nomenclature)
    nsn: str = ""            # Line 2 of the row (LIN/NSN/SN text in master mode)
    qty: int = 1
    unit_of_issue: str = "EA"
    is_continuation: bool = False  # overflow serial row — skip box/qty/UOI/total


@dataclass
class HeaderInfo:
    """Header information for the DD1750 form (multi-line strings allowed)."""
    packed_by: str = ""
    num_boxes: str = "1"
    requisition_no: str = ""
    order_no: str = ""
    end_item: str = ""
    date: str = ""
    typed_name: str = ""   # signer name + title, drawn in the bottom TYPED NAME box
    # Page numbers are auto-calculated during rendering.


def draw_master_header(can, header, page_num, total_pages):
    """
    Draw the master packing list header blocks on the reportlab canvas.

    COORDINATES ARE DERIVED FROM THE REAL FORM CELLS. We measured the
    pre-printed cell dividers in blank_1750.pdf (vertical lines at x=45, 185.4,
    311.4, 408.6, 567; horizontal divider between the PACKED BY row and the
    END ITEM row at reportlab y≈701.6). Data is placed INSIDE its cell, below
    the printed label, so nothing overlaps the form labels. This mirrors the
    sample "1750 Initial Packing List Example.pdf".

    Cell map (reportlab y measured from the bottom of the page):
      PACKED BY / TITLE row  : y ≈ 701.6 .. 750.6
        - "PACKING LIST" title cell : x 45 .. 185      (pre-printed, leave alone)
        - PACKED BY data cell       : x 185.4 .. 311.4 <-- packed_by goes here
        - "1. NO. BOXES" cell        : x 311.4 .. 408.6 <-- num_boxes goes here
      END ITEM row           : y ≈ 652.7 .. 701.6
        - END ITEM cell (wide)       : x 45 .. 408.6   <-- end_item goes here
        - "4. DATE" cell             : x 408.6 .. 567  <-- date goes here

    The "PACKED BY" label sits at the top of its cell (~y 740), so we start the
    data one line below it. The END ITEM cell uses a two-column layout to match
    the example: descriptive lines (Initial Packing List / Container / SUN /
    SEAL) on the left, and the "Major End Items (N)" + "Box #s" summary in a
    second column on the right — this keeps the block short so it never spills
    past the cell's bottom divider.
    """
    # --- PACKED BY block: inside cell B (x 185.4..311.4), below the label ---
    # The "PACKED BY" label occupies the first line (~y 740). We start the
    # typed data just under it and step down 9.5pt per line. 4 lines fit
    # comfortably between y≈730 and y≈702 (the cell bottom divider).
    if header.packed_by:
        lines = header.packed_by.split('\n')
        can.setFont("Helvetica", 7)
        x = 188            # left padding inside the PACKED BY cell (cell starts 185.4)
        y = 734            # first data line, just below the "PACKED BY" label
        for line in lines[:4]:
            can.drawString(x, y, line[:30])   # cell is ~123pt wide → ~30 chars at 7pt
            y -= 9.0       # tighter step so all 4 lines clear the cell's bottom divider

    # --- Number of Boxes: centered in cell C (x 311.4..408.6) ---
    if header.num_boxes:
        can.setFont("Helvetica", 10)
        # Center the value in the NO. BOXES cell, below its label
        box_cx = (311.4 + 408.6) / 2
        can.drawCentredString(box_cx, 725, str(header.num_boxes)[:6])

    # --- END ITEM block: inside the wide cell (x 45..408.6), below the label ---
    # The "3. END ITEM" label is at the top of the cell (~y 691). We start the
    # data just below it (~y 681). We split the 6 logical lines into two columns
    # so the block stays short (the example does the same):
    #   Left column  (x≈100): the descriptive/identifier lines
    #   Right column (x≈250): the "Major End Items (N)" + "Box #s" summary
    if header.end_item:
        all_lines = header.end_item.split('\n')
        # Separate the summary lines (Major End Items / Box #s) from the rest.
        left_lines, right_lines = [], []
        for ln in all_lines:
            upper = ln.upper()
            if upper.startswith("MAJOR END ITEMS") or upper.startswith("BOX #"):
                right_lines.append(ln)
            else:
                left_lines.append(ln)

        can.setFont("Helvetica", 7)
        # Left column: Initial Packing List / Container / SUN / SEAL.
        # Start higher (685) + tighter step (8.5) so the 4th line (SEAL) clears
        # the cell's bottom divider (~652.7); x=47 aligns to the cell's left edge.
        y = 685
        for line in left_lines[:5]:
            can.drawString(47, y, line[:50])
            y -= 8.5
        # Right column: Major End Items (N) / Box #s — aligned to the right half
        y = 685
        for line in right_lines[:2]:
            # Cell is ~158pt wide → ~40 chars at 7pt. The box list is
            # range-compressed upstream (e.g. "1-14") so it fits on one line;
            # this wider clip is just a guard against silent truncation.
            can.drawString(232, y, line[:40])
            y -= 8.5

    # --- Date (block 4): inside the DATE cell (x 408.6..567), below "4. DATE" ---
    # The "4. DATE" label sits at x≈411..441, y≈691. We place the value one line
    # below it so the two don't collide.
    if header.date:
        can.setFont("Helvetica", 9)
        can.drawString(450, 680, str(header.date)[:20])

    # --- Typed name + title (block 6): bottom-left "TYPED NAME AND TITLE" box ---
    # The signer (who must differ from the packer). The form's typed-name box is
    # ~x 92..290, y 46..60; we draw the text just inside it, below the label.
    if getattr(header, "typed_name", ""):
        can.setFont("Helvetica", 8)
        can.drawString(47, 58, str(header.typed_name)[:45])


def generate_dd1750_overlay(
    items: List[BomItem],
    page_num: int,
    total_pages: int,
    header: Optional[HeaderInfo] = None,
    draw_master_header_fn=None,
) -> io.BytesIO:
    """
    Generate a PDF overlay with item data for a single DD1750 page.

    Copied from v25 with ONE change: when `draw_master_header_fn` is provided,
    we call it to draw the header instead of v25's single-line header block.
    This keeps v25's row/footer drawing identical while swapping in the
    master header layout.

    Args:
        items: List of items for this page (max 18)
        page_num: Current page number (1-based)
        total_pages: Total number of pages
        header: Optional header information
        draw_master_header_fn: Optional callable(can, header, page_num, total_pages)
            that draws the master-style header. If None, v25's single-line
            header drawing is used.

    Returns:
        BytesIO buffer containing the overlay PDF
    """
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=(PAGE_W, PAGE_H))

    # === PAGE NUMBERS — always drawn as static text ===
    can.setFont("Helvetica", 10)
    can.drawCentredString(472, PAGE_H - 132, str(page_num))      # Current page
    can.drawCentredString(520, PAGE_H - 132, str(total_pages))   # Total pages

    # === HEADER ===
    if header is not None:
        if draw_master_header_fn is not None:
            # Master mode: delegate to the injected header-drawing function.
            draw_master_header_fn(can, header, page_num, total_pages)
        else:
            # v25 mode: single-line header fields (unchanged from v25).
            if header.packed_by:
                can.setFont("Helvetica", 9)
                can.drawString(95, 736, header.packed_by[:60])
            if header.num_boxes:
                can.setFont("Helvetica", 9)
                can.drawString(285, 736, str(header.num_boxes)[:10])
            if header.requisition_no:
                can.setFont("Helvetica", 9)
                can.drawString(408, 736, str(header.requisition_no)[:30])
            if header.order_no:
                can.setFont("Helvetica", 9)
                can.drawString(408, 716, str(header.order_no)[:30])
            if header.end_item:
                can.setFont("Helvetica", 8)
                lines = header.end_item.split('\n')
                y_top = 696
                for i, line in enumerate(lines[:3]):
                    can.drawString(95, y_top - (i * 10), line[:55])
            if header.date:
                can.setFont("Helvetica", 9)
                can.drawString(450, 696, str(header.date)[:20])

    # === TABLE CONTENT ===
    for i, item in enumerate(items):
        # Calculate Y position for this row (rows go top to bottom)
        row_top = Y_TABLE_TOP - (i * ROW_H)
        y_line1 = row_top - 10.0    # First line (description)
        y_line2 = row_top - 20.0    # Second line (NSN / master line-2 text)

        if not item.is_continuation:
            # Box number (centered) — in master mode this is the box number
            can.setFont("Helvetica", 9)
            box_center_x = (X_BOX_L + X_BOX_R) / 2
            can.drawCentredString(box_center_x, y_line1, str(item.line_no))

            # Line 1: description / model (left-aligned with padding)
            can.setFont("Helvetica", 8)
            desc = item.description[:55] if len(item.description) > 55 else item.description
            can.drawString(X_CONTENT_L + PAD_X, y_line1, desc)

            # Unit of Issue (centered) — always EA
            can.setFont("Helvetica", 9)
            uoi_center_x = (X_UOI_L + X_UOI_R) / 2
            can.drawCentredString(uoi_center_x, y_line1, item.unit_of_issue or "EA")

            # Initial Operation quantity (d) — centered
            init_center_x = (X_INIT_L + X_INIT_R) / 2
            can.drawCentredString(init_center_x, y_line1, str(item.qty))

            # Running Spares (e) — always 0 for an initial packing list
            spares_center_x = (X_SPARES_L + X_SPARES_R) / 2
            can.drawCentredString(spares_center_x, y_line1, "0")

            # Total (f = d + e) — centered
            total_center_x = (X_TOTAL_L + X_TOTAL_R) / 2
            can.drawCentredString(total_center_x, y_line1, str(item.qty))

        # Line 2: NSN/SN text — drawn for all rows (primary and continuation).
        # Pre-split by rows_to_bom_items() so each chunk already fits the column.
        if item.nsn:
            can.setFont("Helvetica", 7)
            line2_text = item.nsn
            if not item.is_continuation and not line2_text.upper().startswith(("LIN:", "NSN:", "SN:")):
                line2_text = f"NSN: {line2_text}"
            can.drawString(X_CONTENT_L + PAD_X, y_line2, line2_text)

    # === "NOTHING FOLLOWS" MARKER (verbatim from v25) ===
    # Drawn on the last page, on the row immediately after the last item.
    if page_num == total_pages and len(items) < ROWS_PER_PAGE:
        marker_row_top = Y_TABLE_TOP - (len(items) * ROW_H)
        marker_y = marker_row_top - 10.0
        marker_center_x = (X_CONTENT_L + X_CONTENT_R) / 2
        can.setFont("Helvetica-Bold", 8)
        can.drawCentredString(
            marker_center_x,
            marker_y,
            "------------------- NOTHING FOLLOWS -------------------"
        )

    can.save()
    packet.seek(0)
    return packet


def generate_dd1750_from_items(
    items: List[BomItem],
    template_path: str,
    output_path: str,
    header: Optional[HeaderInfo] = None,
    draw_master_header_fn=None,
) -> Tuple[str, int]:
    """
    Generate a DD1750 PDF from a list of items.

    Copied from v25 with the `draw_master_header_fn` parameter threaded through
    to the overlay generator. For the master tool we render a FINAL document
    (everything pre-drawn on the canvas) so we don't add editable form fields.

    Args:
        items: List of BomItem objects (already in box order)
        template_path: Path to blank DD1750 template PDF
        output_path: Path for output PDF
        header: Optional header information
        draw_master_header_fn: Optional master header drawing callable

    Returns:
        Tuple of (output_path, item_count)
    """
    if not items:
        # Return blank template if no items
        reader = PdfReader(template_path)
        writer = PdfWriter()
        writer.add_page(reader.pages[0])
        with open(output_path, 'wb') as f:
            writer.write(f)
        return output_path, 0

    total_pages = math.ceil(len(items) / ROWS_PER_PAGE)
    writer = PdfWriter()

    for page_num in range(total_pages):
        start_idx = page_num * ROWS_PER_PAGE
        end_idx = min((page_num + 1) * ROWS_PER_PAGE, len(items))
        page_items = items[start_idx:end_idx]

        # Generate overlay with header info, passing the master header hook down
        overlay_buffer = generate_dd1750_overlay(
            page_items,
            page_num + 1,
            total_pages,
            header,
            draw_master_header_fn=draw_master_header_fn,
        )
        overlay = PdfReader(overlay_buffer)

        # Merge overlay onto a fresh copy of the template page
        template_page = PdfReader(template_path).pages[0]
        template_page.merge_page(overlay.pages[0])
        writer.add_page(template_page)

    with open(output_path, 'wb') as f:
        writer.write(f)

    return output_path, len(items)


def format_packed_by(name: str, rank: str = "", unit: str = "") -> str:
    """
    Format packer info into a single-line PACKED BY string. (Copied from v25.)

    Examples:
        format_packed_by("John A. Smith", "CPT", "B BTY 1-1 ADA")
            -> "CPT JOHN A. SMITH, B BTY 1-1 ADA"
    """
    name = (name or "").strip()
    rank = (rank or "").strip()
    unit = (unit or "").strip()

    parts = []
    if rank:
        parts.append(rank.upper())
    if name:
        parts.append(name.upper())
    head = ' '.join(parts)

    if unit:
        return f"{head}, {unit.upper()}" if head else unit.upper()
    return head


def format_end_item(nomenclature: str, model: str = "", serial_number: str = "") -> str:
    """
    Format end-item info as a three-line block. (Copied from v25, kept for reuse.)
    """
    return (
        f"NOMENCLATURE: {nomenclature or ''}\n"
        f"MODEL: {model or ''}\n"
        f"SERIAL NUMBER: {serial_number or ''}"
    )


__all__ = [
    'BomItem', 'HeaderInfo',
    'generate_dd1750_overlay', 'generate_dd1750_from_items',
    'draw_master_header', 'format_packed_by', 'format_end_item',
    'ROWS_PER_PAGE',
]
