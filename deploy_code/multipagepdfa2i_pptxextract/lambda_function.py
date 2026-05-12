import hashlib
import io
import json
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
    media_names = [n for n in z.namelist() if n.startswith("ppt/media/") and not n.endswith("/")]

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
                "pptx_path": n,
                "name": basename,
                "s3_key": dst_key,
                "content_type": content_type,
                "browser_supported": ext in {"png", "jpg", "jpeg", "gif", "webp"},
            }
        )

    return uploaded


def _slide_order(z: zipfile.ZipFile) -> List[str]:
    pres = _read_xml(z, "ppt/presentation.xml")
    pres_rels = _rels_map(_read_xml(z, "ppt/_rels/presentation.xml.rels"))
    if pres is None:
        return []

    slide_paths: List[str] = []
    for sld_id in pres.findall(".//{*}sldId"):
        rid = sld_id.get(_qname(_REL_NS, "id")) or sld_id.get("r:id")
        if not rid:
            continue
        rel = pres_rels.get(rid)
        if not rel:
            continue
        target = rel.get("target") or ""
        if not target:
            continue
        slide_paths.append(posixpath.normpath(posixpath.join("ppt", target)))

    return slide_paths


def _bbox_emu(el: ET.Element) -> Optional[Dict[str, int]]:
    xfrm = el.find(".//{*}xfrm")
    if xfrm is None:
        return None

    off = xfrm.find("{*}off")
    ext = xfrm.find("{*}ext")
    if off is None or ext is None:
        return None

    x = off.get("x")
    y = off.get("y")
    cx = ext.get("cx")
    cy = ext.get("cy")

    try:
        return {
            "x": int(x) if x is not None else 0,
            "y": int(y) if y is not None else 0,
            "cx": int(cx) if cx is not None else 0,
            "cy": int(cy) if cy is not None else 0,
        }
    except ValueError:
        return None


def _shape_text(el: ET.Element) -> str:
    texts = [t.text or "" for t in el.findall(".//{*}txBody//{*}t")]
    return "".join(texts).strip()


def _parse_slide(
    z: zipfile.ZipFile,
    slide_path: str,
    slide_index: int,
    images_prefix: str,
) -> Dict[str, Any]:
    slide = _read_xml(z, slide_path)
    if slide is None:
        return {"index": slide_index, "path": slide_path, "elements": [], "warnings": ["slide not found"]}

    slide_dir = posixpath.dirname(slide_path)
    slide_base = posixpath.basename(slide_path)
    rels_path = posixpath.join(slide_dir, "_rels", f"{slide_base}.rels")
    rels = _rels_map(_read_xml(z, rels_path))

    elements: List[Dict[str, Any]] = []

    for sp in slide.findall(".//{*}sp"):
        text = _shape_text(sp)
        if not text:
            continue
        elements.append(
            {
                "type": "text",
                "text": text,
                "bbox_emu": _bbox_emu(sp),
            }
        )

    for pic in slide.findall(".//{*}pic"):
        blip = pic.find(".//{*}blip")
        rid = None
        if blip is not None:
            rid = blip.get(_qname(_REL_NS, "embed")) or blip.get("r:embed")

        target_path = None
        if rid and rid in rels:
            target_path = _resolve_rel(slide_path, rels[rid].get("target", ""))

        s3_key = None
        if target_path and target_path.startswith("ppt/media/"):
            s3_key = f"{images_prefix}/{posixpath.basename(target_path)}"

        elements.append(
            {
                "type": "image",
                "rel_id": rid,
                "pptx_path": target_path,
                "s3_key": s3_key,
                "bbox_emu": _bbox_emu(pic),
            }
        )

    return {"index": slide_index, "path": slide_path, "elements": elements, "warnings": []}


def lambda_handler(event, context):
    document_id = event["document_id"]
    source_type = (event.get("source_type") or "").lower().lstrip(".")
    if source_type != "pptx":
        raise ValueError(f"pptxextract invoked for source_type={source_type}")

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
        uploaded_images = _upload_media(z, processing_bucket, assets_images_prefix)
        slides = _slide_order(z)
        slide_manifests = [_parse_slide(z, p, idx + 1, assets_images_prefix) for idx, p in enumerate(slides)]

    elements: List[Dict[str, Any]] = []
    order = 0
    for s in slide_manifests:
        slide_index = s.get("index")
        part_id = f"slide:{slide_index}"

        for el_i, el in enumerate(s.get("elements", []) or [], start=1):
            etype = el.get("type")
            loc = {"pptx": {"slide": slide_index, "bbox_emu": el.get("bbox_emu")}}

            if etype == "text":
                text = (el.get("text") or "").strip()
                if not text:
                    continue
                order += 1
                elements.append(
                    {
                        "id": f"{part_id}:text:{el_i}",
                        "type": "text",
                        "part_id": part_id,
                        "order": order,
                        "text": text,
                        "loc": loc,
                    }
                )
            elif etype == "image":
                order += 1
                elements.append(
                    {
                        "id": f"{part_id}:image:{el_i}",
                        "type": "image",
                        "part_id": part_id,
                        "order": order,
                        "rel_id": el.get("rel_id"),
                        "openxml_path": el.get("pptx_path"),
                        "s3_key": el.get("s3_key"),
                        "loc": loc,
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
                    "rel_id": el.get("rel_id"),
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
            "openxml_path": m.get("pptx_path"),
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
                "msg": "openxml-pptx-extracted",
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
