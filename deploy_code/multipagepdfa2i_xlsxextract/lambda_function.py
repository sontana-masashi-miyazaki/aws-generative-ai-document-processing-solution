import hashlib
import io
import json
import os
import posixpath
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

import boto3


S3 = boto3.client("s3")

_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


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


def _compute_work_prefix(event: Dict[str, Any]) -> str:
    work_prefix = event.get("work_prefix")
    if work_prefix:
        return _norm_prefix(work_prefix)

    pipeline_version = event["pipeline_version"]
    document_id = event["document_id"]
    hash_prefix = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:2]
    return f"work/{pipeline_version}/{hash_prefix}/{document_id}"


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
    base_dir = posixpath.dirname(base_path)
    return posixpath.normpath(posixpath.join(base_dir, target))


def _shared_strings(z: zipfile.ZipFile) -> List[str]:
    root = _read_xml(z, "xl/sharedStrings.xml")
    if root is None:
        return []

    strings: List[str] = []
    for si in root.findall(".//{*}si"):
        texts = [t.text or "" for t in si.findall(".//{*}t")]
        strings.append("".join(texts))
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
        texts = [t_el.text or "" for t_el in c.findall(".//{*}is//{*}t")]
        s = "".join(texts).strip()
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

        sheet_path = posixpath.normpath(posixpath.join("xl", target))
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


def _sheet_cells(z: zipfile.ZipFile, sheet_path: str, shared: List[str], max_cells: int) -> Tuple[List[Dict[str, str]], bool]:
    root = _read_xml(z, sheet_path)
    if root is None:
        return [], False

    cells: List[Dict[str, str]] = []
    truncated = False

    for c in root.findall(".//{*}c"):
        ref = c.get("r")
        if not ref:
            continue

        text = _cell_text(c, shared)
        if text is None:
            continue

        cells.append({"ref": ref, "text": text})
        if len(cells) >= max_cells:
            truncated = True
            break

    return cells, truncated


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
    relations_key = f"{structured_prefix}/relations.jsonl"
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

            cells, _truncated = _sheet_cells(z, sheet_path, shared, max_cells=max_cells)
            for c in cells:
                text = (c.get("text") or "").strip()
                if not text:
                    continue
                order += 1
                ref = c.get("ref") or ""
                elements.append(
                    {
                        "id": f"{part_id}:cell:{ref}" if ref else f"{part_id}:cell:{order}",
                        "type": "text",
                        "part_id": part_id,
                        "order": order,
                        "text": text,
                        "loc": {"xlsx": {"sheet": sheet_name, "cell": ref or None}},
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

        if el.get("type") == "text":
            rec["text"] = el.get("text")
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
                "relations": {"bucket": processing_bucket, "key": relations_key},
                "assets_manifest": {"bucket": processing_bucket, "key": assets_manifest_key},
            },
            "assets": {
                "images_prefix": assets_images_prefix,
            },
        },
        "counts": {
            "object_count": len(objects),
            "relation_count": 0,
            "asset_count": len(images_assets),
        },
        "pointers": {
            "objects_s3_uri": f"s3://{processing_bucket}/{objects_key}",
            "relations_s3_uri": f"s3://{processing_bucket}/{relations_key}",
            "assets_manifest_s3_uri": f"s3://{processing_bucket}/{assets_manifest_key}",
        },
    }

    _put_jsonl(processing_bucket, objects_key, objects)
    _put_jsonl(processing_bucket, relations_key, [])
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
        "relations": {"bucket": processing_bucket, "key": relations_key},
        "assets_manifest": {"bucket": processing_bucket, "key": assets_manifest_key},
    }
    return out
