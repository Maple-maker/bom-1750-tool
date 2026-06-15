"""
app.py — Flask web app for the BOM DD1750 tool.

Routes:
  GET  /              -> single-page UI
  POST /upload-boms   -> parse batch of GCSS-Army BOM PDFs; return JSON
  POST /generate-boms -> generate individual + master DD1750s; stream ZIP
  GET  /api/health    -> liveness probe
"""

import io
import os
import tempfile
import zipfile

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import master_core
import render_core
from bom_parser import parse_bom_pdf

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PDF = os.path.join(BASE_DIR, "blank_1750.pdf")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "bom-1750-tool",
        "template_present": os.path.exists(TEMPLATE_PDF),
    })


@app.route("/upload-boms", methods=["POST"])
def upload_boms():
    """
    Accept multipart 'files' (one or many BOM PDFs).
    Returns JSON with parsed BOM records.
    """
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded (expected form field 'files')."}), 400

    boms = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for f in files:
            if not f or not f.filename:
                continue
            if not f.filename.lower().endswith(".pdf"):
                continue
            safe = secure_filename(f.filename) or "upload.pdf"
            disk_path = os.path.join(tmpdir, safe)
            f.save(disk_path)
            try:
                record = parse_bom_pdf(disk_path)
                boms.append(record.to_dict())
            except Exception as e:
                boms.append({"error": str(e), "source_file": f.filename})

    if not boms:
        return jsonify({"error": "No PDF files found in the upload."}), 400

    return jsonify({"boms": boms, "bom_count": len(boms)})


@app.route("/generate-boms", methods=["POST"])
def generate_boms():
    """
    Body: {"boms": [...], "header": {...}}
    Generates:
      - One individual DD1750 per BOM (components as line items)
      - One master DD1750 (end items grouped by LIN)
    Returns as a ZIP download.
    """
    data = request.get_json(silent=True) or {}
    boms = data.get("boms", [])
    header = data.get("header", {})

    if not boms:
        return jsonify({"error": "No BOMs provided."}), 400

    zip_buf = io.BytesIO()

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:

            # --- Individual DD1750 per BOM ---
            for bom in boms:
                components = bom.get("components", [])
                items = [
                    render_core.BomItem(
                        line_no=i + 1,
                        description=c["description"],
                        nsn=c["nsn"],
                        qty=c["oh_qty"],
                        unit_of_issue="EA",
                    )
                    for i, c in enumerate(components)
                ]

                end_item_text = "\n".join([
                    f"{bom.get('lin', '')} {bom.get('desc', '')}",
                    f"SN: {bom.get('serial', '')}",
                    f"SLOC: {bom.get('sloc', '')}",
                ])
                header_info = render_core.HeaderInfo(
                    packed_by=header.get("packed_by", ""),
                    num_boxes="1",
                    date=header.get("date", ""),
                    typed_name=header.get("signer_name", ""),
                    end_item=end_item_text,
                )

                safe_name = bom.get("serial") or bom.get("lin") or "UNKNOWN"
                # Sanitize for filesystem
                safe_name = "".join(c for c in safe_name if c.isalnum() or c in "-_")
                out_path = os.path.join(tmpdir, f"{safe_name}_DD1750.pdf")
                render_core.generate_dd1750_from_items(items, TEMPLATE_PDF, out_path, header_info)
                zf.write(out_path, f"individual/{safe_name}_DD1750.pdf")

            # --- Master DD1750: group by LIN ---
            lin_groups = {}
            for bom in boms:
                lin = bom.get("lin") or "UNKNOWN"
                if lin not in lin_groups:
                    lin_groups[lin] = {
                        "lin": lin,
                        "desc": bom.get("desc", ""),
                        "serials": [],
                        "nsn": bom.get("niin", ""),
                    }
                sn = bom.get("serial", "")
                if sn and sn not in lin_groups[lin]["serials"]:
                    lin_groups[lin]["serials"].append(sn)

            master_rows_dicts = []
            for i, (lin, grp) in enumerate(lin_groups.items(), 1):
                master_rows_dicts.append({
                    "box_num": i,
                    "model": grp["desc"],
                    "lin": lin,
                    "nsn": grp["nsn"],
                    "serials": grp["serials"],
                    "qty": len(grp["serials"]) or 1,
                    "needs_review": False,
                })

            master_items = master_core.rows_to_bom_items(master_rows_dicts)
            master_header = master_core.build_master_header(header, master_rows_dicts)

            master_path = os.path.join(tmpdir, "Master_DD1750.pdf")
            render_core.generate_dd1750_from_items(
                master_items,
                TEMPLATE_PDF,
                master_path,
                header=master_header,
                draw_master_header_fn=render_core.draw_master_header,
            )
            zf.write(master_path, "Master_DD1750.pdf")

    zip_buf.seek(0)
    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="DD1750s.zip",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
