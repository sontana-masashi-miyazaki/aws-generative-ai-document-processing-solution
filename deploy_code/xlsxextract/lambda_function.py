import hashlib
import io
import json
import os
import posixpath
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

import boto3


S3 = boto3.client("s3")

_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REF_RE = re.compile(r"^([A-Z]+)([0-9]+)$")
_NOISE_HEADER_RE = re.compile(r"^(?:unnamed(?::\s*\d+)?|index)$", re.IGNORECASE)


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def _qname(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


def _safe_int(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _norm_prefix(prefix: Optional[str]) -> str:
    return (prefix or "").strip("/")


def _parse_s3_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"Invalid s3 uri: {uri}")
    no_scheme = uri[5:]
    bucket, _, key = no_scheme.partition("/")
    if not bucket or not key:
        raise ValueError(f"Invalid s3 uri: {uri}")
    return bucket, key


def _get_source_bucket_key(event: Dict[str, Any]) -> Tuple[str, str]:
    if event.get("source_s3_uri"):
        return _parse_s3_uri(event["source_s3_uri"])
    if event.get("source_bucket") and event.get("source_key"):
        return event["source_bucket"], event["source_key"]
    raise KeyError("Missing source_bucket/source_key or source_s3_uri")


def _source_filename_segment(source_key: str) -> str:
    filename = posixpath.basename((source_key or "").rstrip("/")) or "source"
    filename = unicodedata.normalize("NFKC", filename)
    filename = filename.replace("/", "_").replace("\\", "_")
    filename = "".join(ch if ch >= " " and ch != "\x7f" else "_" for ch in filename)
    return filename.strip() or "source"


def _compute_work_prefix(event: Dict[str, Any]) -> str:
    work_prefix = event.get("work_prefix")
    if work_prefix:
        return _norm_prefix(work_prefix)

    pipeline_version = event["pipeline_version"]
    document_id = event["document_id"]
    hash_prefix = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:2]
    _source_bucket, source_key = _get_source_bucket_key(event)
    source_filename = event.get("source_filename") or _source_filename_segment(source_key)
    return f"work/{pipeline_version}/{hash_prefix}/{source_filename}/{document_id}"


def _structured_prefix(event: Dict[str, Any]) -> str:
    return _norm_prefix(event.get("structured_prefix")) or f"{_compute_work_prefix(event)}/structured"


def _assets_images_prefix(event: Dict[str, Any]) -> str:
    return _norm_prefix(event.get("assets_images_prefix")) or f"{_compute_work_prefix(event)}/assets/images"


def _classification(event: Dict[str, Any]) -> Any:
    return event.get("classification")


def _put_json(bucket: str, key: str, doc: Any) -> None:
    S3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def _put_jsonl(bucket: str, key: str, records: List[Dict[str, Any]]) -> None:
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    if body:
        body += "\n"

    S3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson; charset=utf-8",
    )


def _read_xml(z: zipfile.ZipFile, name: str) -> Optional[ET.Element]:
    try:
        with z.open(name) as f:
            return ET.fromstring(f.read())
    except KeyError:
        return None


def _rels_map(root: Optional[ET.Element]) -> Dict[str, Dict[str, str]]:
    """Return map: relId -> {target, type}."""
    if root is None:
        return {}

    out: Dict[str, Dict[str, str]] = {}
    for rel in root.findall(f".//{_qname(_PKG_REL_NS, 'Relationship')}"):
        rid = rel.get("Id")
        if not rid:
            continue
        out[rid] = {
            "target": rel.get("Target", ""),
            "type": rel.get("Type", ""),
        }
    return out


def _resolve_rel(base_path: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    base_dir = posixpath.dirname(base_path)
    return posixpath.normpath(posixpath.join(base_dir, target))


def _shared_strings(z: zipfile.ZipFile) -> List[str]:
    root = _read_xml(z, "xl/sharedStrings.xml")
    if root is None:
        return []

    strings: List[str] = []
    for si in root.findall(".//{*}si"):
        parts: List[str] = []
        for child in list(si):
            child_name = _local_name(child.tag)
            if child_name == "t":
                parts.append(child.text or "")
            elif child_name == "r":
                parts.extend(t.text or "" for t in child.findall(".//{*}t"))
        strings.append("".join(parts))
    return strings


def _cell_text(c: ET.Element, shared: List[str]) -> Optional[str]:
    t = c.get("t")
    if t == "s":
        v = c.find("{*}v")
        idx = _safe_int(v.text if v is not None else None)
        if idx is None or idx < 0 or idx >= len(shared):
            return None
        return shared[idx]

    if t == "inlineStr":
        is_el = c.find("{*}is")
        parts: List[str] = []
        if is_el is not None:
            for child in list(is_el):
                child_name = _local_name(child.tag)
                if child_name == "t":
                    parts.append(child.text or "")
                elif child_name == "r":
                    parts.extend(t_el.text or "" for t_el in child.findall(".//{*}t"))
        s = "".join(parts).strip()
        return s or None

    v = c.find("{*}v")
    if v is None or v.text is None:
        return None

    s = v.text.strip()
    return s or None


def _workbook_sheets(z: zipfile.ZipFile) -> List[Dict[str, str]]:
    wb = _read_xml(z, "xl/workbook.xml")
    wb_rels = _rels_map(_read_xml(z, "xl/_rels/workbook.xml.rels"))
    if wb is None:
        return []

    sheets: List[Dict[str, str]] = []
    for sh in wb.findall(".//{*}sheet"):
        name = sh.get("name") or ""
        rid = sh.get(_qname(_REL_NS, "id")) or sh.get("r:id") or ""
        target = wb_rels.get(rid, {}).get("target") if rid else None
        if not target:
            continue

        sheet_path = _resolve_rel("xl/workbook.xml", target)
        sheets.append({"name": name, "path": sheet_path})

    return sheets


def _extract_images(z: zipfile.ZipFile, bucket: str, images_prefix: str) -> List[Dict[str, Any]]:
    uploaded: List[Dict[str, Any]] = []

    media_names = [n for n in z.namelist() if n.startswith("xl/media/") and not n.endswith("/")]
    for n in media_names:
        basename = posixpath.basename(n)
        dst_key = f"{images_prefix}/{basename}"

        ext = Path(basename).suffix.lower().lstrip(".")
        content_type = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "bmp": "image/bmp",
        }.get(ext)

        body = z.read(n)
        put_kwargs = {
            "Bucket": bucket,
            "Key": dst_key,
            "Body": body,
        }
        if content_type:
            put_kwargs["ContentType"] = content_type

        S3.put_object(**put_kwargs)

        uploaded.append(
            {
                "xlsx_path": n,
                "name": basename,
                "s3_key": dst_key,
                "content_type": content_type,
                "browser_supported": ext in {"png", "jpg", "jpeg", "gif", "webp"},
            }
        )

    return uploaded


def _drawing_images(
    z: zipfile.ZipFile,
    sheet_path: str,
    sheet_name: str,
    images_prefix: str,
) -> List[Dict[str, Any]]:
    sheet_root = _read_xml(z, sheet_path)
    if sheet_root is None:
        return []

    drawing_rids = []
    for d in sheet_root.findall(".//{*}drawing"):
        rid = d.get(_qname(_REL_NS, "id")) or d.get("r:id")
        if rid:
            drawing_rids.append(rid)

    if not drawing_rids:
        return []

    sheet_dir = posixpath.dirname(sheet_path)
    sheet_base = posixpath.basename(sheet_path)
    sheet_rels_path = posixpath.join(sheet_dir, "_rels", f"{sheet_base}.rels")
    sheet_rels = _rels_map(_read_xml(z, sheet_rels_path))

    images: List[Dict[str, Any]] = []

    for drawing_rid in drawing_rids:
        rel = sheet_rels.get(drawing_rid)
        if not rel:
            continue

        drawing_path = _resolve_rel(sheet_path, rel.get("target", ""))
        drawing_root = _read_xml(z, drawing_path)
        if drawing_root is None:
            continue

        drawing_rels_path = posixpath.join(
            posixpath.dirname(drawing_path),
            "_rels",
            f"{posixpath.basename(drawing_path)}.rels",
        )
        drawing_rels = _rels_map(_read_xml(z, drawing_rels_path))

        anchors = drawing_root.findall(".//{*}twoCellAnchor") + drawing_root.findall(".//{*}oneCellAnchor")
        for anchor in anchors:
            from_el = anchor.find("{*}from")
            to_el = anchor.find("{*}to")

            def _cell_pos(el: Optional[ET.Element]) -> Optional[Dict[str, int]]:
                if el is None:
                    return None
                col_el = el.find("{*}col")
                row_el = el.find("{*}row")
                col = _safe_int(col_el.text if col_el is not None else None)
                row = _safe_int(row_el.text if row_el is not None else None)
                if col is None or row is None:
                    return None
                return {"col": col + 1, "row": row + 1}

            from_cell = _cell_pos(from_el)
            to_cell = _cell_pos(to_el)

            blip = anchor.find(".//{*}blip")
            if blip is None:
                continue

            embed_rid = blip.get(_qname(_REL_NS, "embed")) or blip.get("r:embed")
            if not embed_rid:
                continue

            img_rel = drawing_rels.get(embed_rid)
            if not img_rel:
                continue

            xlsx_img_path = _resolve_rel(drawing_path, img_rel.get("target", ""))
            img_name = posixpath.basename(xlsx_img_path)
            s3_key = f"{images_prefix}/{img_name}"

            images.append(
                {
                    "sheet": sheet_name,
                    "name": img_name,
                    "s3_key": s3_key,
                    "anchor": {"from": from_cell, "to": to_cell},
                    "xlsx_path": xlsx_img_path,
                }
            )

    return images


def _sheet_cells(
    z: zipfile.ZipFile, sheet_path: str, shared: List[str], max_cells: int
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], bool]:
    root = _read_xml(z, sheet_path)
    if root is None:
        return [], [], False

    cells: List[Dict[str, str]] = []
    filled_cells: Dict[str, Dict[str, str]] = {}
    truncated = False

    for c in root.findall(".//{*}c"):
        ref = c.get("r")
        if not ref:
            continue

        text = _cell_text(c, shared)
        if text is None:
            continue

        cells.append({"ref": ref, "text": text})
        filled_cells[ref] = {"ref": ref, "text": text, "source_ref": ref}
        if len(cells) >= max_cells:
            truncated = True
            break

    for merge_el in root.findall(".//{*}mergeCell"):
        merge_ref = merge_el.get("ref") or ""
        start_ref, sep, end_ref = merge_ref.partition(":")
        if not sep:
            end_ref = start_ref
        start_row, start_col, start_index = _parse_cell_ref(start_ref)
        end_row, end_col, end_index = _parse_cell_ref(end_ref)
        if (
            start_row is None
            or start_col is None
            or start_index is None
            or end_row is None
            or end_col is None
            or end_index is None
        ):
            continue
        anchor = filled_cells.get(f"{start_col}{start_row}")
        if anchor is None or not (anchor.get("text") or "").strip():
            continue
        row_start = min(start_row, end_row)
        row_end = max(start_row, end_row)
        col_start = min(start_index, end_index)
        col_end = max(start_index, end_index)
        for row_num in range(row_start, row_end + 1):
            for col_index in range(col_start, col_end + 1):
                col_label = _column_label(col_index)
                if col_label is None:
                    continue
                ref = f"{col_label}{row_num}"
                current = filled_cells.get(ref)
                if current is None or not (current.get("text") or "").strip():
                    filled_cells[ref] = {
                        "ref": ref,
                        "text": anchor["text"],
                        "source_ref": anchor["source_ref"],
                    }

    filled = [filled_cells[ref] for ref in sorted(filled_cells.keys(), key=_cell_sort_key)]
    return cells, filled, truncated


def _column_index(col_label: str) -> Optional[int]:
    value = 0
    for ch in (col_label or "").upper():
        if ch < "A" or ch > "Z":
            return None
        value = (value * 26) + (ord(ch) - 64)
    return value or None


def _column_label(col_index: int) -> Optional[str]:
    if not isinstance(col_index, int) or col_index <= 0:
        return None
    out: List[str] = []
    value = col_index
    while value > 0:
        value, rem = divmod(value - 1, 26)
        out.append(chr(65 + rem))
    return "".join(reversed(out))


def _parse_cell_ref(cell_ref: str) -> Tuple[Optional[int], Optional[str], Optional[int]]:
    if not isinstance(cell_ref, str):
        return None, None, None
    m = _CELL_REF_RE.match(cell_ref.upper())
    if not m:
        return None, None, None
    col_label = m.group(1)
    return _safe_int(m.group(2)), col_label, _column_index(col_label)


def _cell_sort_key(cell_ref: str) -> Tuple[int, int]:
    row_num, col_label, col_index = _parse_cell_ref(cell_ref)
    return (row_num or 0, col_index or 0)


def _normalize_sheet_text(text: Optional[str]) -> str:
    return " ".join((text or "").strip().split())


def _is_numeric_text(text: Optional[str]) -> bool:
    normalized = _normalize_sheet_text(text)
    if not normalized:
        return False
    compact = re.sub(r"[\s,._/%:-]", "", normalized)
    return compact.isdigit()


def _is_noise_header(text: Optional[str]) -> bool:
    normalized = _normalize_sheet_text(text)
    if not normalized:
        return True
    return _NOISE_HEADER_RE.match(normalized) is not None


def _sheet_rows(cells: List[Dict[str, str]]) -> Dict[int, List[Dict[str, Any]]]:
    rows: Dict[int, List[Dict[str, Any]]] = {}
    for cell in cells:
        ref = cell.get("ref") or ""
        row_num, col_label, col_index = _parse_cell_ref(ref)
        if row_num is None or col_label is None or col_index is None:
            continue
        rows.setdefault(row_num, []).append(
            {
                "ref": ref,
                "text": cell.get("text") or "",
                "row": row_num,
                "col_label": col_label,
                "col_index": col_index,
                "source_ref": cell.get("source_ref") or ref,
            }
        )

    for row_cells in rows.values():
        row_cells.sort(key=lambda item: item["col_index"])
    return rows


def _build_header_map(rows: Dict[int, List[Dict[str, Any]]], header_rows: List[int]) -> Tuple[Dict[str, str], List[str]]:
    if not header_rows:
        return {}, []

    header_cols = sorted(
        {
            cell["col_label"]
            for row_num in header_rows
            for cell in rows.get(row_num, [])
            if _normalize_sheet_text(cell.get("text"))
        },
        key=lambda label: _column_index(label) or 0,
    )
    header_map: Dict[str, str] = {}
    header_order: List[str] = []
    seen: Dict[str, int] = {}

    for col_label in header_cols:
        parts: List[str] = []
        for row_num in header_rows:
            cell = next((item for item in rows.get(row_num, []) if item["col_label"] == col_label), None)
            header_text = _normalize_sheet_text((cell or {}).get("text"))
            if not header_text or _is_noise_header(header_text) or _is_numeric_text(header_text):
                continue
            if not parts or parts[-1] != header_text:
                parts.append(header_text)
        if not parts:
            continue
        header = " / ".join(parts)
        count = seen.get(header, 0) + 1
        seen[header] = count
        if count > 1:
            header = f"{header} [{count}]"
        header_map[col_label] = header
        header_order.append(col_label)

    return header_map, header_order


def _detect_primary_table(part_id: str, sheet_name: str, rows: Dict[int, List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None

    sorted_rows = sorted(rows.keys())
    best: Optional[Dict[str, Any]] = None

    for idx, row_num in enumerate(sorted_rows[:50]):
        for depth in range(1, min(idx + 1, 3) + 1):
            header_rows = sorted_rows[idx - depth + 1 : idx + 1]
            if any((header_rows[pos] - header_rows[pos - 1]) > 1 for pos in range(1, len(header_rows))):
                continue

            header_map, header_order = _build_header_map(rows, header_rows)
            if len(header_order) < 2:
                continue

            if depth > 1:
                max_subheader_cells = max(2, len(header_order) - 1)
                row_text_counts = []
                for header_candidate_row in header_rows:
                    count = sum(
                        1
                        for cell in rows.get(header_candidate_row, [])
                        if _normalize_sheet_text(cell.get("text"))
                        and not _is_noise_header(cell.get("text"))
                        and not _is_numeric_text(cell.get("text"))
                    )
                    row_text_counts.append(count)
                if any(count >= max_subheader_cells for count in row_text_counts[1:]):
                    continue

            active_cols = set(header_order)
            data_rows: List[int] = []
            blank_run = 0
            for next_row in sorted_rows:
                if next_row <= row_num:
                    continue
                overlaps = [
                    cell
                    for cell in rows[next_row]
                    if cell["col_label"] in active_cols and _normalize_sheet_text(cell.get("text"))
                ]
                if overlaps:
                    data_rows.append(next_row)
                    blank_run = 0
                    continue

                blank_run += 1
                if data_rows and blank_run >= 2:
                    break

            if not data_rows:
                continue

            score = (len(header_order) * 100) + (len(data_rows) * 10) + (depth * 5)
            score += sum(1 for header in header_map.values() if len(header) <= 60)
            score += sum(25 for header in header_map.values() if " / " in header)

            if best is None or score > best["score"] or (score == best["score"] and row_num < best["header_row"]):
                best = {
                    "score": score,
                    "header_row": row_num,
                    "header_rows": header_rows,
                    "header_map": header_map,
                    "header_order": header_order,
                    "headers": [header_map[col_label] for col_label in header_order],
                    "data_rows": data_rows,
                    "row_end": data_rows[-1],
                }

    if best is None:
        return None

    header_row = best["header_row"]
    all_header_cells = [cell for row_num in best["header_rows"] for cell in rows.get(row_num, [])]
    noise_columns = [
        cell["col_label"]
        for cell in all_header_cells
        if cell["col_label"] not in best["header_map"]
    ]

    first_col = best["header_order"][0]
    last_col = best["header_order"][-1]
    row_start = best["data_rows"][0]
    row_end = best["row_end"]

    return {
        "table_id": f"{part_id}:table:primary",
        "title": sheet_name,
        "header_row": header_row,
        "header_rows": best["header_rows"],
        "header_map": best["header_map"],
        "header_order": best["header_order"],
        "headers": best["headers"],
        "data_rows": best["data_rows"],
        "data_rows_set": set(best["data_rows"]),
        "row_start": row_start,
        "row_end": row_end,
        "range": f"{first_col}{header_row}:{last_col}{row_end}",
        "noise_columns_set": set(noise_columns),
    }


def _build_table_row_objects(
    part_id: str,
    sheet_name: str,
    table: Dict[str, Any],
    rows: Dict[int, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    by_row = {row_num: {cell["col_label"]: cell for cell in rows.get(row_num, [])} for row_num in table["data_rows"]}
    row_objects: List[Dict[str, Any]] = []

    for row_num in table["data_rows"]:
        fields: List[Dict[str, Any]] = []
        source_cells: List[str] = []
        source_object_ids: List[str] = []
        for col_label in table["header_order"]:
            cell = by_row.get(row_num, {}).get(col_label)
            if cell is None:
                continue
            value = _normalize_sheet_text(cell.get("text"))
            if not value:
                continue
            header = table["header_map"].get(col_label)
            source_ref = cell.get("source_ref") or cell.get("ref")
            if isinstance(source_ref, str) and source_ref and source_ref not in source_cells:
                source_cells.append(source_ref)
                source_object_ids.append(f"{part_id}:cell:{source_ref}")
            fields.append(
                {
                    "header": header,
                    "value": value,
                    "cell": cell.get("ref"),
                    "source_cell": source_ref,
                }
            )

        if not fields:
            continue

        row_text = "\n".join(
            [f"Table: {table['title']}", f"Row: {row_num}"]
            + [f"{field['header']}: {field['value']}" for field in fields if field.get("header")]
        )
        row_objects.append(
            {
                "id": f"{table['table_id']}:row:{row_num}",
                "type": "row",
                "part_id": part_id,
                "title": table["title"],
                "text": row_text,
                "loc": {"xlsx": {"sheet": sheet_name, "row": row_num}},
                "metadata_extra": {
                    "table_id": table["table_id"],
                    "table_title": table["title"],
                    "table_headers": table["headers"],
                    "source_cells": source_cells,
                    "source_object_ids": source_object_ids,
                    "fields": fields,
                },
            }
        )

    return row_objects


def lambda_handler(event, context):
    document_id = event["document_id"]
    source_type = (event.get("source_type") or "").lower().lstrip(".")
    if source_type != "xlsx":
        raise ValueError(f"xlsxextract invoked for source_type={source_type}")

    processing_bucket = event["processing_bucket"]
    source_bucket, source_key = _get_source_bucket_key(event)

    structured_prefix = _structured_prefix(event)
    assets_images_prefix = _assets_images_prefix(event)

    doc_manifest_key = f"{structured_prefix}/document_manifest.json"
    objects_key = f"{structured_prefix}/objects.jsonl"
    assets_manifest_key = f"{structured_prefix}/assets_manifest.json"

    obj = S3.get_object(Bucket=source_bucket, Key=source_key)
    data = obj["Body"].read()

    with zipfile.ZipFile(io.BytesIO(data), "r") as z:
        uploaded_images = _extract_images(z, processing_bucket, assets_images_prefix)
        shared = _shared_strings(z)
        sheets = _workbook_sheets(z)

        max_cells = int(os.environ.get("XLSX_MAX_CELLS", "5000"))

        elements: List[Dict[str, Any]] = []
        media_by_name = {m.get("name"): m for m in uploaded_images if m.get("name")}

        order = 0
        for sheet_index, sh in enumerate(sheets, start=1):
            part_id = f"sheet:{sheet_index}"
            sheet_name = sh.get("name")
            sheet_path = sh.get("path")

            cells, filled_cells, _truncated = _sheet_cells(z, sheet_path, shared, max_cells=max_cells)
            sheet_rows = _sheet_rows(cells)
            filled_rows = _sheet_rows(filled_cells)
            table = _detect_primary_table(part_id, sheet_name, filled_rows)

            if table is not None:
                order += 1
                table_source_ids: List[str] = []
                for row_num in table["data_rows"]:
                    for cell in filled_rows.get(row_num, []):
                        if cell["col_label"] not in table["header_map"]:
                            continue
                        source_id = f"{part_id}:cell:{cell.get('source_ref') or cell['ref']}"
                        if source_id not in table_source_ids:
                            table_source_ids.append(source_id)
                elements.append(
                    {
                        "id": table["table_id"],
                        "type": "table",
                        "part_id": part_id,
                        "order": order,
                        "text": table["title"],
                        "loc": {
                            "xlsx": {
                                "sheet": sheet_name,
                                "range": table["range"],
                                "header_row": table["header_row"],
                                "row_start": table["row_start"],
                                "row_end": table["row_end"],
                            }
                        },
                        "metadata_extra": {
                            "table_id": table["table_id"],
                            "headers": table["headers"],
                            "source_object_ids": table_source_ids,
                        },
                    }
                )
                for row_object in _build_table_row_objects(part_id, sheet_name, table, filled_rows):
                    order += 1
                    row_object["order"] = order
                    elements.append(row_object)

            for c in cells:
                text = (c.get("text") or "").strip()
                if not text:
                    continue
                order += 1
                ref = c.get("ref") or ""
                row_num, col_label, col_index = _parse_cell_ref(ref)
                metadata_extra: Dict[str, Any] = {}
                if table is not None and row_num is not None and col_label is not None:
                    metadata_extra.update(
                        {
                            "table_id": table["table_id"],
                            "table_title": table["title"],
                            "table_headers": table["headers"],
                            "header_row": table["header_row"],
                        }
                    )
                    if row_num in table["header_rows"] and col_label in table["header_map"]:
                        metadata_extra.update(
                            {
                                "table_role": "header",
                                "header": table["header_map"][col_label],
                            }
                        )
                    elif row_num in table["data_rows_set"] and col_label in table["header_map"]:
                        metadata_extra.update(
                            {
                                "table_role": "data",
                                "header": table["header_map"][col_label],
                            }
                        )
                    elif table["header_row"] <= row_num <= table["row_end"] and col_label in table["noise_columns_set"]:
                        metadata_extra.update(
                            {
                                "table_role": "noise",
                                "search_excluded": True,
                            }
                        )
                    else:
                        metadata_extra.pop("table_id", None)
                        metadata_extra.pop("table_title", None)
                        metadata_extra.pop("table_headers", None)
                        metadata_extra.pop("header_row", None)
                elements.append(
                    {
                        "id": f"{part_id}:cell:{ref}" if ref else f"{part_id}:cell:{order}",
                        "type": "cell",
                        "part_id": part_id,
                        "order": order,
                        "text": text,
                        "loc": {
                            "xlsx": {
                                "sheet": sheet_name,
                                "cell": ref or None,
                                "row": row_num,
                                "col": col_index,
                            }
                        },
                        "metadata_extra": metadata_extra,
                    }
                )

            for img_index, img in enumerate(
                _drawing_images(z, sheet_path, sheet_name, assets_images_prefix), start=1
            ):
                order += 1
                name = img.get("name")
                media = media_by_name.get(name) if name else None
                elements.append(
                    {
                        "id": f"{part_id}:image:{img_index}",
                        "type": "image",
                        "part_id": part_id,
                        "order": order,
                        "name": name,
                        "s3_key": img.get("s3_key"),
                        "content_type": (media or {}).get("content_type"),
                        "openxml_path": img.get("xlsx_path"),
                        "loc": {"xlsx": {"sheet": sheet_name, "anchor": img.get("anchor")}},
                    }
                )

    classification = _classification(event)
    objects: List[Dict[str, Any]] = []
    for el in elements:
        rec: Dict[str, Any] = {
            "object_id": el.get("id"),
            "object_type": el.get("type"),
            "metadata": {
                "source_type": source_type,
                "loc": el.get("loc"),
            },
        }
        if classification is not None:
            rec["metadata"]["classification"] = classification
        if isinstance(el.get("metadata_extra"), dict):
            rec["metadata"].update(el["metadata_extra"])

        if el.get("type") in {"text", "cell", "table", "row"}:
            rec["text"] = el.get("text")
            if el.get("title"):
                rec["title"] = el.get("title")
        elif el.get("type") == "image":
            rec["metadata"].update(
                {
                    "s3_key": el.get("s3_key"),
                    "openxml_path": el.get("openxml_path"),
                    "content_type": el.get("content_type"),
                    "name": el.get("name"),
                }
            )
            if el.get("s3_key"):
                rec["metadata"]["s3_uri"] = f"s3://{processing_bucket}/{el['s3_key']}"

        objects.append(rec)

    referenced_image_keys = {
        el.get("s3_key")
        for el in elements
        if el.get("type") == "image" and isinstance(el.get("s3_key"), str) and el.get("s3_key")
    }

    images_assets = [
        {
            "name": m.get("name"),
            "s3_bucket": processing_bucket,
            "s3_key": m.get("s3_key"),
            "s3_uri": f"s3://{processing_bucket}/{m['s3_key']}" if m.get("s3_key") else None,
            "content_type": m.get("content_type"),
            "openxml_path": m.get("xlsx_path"),
        }
        for m in uploaded_images
        if m.get("s3_key") in referenced_image_keys
    ]

    assets_manifest = {
        "schema": "structured-assets-manifest@1",
        "document_id": document_id,
        "assets": {"images": images_assets},
    }

    document_manifest = {
        "schema": "structured-document-manifest@1",
        "document_id": document_id,
        "source": {
            "source_type": source_type,
            "bucket": source_bucket,
            "key": source_key,
            "s3_uri": f"s3://{source_bucket}/{source_key}",
        },
        "output": {
            "processing_bucket": processing_bucket,
            "structured": {
                "document_manifest": {"bucket": processing_bucket, "key": doc_manifest_key},
                "objects": {"bucket": processing_bucket, "key": objects_key},
                "assets_manifest": {"bucket": processing_bucket, "key": assets_manifest_key},
            },
            "assets": {
                "images_prefix": assets_images_prefix,
            },
        },
        "counts": {
            "object_count": len(objects),
            "asset_count": len(images_assets),
        },
        "pointers": {
            "objects_s3_uri": f"s3://{processing_bucket}/{objects_key}",
            "assets_manifest_s3_uri": f"s3://{processing_bucket}/{assets_manifest_key}",
        },
    }

    _put_jsonl(processing_bucket, objects_key, objects)
    _put_json(processing_bucket, assets_manifest_key, assets_manifest)
    _put_json(processing_bucket, doc_manifest_key, document_manifest)

    print(
        json.dumps(
            {
                "msg": "openxml-xlsx-extracted",
                "document_id": document_id,
                "object_count": len(objects),
                "asset_count": len(images_assets),
                "structured_prefix": structured_prefix,
                "assets_images_prefix": assets_images_prefix,
            }
        )
    )

    out = dict(event)
    out["work_prefix"] = _compute_work_prefix(event) + "/"
    out["structured_prefix"] = structured_prefix + "/"
    out["assets_images_prefix"] = assets_images_prefix + "/"
    out["structured_outputs"] = {
        "document_manifest": {"bucket": processing_bucket, "key": doc_manifest_key},
        "objects": {"bucket": processing_bucket, "key": objects_key},
        "assets_manifest": {"bucket": processing_bucket, "key": assets_manifest_key},
    }
    return out
