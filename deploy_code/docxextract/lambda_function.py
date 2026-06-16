import hashlib
import io
import json
import posixpath
import unicodedata
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


def _upload_media(z: zipfile.ZipFile, bucket: str, images_prefix: str) -> List[Dict[str, Any]]:
    uploaded: List[Dict[str, Any]] = []
    media_names = [n for n in z.namelist() if n.startswith("word/media/") and not n.endswith("/")]

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
            "tif": "image/tiff",
            "tiff": "image/tiff",
            "emf": "image/emf",
            "wmf": "image/wmf",
        }.get(ext)

        body = z.read(n)
        put_kwargs = {"Bucket": bucket, "Key": dst_key, "Body": body}
        if content_type:
            put_kwargs["ContentType"] = content_type

        S3.put_object(**put_kwargs)

        uploaded.append(
            {
                "docx_path": n,
                "name": basename,
                "s3_key": dst_key,
                "content_type": content_type,
                "browser_supported": ext in {"png", "jpg", "jpeg", "gif", "webp"},
            }
        )

    return uploaded


def _element_text(el: ET.Element) -> str:
    texts = [t.text or "" for t in el.findall(".//{*}t")]
    return "".join(texts).strip()


def _find_image_rel_ids(el: ET.Element) -> List[str]:
    rids: List[str] = []

    for blip in el.findall(".//{*}blip"):
        rid = blip.get(_qname(_REL_NS, "embed")) or blip.get("r:embed")
        if rid:
            rids.append(rid)

    for imagedata in el.findall(".//{*}imagedata"):
        rid = imagedata.get(_qname(_REL_NS, "id")) or imagedata.get("r:id")
        if rid:
            rids.append(rid)

    seen = set()
    out: List[str] = []
    for rid in rids:
        if rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
    return out


def _parse_docx(z: zipfile.ZipFile, images_prefix: str) -> Dict[str, Any]:
    doc_root = _read_xml(z, "word/document.xml")
    if doc_root is None:
        return {"elements": [], "warnings": ["word/document.xml not found"]}

    rels_root = _read_xml(z, "word/_rels/document.xml.rels")
    rels = _rels_map(rels_root)

    elements: List[Dict[str, Any]] = []

    body = doc_root.find(".//{*}body")
    if body is None:
        return {"elements": [], "warnings": ["word/body not found"]}

    p_idx = 0
    t_idx = 0

    for child in list(body):
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if local == "p":
            p_idx += 1
            text = _element_text(child)
            rids = _find_image_rel_ids(child)
            item: Dict[str, Any] = {"type": "paragraph", "index": p_idx}
            if text:
                item["text"] = text
            if rids:
                item["image_rel_ids"] = rids
                item["image_targets"] = [
                    {
                        "rel_id": rid,
                        "docx_path": _resolve_rel(
                            "word/document.xml", rels.get(rid, {}).get("target", "")
                        ),
                        "s3_key": None,
                    }
                    for rid in rids
                ]
                for tgt in item["image_targets"]:
                    if tgt["docx_path"].startswith("word/media/"):
                        tgt["s3_key"] = f"{images_prefix}/{posixpath.basename(tgt['docx_path'])}"
            if text or rids:
                elements.append(item)

        elif local == "tbl":
            t_idx += 1
            rows: List[List[str]] = []
            for tr in child.findall(".//{*}tr"):
                row: List[str] = []
                for tc in tr.findall(".//{*}tc"):
                    row.append(_element_text(tc))
                if any(c for c in row):
                    rows.append(row)

            rids = _find_image_rel_ids(child)
            item2: Dict[str, Any] = {"type": "table", "index": t_idx}
            if rows:
                item2["rows"] = rows
            if rids:
                item2["image_rel_ids"] = rids
                item2["image_targets"] = [
                    {
                        "rel_id": rid,
                        "docx_path": _resolve_rel(
                            "word/document.xml", rels.get(rid, {}).get("target", "")
                        ),
                        "s3_key": None,
                    }
                    for rid in rids
                ]
                for tgt in item2["image_targets"]:
                    if tgt["docx_path"].startswith("word/media/"):
                        tgt["s3_key"] = f"{images_prefix}/{posixpath.basename(tgt['docx_path'])}"

            if rows or rids:
                elements.append(item2)

    return {"elements": elements, "warnings": []}


def _build_elements(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    part_id = "doc:1"
    elements: List[Dict[str, Any]] = []
    order = 0

    for item in parsed.get("elements", []):
        itype = item.get("type")
        idx = item.get("index")

        if itype == "paragraph":
            text = (item.get("text") or "").strip()
            if text:
                order += 1
                elements.append(
                    {
                        "id": f"{part_id}:p:{idx}",
                        "type": "text",
                        "part_id": part_id,
                        "order": order,
                        "text": text,
                        "loc": {"docx": {"paragraph": idx}},
                    }
                )

            for img_i, tgt in enumerate(item.get("image_targets", []) or [], start=1):
                order += 1
                elements.append(
                    {
                        "id": f"{part_id}:p:{idx}:img:{img_i}",
                        "type": "image",
                        "part_id": part_id,
                        "order": order,
                        "rel_id": tgt.get("rel_id"),
                        "openxml_path": tgt.get("docx_path"),
                        "s3_key": tgt.get("s3_key"),
                        "loc": {"docx": {"paragraph": idx}},
                    }
                )

        elif itype == "table":
            rows = item.get("rows")
            if rows:
                order += 1
                elements.append(
                    {
                        "id": f"{part_id}:tbl:{idx}",
                        "type": "table",
                        "part_id": part_id,
                        "order": order,
                        "rows": rows,
                        "loc": {"docx": {"table": idx}},
                    }
                )

            for img_i, tgt in enumerate(item.get("image_targets", []) or [], start=1):
                order += 1
                elements.append(
                    {
                        "id": f"{part_id}:tbl:{idx}:img:{img_i}",
                        "type": "image",
                        "part_id": part_id,
                        "order": order,
                        "rel_id": tgt.get("rel_id"),
                        "openxml_path": tgt.get("docx_path"),
                        "s3_key": tgt.get("s3_key"),
                        "loc": {"docx": {"table": idx}},
                    }
                )

    return elements


def lambda_handler(event, context):
    document_id = event["document_id"]
    source_type = (event.get("source_type") or "").lower().lstrip(".")
    if source_type != "docx":
        raise ValueError(f"docxextract invoked for source_type={source_type}")

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
        uploaded_images = _upload_media(z, processing_bucket, assets_images_prefix)
        parsed = _parse_docx(z, assets_images_prefix)

    elements = _build_elements(parsed)

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
        elif el.get("type") == "table":
            rec["metadata"]["rows"] = el.get("rows")
        elif el.get("type") == "image":
            rec["metadata"].update(
                {
                    "s3_key": el.get("s3_key"),
                    "openxml_path": el.get("openxml_path"),
                    "rel_id": el.get("rel_id"),
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
            "openxml_path": m.get("docx_path"),
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
                "msg": "openxml-docx-extracted",
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
