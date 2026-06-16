import datetime
import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import boto3
import botocore

# No document content in logs.
logging.getLogger().setLevel(logging.WARNING)

_S3 = boto3.client("s3")
_CELL_REF_RE = re.compile(r"^([A-Z]+)([0-9]+)$")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _require_str(d: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    raise ValueError(f"Missing required field: one of {keys}")


def _join_s3_key(prefix: str, suffix: str) -> str:
    prefix = (prefix or "").lstrip("/")
    suffix = (suffix or "").lstrip("/")
    if prefix.endswith("/"):
        return prefix + suffix
    return f"{prefix}/{suffix}" if prefix else suffix


def _s3_exists(bucket: str, key: str) -> bool:
    try:
        _S3.head_object(Bucket=bucket, Key=key)
        return True
    except botocore.exceptions.ClientError as e:  # noqa: BLE001
        code = e.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _s3_get_bytes(bucket: str, key: str) -> bytes:
    resp = _S3.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def _s3_get_jsonl(bucket: str, key: str) -> Iterable[Dict[str, Any]]:
    body = _s3_get_bytes(bucket, key).decode("utf-8")
    for ln in body.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        yield json.loads(ln)


def _s3_put_jsonl(bucket: str, key: str, json_lines: Iterable[str]) -> None:
    payload = "\n".join(json_lines) + "\n"
    _S3.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload.encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def _extract_text(obj: Dict[str, Any]) -> Optional[str]:
    for k in ("text", "content", "body", "value"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v

    meta = obj.get("metadata")
    if isinstance(meta, dict):
        rows = meta.get("rows")
        if isinstance(rows, list) and rows:
            try:
                lines = ["\t".join(str(c or "") for c in (r or [])) for r in rows]
                text = "\n".join(lines).strip()
                return text or None
            except Exception:
                return None

    return None


def _extract_title(obj: Dict[str, Any]) -> Optional[str]:
    for k in ("title", "heading", "name"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:256]
    return None


def _split_text(text: str, max_chars: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    parts: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        j = min(i + max_chars, n)
        if j < n:
            window_start = i + int(max_chars * 0.8)
            cut = max(text.rfind("\n", window_start, j), text.rfind(" ", window_start, j))
            if cut > i:
                j = cut
        part = text[i:j].strip()
        if part:
            parts.append(part)
        i = j
    return parts


def _looks_like_heading(text: str) -> bool:
    text = (text or "").strip()
    if not text or len(text) > 120:
        return False
    if "\n" in text:
        return False
    return True


def _obj_id(obj: Dict[str, Any], idx: int) -> str:
    v = obj.get("id")
    if not (isinstance(v, str) and v.strip()):
        v = obj.get("object_id")
    return v if isinstance(v, str) and v.strip() else f"obj_{idx}"


def _obj_type(obj: Dict[str, Any]) -> Optional[str]:
    v = obj.get("type")
    if not (isinstance(v, str) and v.strip()):
        v = obj.get("object_type")
    return v if isinstance(v, str) and v.strip() else None


def _metadata(obj: Dict[str, Any]) -> Dict[str, Any]:
    meta = obj.get("metadata")
    return dict(meta) if isinstance(meta, dict) else {}


def _nested_loc(meta: Dict[str, Any], source_type: str, key: str) -> Any:
    loc = meta.get("loc")
    if not isinstance(loc, dict):
        return None
    typed = loc.get(source_type)
    if not isinstance(typed, dict):
        return None
    return typed.get(key)


def _normalize_s3_key(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith("s3://"):
        rest = value[5:]
        _bucket, _sep, key = rest.partition("/")
        return key.lstrip("/") if key else None
    return value.lstrip("/")


def _image_enrichment_is_useful(obj: Dict[str, Any], text: Optional[str]) -> bool:
    if not text:
        return False
    meta = obj.get("metadata")
    if isinstance(meta, dict) and meta.get("enrichment_error"):
        return False
    return text.strip().lower() != "image description unavailable."


def _normalize_records(
    records: List[Dict[str, Any]], default_source_type: str
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, str]]]]:
    base_objects: List[Dict[str, Any]] = []
    image_descriptions: Dict[str, List[Dict[str, str]]] = {}

    for idx, obj in enumerate(records):
        obj_id = _obj_id(obj, idx)
        obj_type = _obj_type(obj)
        meta = _metadata(obj)
        text = _extract_text(obj)
        title = _extract_title(obj)
        source_type = meta.get("source_type")
        if not isinstance(source_type, str) or not source_type.strip():
            source_type = default_source_type
        s3_key = _normalize_s3_key(meta.get("s3_key"))

        if obj_type == "image_enrichment":
            source = obj.get("source")
            if not s3_key and isinstance(source, dict):
                s3_key = _normalize_s3_key(source.get("s3_key"))
            if s3_key and _image_enrichment_is_useful(obj, text):
                image_descriptions.setdefault(s3_key, []).append({"id": obj_id, "text": text or ""})
            continue

        base_objects.append(
            {
                "seq": idx,
                "id": obj_id,
                "type": obj_type,
                "text": text,
                "title": title,
                "metadata": meta,
                "source_type": source_type,
                "s3_key": s3_key,
            }
        )

    return base_objects, image_descriptions


def _segment_metadata(
    objects: List[Dict[str, Any]],
    group_kind: str,
    group_key: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    first_meta = dict(objects[0].get("metadata") or {}) if objects else {}
    out = {
        "source_type": objects[0].get("source_type") if objects else None,
        "classification": first_meta.get("classification"),
        "loc": first_meta.get("loc"),
        "group_kind": group_kind,
        "group_key": group_key,
    }
    if len(objects) > 1:
        out["group_object_count"] = len(objects)
    if extra:
        out.update(extra)
    return {k: v for k, v in out.items() if v is not None}


def _make_segment(
    *,
    objects: List[Dict[str, Any]],
    title: Optional[str],
    text: str,
    group_kind: str,
    group_key: str,
    metadata_extra: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text or not objects:
        return None
    return {
        "title": title.strip()[:256] if isinstance(title, str) and title.strip() else None,
        "text": text,
        "source_object_ids": [o["id"] for o in objects],
        "object_types": sorted({o.get("type") or "unknown" for o in objects}),
        "metadata": _segment_metadata(objects, group_kind, group_key, metadata_extra),
    }


def _parse_cell_ref(cell_ref: str) -> Tuple[int, str]:
    if not isinstance(cell_ref, str):
        return (0, "")
    m = _CELL_REF_RE.match(cell_ref.upper())
    if not m:
        return (0, cell_ref.upper())
    return (int(m.group(2)), m.group(1))


def _column_index(col_label: str) -> int:
    value = 0
    for ch in (col_label or "").upper():
        if ch < "A" or ch > "Z":
            return 0
        value = (value * 26) + (ord(ch) - 64)
    return value


def _pack_line_segments(
    *,
    title: Optional[str],
    header: Optional[str],
    lines: List[str],
    line_objects: List[List[Dict[str, Any]]],
    max_chars: int,
    group_kind: str,
    group_key_prefix: str,
    metadata_builder,
    separator: str = "\n",
) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    current_lines: List[str] = []
    current_objects: List[Dict[str, Any]] = []
    current_len = len(header) + 2 if header else 0
    current_start = 0

    for idx, (line, objs) in enumerate(zip(lines, line_objects)):
        add_len = len(line) + (len(separator) if current_lines else 0)
        if current_lines and current_len + add_len > max_chars:
            text = separator.join(current_lines)
            if header:
                text = f"{header}\n{text}"
            segment = _make_segment(
                objects=current_objects,
                title=title,
                text=text,
                group_kind=group_kind,
                group_key=f"{group_key_prefix}:{current_start + 1}-{idx}",
                metadata_extra=metadata_builder(current_start, idx - 1),
            )
            if segment:
                segments.append(segment)
            current_lines = []
            current_objects = []
            current_len = len(header) + 2 if header else 0
            current_start = idx

        current_lines.append(line)
        current_objects.extend(objs)
        current_len += add_len

    if current_lines and current_objects:
        text = separator.join(current_lines)
        if header:
            text = f"{header}\n{text}"
        segment = _make_segment(
            objects=current_objects,
            title=title,
            text=text,
            group_kind=group_kind,
            group_key=f"{group_key_prefix}:{current_start + 1}-{len(lines)}",
            metadata_extra=metadata_builder(current_start, len(lines) - 1),
        )
        if segment:
            segments.append(segment)

    return segments


def _build_xlsx_segments(
    objects: List[Dict[str, Any]], image_descriptions: Dict[str, List[Dict[str, str]]], max_chars: int
) -> List[Dict[str, Any]]:
    sheets: Dict[str, Dict[str, Any]] = {}
    matched_keys: Set[str] = set()

    for obj in objects:
        if obj.get("source_type") != "xlsx":
            continue
        meta = obj.get("metadata") or {}
        sheet_name = _nested_loc(meta, "xlsx", "sheet") or "Sheet"
        bucket = sheets.setdefault(sheet_name, {"table_rows": {}, "loose_rows": {}, "images": [], "sheet_objects": []})
        bucket["sheet_objects"].append(obj)

        if obj.get("type") in {"text", "cell"} and obj.get("text"):
            if meta.get("search_excluded"):
                continue
            if meta.get("table_role") == "header":
                continue

            row_num = _nested_loc(meta, "xlsx", "row")
            cell_ref = _nested_loc(meta, "xlsx", "cell") or ""
            parsed_row, parsed_col = _parse_cell_ref(cell_ref)
            if not isinstance(row_num, int):
                row_num = parsed_row

            col_index = _nested_loc(meta, "xlsx", "col")
            if not isinstance(col_index, int):
                col_index = _column_index(parsed_col)

            if row_num <= 0:
                continue

            table_id = meta.get("table_id")
            table_role = meta.get("table_role")
            header_text = meta.get("header")
            if (
                isinstance(table_id, str)
                and table_id
                and table_role == "data"
                and isinstance(header_text, str)
                and header_text.strip()
            ):
                table_bucket = bucket["table_rows"].setdefault(
                    table_id,
                    {
                        "title": meta.get("table_title") or sheet_name,
                        "headers": meta.get("table_headers") or [],
                        "rows": {},
                    },
                )
                table_bucket["rows"].setdefault(row_num, []).append((col_index, header_text.strip(), obj["text"], obj))
            else:
                bucket["loose_rows"].setdefault(row_num, []).append((col_index, cell_ref, obj["text"], obj))
        elif obj.get("type") == "image" and obj.get("s3_key"):
            entries = image_descriptions.get(obj["s3_key"]) or []
            if entries:
                matched_keys.add(obj["s3_key"])
                anchor = _nested_loc(meta, "xlsx", "anchor")
                desc = " ".join(entry["text"] for entry in entries if entry.get("text")).strip()
                image_objs = [obj] + [{"id": entry["id"], "type": "image_enrichment", "metadata": meta, "source_type": "xlsx"} for entry in entries]
                bucket["images"].append(
                    {
                        "line": (
                            f"Anchor {anchor}: {desc}"
                            if isinstance(anchor, str) and anchor.strip()
                            else desc
                        ),
                        "objects": image_objs,
                    }
                )

    segments: List[Dict[str, Any]] = []
    for sheet_name, sheet in sheets.items():
        for table_id, table in sheet["table_rows"].items():
            row_numbers = sorted(r for r in table["rows"].keys() if r > 0)
            lines: List[str] = []
            line_objects: List[List[Dict[str, Any]]] = []
            row_sequence: List[int] = []
            for row_num in row_numbers:
                cells = sorted(table["rows"][row_num], key=lambda item: item[0])
                rendered = "\n".join(
                    f"{header}: {text}"
                    for _col, header, text, _obj in cells
                    if isinstance(header, str) and header.strip() and isinstance(text, str) and text.strip()
                )
                if not rendered:
                    continue
                lines.append(rendered)
                line_objects.append([obj for _col, _header, _text, obj in cells])
                row_sequence.append(row_num)

            if not lines:
                continue

            title = table["title"] if isinstance(table.get("title"), str) and table["title"].strip() else sheet_name
            header_lines = [f"Sheet: {sheet_name}"]
            if title != sheet_name:
                header_lines.append(f"Table: {title}")
            segments.extend(
                _pack_line_segments(
                    title=title,
                    header="\n".join(header_lines),
                    lines=lines,
                    line_objects=line_objects,
                    max_chars=max_chars,
                    group_kind="table_row_block",
                    group_key_prefix=f"sheet:{sheet_name}:{table_id}",
                    metadata_builder=lambda start, end, rows=row_sequence, name=sheet_name, current_table_id=table_id: {
                        "loc": {"xlsx": {"sheet": name, "row_start": rows[start], "row_end": rows[end]}},
                        "table_id": current_table_id,
                    },
                    separator="\n\n",
                )
            )

        row_numbers = sorted(r for r in sheet["loose_rows"].keys() if r > 0)
        lines = []
        line_objects = []
        row_sequence = []
        for row_num in row_numbers:
            cells = sorted(sheet["loose_rows"][row_num], key=lambda item: item[0])
            rendered = " | ".join(
                text.strip()
                for _col, _cell_ref, text, _obj in cells
                if isinstance(text, str) and text.strip()
            )
            if not rendered:
                continue
            lines.append(rendered)
            line_objects.append([obj for _col, _cell_ref, _text, obj in cells])
            row_sequence.append(row_num)

        if lines:
            segments.extend(
                _pack_line_segments(
                    title=sheet_name,
                    header=f"Sheet: {sheet_name}",
                    lines=lines,
                    line_objects=line_objects,
                    max_chars=max_chars,
                    group_kind="sheet_cells",
                    group_key_prefix=f"sheet:{sheet_name}:cells",
                    metadata_builder=lambda start, end, rows=row_sequence, name=sheet_name: {
                        "loc": {"xlsx": {"sheet": name, "row_start": rows[start], "row_end": rows[end]}}
                    },
                    separator="\n\n",
                )
            )

        if sheet["images"]:
            image_lines = [img["line"] for img in sheet["images"] if img["line"]]
            image_objects = [img["objects"] for img in sheet["images"] if img["line"]]
            segments.extend(
                _pack_line_segments(
                    title=f"{sheet_name} images",
                    header=f"Sheet: {sheet_name}\nImage descriptions:",
                    lines=[f"- {line}" for line in image_lines],
                    line_objects=image_objects,
                    max_chars=max_chars,
                    group_kind="sheet_images",
                    group_key_prefix=f"sheet:{sheet_name}:images",
                    metadata_builder=lambda _start, _end, name=sheet_name: {"loc": {"xlsx": {"sheet": name}}},
                )
            )

    for key, entries in image_descriptions.items():
        if key in matched_keys:
            continue
        desc = " ".join(entry["text"] for entry in entries if entry.get("text")).strip()
        if not desc:
            continue
        dummy_objects = [
            {"id": entry["id"], "type": "image_enrichment", "metadata": {}, "source_type": "xlsx"}
            for entry in entries
        ]
        segment = _make_segment(
            objects=dummy_objects,
            title="Workbook images",
            text=f"Image description: {desc}",
            group_kind="sheet_images",
            group_key=f"unmatched-image:{key}",
            metadata_extra={"s3_key": key},
        )
        if segment:
            segments.append(segment)

    return segments


def _build_pptx_segments(
    objects: List[Dict[str, Any]], image_descriptions: Dict[str, List[Dict[str, str]]]
) -> List[Dict[str, Any]]:
    slides: Dict[int, Dict[str, Any]] = {}
    matched_keys: Set[str] = set()

    for obj in objects:
        if obj.get("source_type") != "pptx":
            continue
        meta = obj.get("metadata") or {}
        slide = _nested_loc(meta, "pptx", "slide")
        if not isinstance(slide, int):
            continue
        bucket = slides.setdefault(slide, {"texts": [], "images": [], "objects": []})
        bucket["objects"].append(obj)

        if obj.get("type") == "text" and obj.get("text"):
            bucket["texts"].append((obj["text"], obj))
        elif obj.get("type") == "image" and obj.get("s3_key"):
            entries = image_descriptions.get(obj["s3_key"]) or []
            if entries:
                matched_keys.add(obj["s3_key"])
                desc = " ".join(entry["text"] for entry in entries if entry.get("text")).strip()
                image_objs = [obj] + [{"id": entry["id"], "type": "image_enrichment", "metadata": meta, "source_type": "pptx"} for entry in entries]
                bucket["images"].append((desc, image_objs))

    segments: List[Dict[str, Any]] = []
    for slide, bucket in slides.items():
        text_lines = [text for text, _obj in bucket["texts"] if text]
        image_lines = [text for text, _objs in bucket["images"] if text]

        parts: List[str] = []
        if text_lines:
            parts.append("\n".join(text_lines))
        if image_lines:
            parts.append("Image descriptions:\n" + "\n".join(f"- {line}" for line in image_lines))

        text = "\n\n".join(part for part in parts if part).strip()
        objects_for_segment: List[Dict[str, Any]] = []
        for _text, obj in bucket["texts"]:
            objects_for_segment.append(obj)
        for _text, image_objs in bucket["images"]:
            objects_for_segment.extend(image_objs)

        title = None
        if text_lines and _looks_like_heading(text_lines[0]):
            title = text_lines[0]
        elif image_lines and not text_lines:
            title = f"Slide {slide} images"

        segment = _make_segment(
            objects=objects_for_segment,
            title=title,
            text=f"Slide {slide}\n{text}" if text else "",
            group_kind="slide",
            group_key=f"slide:{slide}",
            metadata_extra={"loc": {"pptx": {"slide": slide}}},
        )
        if segment:
            segments.append(segment)

    for key, entries in image_descriptions.items():
        if key in matched_keys:
            continue
        desc = " ".join(entry["text"] for entry in entries if entry.get("text")).strip()
        if not desc:
            continue
        dummy_objects = [
            {"id": entry["id"], "type": "image_enrichment", "metadata": {}, "source_type": "pptx"}
            for entry in entries
        ]
        segment = _make_segment(
            objects=dummy_objects,
            title="Slide images",
            text=f"Image description: {desc}",
            group_kind="slide_images",
            group_key=f"unmatched-image:{key}",
            metadata_extra={"s3_key": key},
        )
        if segment:
            segments.append(segment)

    return segments


def _build_docx_segments(
    objects: List[Dict[str, Any]], image_descriptions: Dict[str, List[Dict[str, str]]], max_chars: int
) -> List[Dict[str, Any]]:
    blocks: List[Tuple[str, Dict[str, Any]]] = []
    matched_keys: Set[str] = set()

    for obj in objects:
        if obj.get("source_type") != "docx":
            continue
        text = obj.get("text")
        if obj.get("type") in {"text", "table"} and text:
            blocks.append((text, obj))
            continue
        if obj.get("type") == "image" and obj.get("s3_key"):
            entries = image_descriptions.get(obj["s3_key"]) or []
            if not entries:
                continue
            matched_keys.add(obj["s3_key"])
            desc = " ".join(entry["text"] for entry in entries if entry.get("text")).strip()
            image_objs = [obj] + [{"id": entry["id"], "type": "image_enrichment", "metadata": obj.get("metadata") or {}, "source_type": "docx"} for entry in entries]
            blocks.append((f"Image description: {desc}", {"id": obj["id"], "type": "image_bundle", "text": desc, "metadata": obj.get("metadata") or {}, "source_type": "docx", "bundle_objects": image_objs}))

    segments: List[Dict[str, Any]] = []
    current_texts: List[str] = []
    current_objects: List[Dict[str, Any]] = []
    segment_index = 1
    current_len = 0

    def flush() -> None:
        nonlocal current_texts, current_objects, segment_index, current_len
        if not current_texts or not current_objects:
            current_texts = []
            current_objects = []
            current_len = 0
            return
        title = current_texts[0] if _looks_like_heading(current_texts[0]) else None
        first_loc = (current_objects[0].get("metadata") or {}).get("loc")
        last_loc = (current_objects[-1].get("metadata") or {}).get("loc")
        segment = _make_segment(
            objects=current_objects,
            title=title,
            text="\n\n".join(current_texts),
            group_kind="document_flow",
            group_key=f"docx:{segment_index}",
            metadata_extra={"loc": first_loc, "loc_end": last_loc},
        )
        if segment:
            segments.append(segment)
            segment_index += 1
        current_texts = []
        current_objects = []
        current_len = 0

    for text, obj in blocks:
        block_objects = obj.get("bundle_objects") if isinstance(obj.get("bundle_objects"), list) else [obj]
        add_len = len(text) + (2 if current_texts else 0)
        if current_texts and (_looks_like_heading(text) and current_len >= int(max_chars * 0.5)):
            flush()
        if current_texts and current_len + add_len > max_chars:
            flush()
        current_texts.append(text)
        current_objects.extend(block_objects)
        current_len += add_len

    flush()

    for key, entries in image_descriptions.items():
        if key in matched_keys:
            continue
        desc = " ".join(entry["text"] for entry in entries if entry.get("text")).strip()
        if not desc:
            continue
        dummy_objects = [
            {"id": entry["id"], "type": "image_enrichment", "metadata": {}, "source_type": "docx"}
            for entry in entries
        ]
        segment = _make_segment(
            objects=dummy_objects,
            title="Document images",
            text=f"Image description: {desc}",
            group_kind="document_flow",
            group_key=f"unmatched-image:{key}",
            metadata_extra={"s3_key": key},
        )
        if segment:
            segments.append(segment)

    return segments


def _build_pdf_segments(objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    pages: Dict[int, Dict[str, Any]] = {}
    for obj in objects:
        if obj.get("source_type") != "pdf" or obj.get("type") != "text" or not obj.get("text"):
            continue
        meta = obj.get("metadata") or {}
        page = _nested_loc(meta, "pdf", "page")
        page_num = int(page) if isinstance(page, int) else 0
        bucket = pages.setdefault(page_num, {"lines": [], "objects": []})
        bucket["lines"].append(obj["text"])
        bucket["objects"].append(obj)

    segments: List[Dict[str, Any]] = []
    for page_num, bucket in pages.items():
        label = f"Page {page_num}" if page_num else "PDF"
        segment = _make_segment(
            objects=bucket["objects"],
            title=label,
            text=f"{label}\n" + "\n".join(bucket["lines"]),
            group_kind="page",
            group_key=f"page:{page_num}",
            metadata_extra={"loc": {"pdf": {"page": page_num}} if page_num else {"pdf": {}}},
        )
        if segment:
            segments.append(segment)
    return segments


def _build_generic_segments(objects: List[Dict[str, Any]], max_chars: int) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    current_texts: List[str] = []
    current_objects: List[Dict[str, Any]] = []
    current_len = 0
    segment_index = 1

    def flush() -> None:
        nonlocal current_texts, current_objects, current_len, segment_index
        if not current_texts or not current_objects:
            current_texts = []
            current_objects = []
            current_len = 0
            return
        title = current_texts[0] if _looks_like_heading(current_texts[0]) else None
        segment = _make_segment(
            objects=current_objects,
            title=title,
            text="\n\n".join(current_texts),
            group_kind="generic",
            group_key=f"generic:{segment_index}",
        )
        if segment:
            segments.append(segment)
            segment_index += 1
        current_texts = []
        current_objects = []
        current_len = 0

    for obj in objects:
        text = obj.get("text")
        if not text:
            continue
        add_len = len(text) + (2 if current_texts else 0)
        if current_texts and current_len + add_len > max_chars:
            flush()
        current_texts.append(text)
        current_objects.append(obj)
        current_len += add_len

    flush()
    return segments


def _build_segments(
    objects: List[Dict[str, Any]], image_descriptions: Dict[str, List[Dict[str, str]]], max_chars: int
) -> List[Dict[str, Any]]:
    source_types = {obj.get("source_type") for obj in objects if isinstance(obj.get("source_type"), str)}

    if source_types == {"xlsx"}:
        segments = _build_xlsx_segments(objects, image_descriptions, max_chars)
    elif source_types == {"pptx"}:
        segments = _build_pptx_segments(objects, image_descriptions)
    elif source_types == {"docx"}:
        segments = _build_docx_segments(objects, image_descriptions, max_chars)
    elif source_types == {"pdf"}:
        segments = _build_pdf_segments(objects)
    else:
        segments = []

    if segments:
        return segments
    return _build_generic_segments(objects, max_chars)


def _emit_chunks(
    *,
    document_id: str,
    input_source: str,
    segments: List[Dict[str, Any]],
    max_chars: int,
) -> List[str]:
    chunks_out: List[str] = []
    chunk_count = 0

    for segment in segments:
        parts = _split_text(segment["text"], max_chars)
        for part_idx, part in enumerate(parts):
            chunk_count += 1
            chunk_id = f"chunk_{chunk_count:06d}"
            source_object_ids = list(segment["source_object_ids"])
            metadata = dict(segment["metadata"])
            metadata.update(
                {
                    "source": input_source,
                    "object_type": (
                        segment["object_types"][0]
                        if len(segment["object_types"]) == 1
                        else "mixed"
                    ),
                    "object_types": segment["object_types"],
                    "source_object_id": source_object_ids[0] if source_object_ids else None,
                    "source_object_ids": source_object_ids,
                    "part_index": part_idx,
                }
            )

            chunk = {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "chunk_type": metadata.get("group_kind"),
                "source_object_ids": source_object_ids,
                "title": segment.get("title"),
                "text": part,
                "metadata": {k: v for k, v in metadata.items() if v is not None},
            }
            chunks_out.append(json.dumps(chunk, ensure_ascii=False))

    return chunks_out


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """ChunkBuild Lambda.

    Prefer structured/enriched_objects.jsonl if present, otherwise structured/objects.jsonl.

    Output:
      - search/chunks/chunks.jsonl (JSONL)
        {chunk_id, document_id, chunk_type, source_object_ids, title, text, metadata}

    Env:
      - CHUNK_MAX_CHARS (default 2000)
    """

    bucket = _require_str(event, "bucket", "Bucket")
    work_prefix = _require_str(event, "work_prefix", "workPrefix", "prefix")
    document_id = event.get("document_id") or event.get("documentId") or event.get("id")
    document_id = document_id if isinstance(document_id, str) and document_id.strip() else "unknown"

    max_chars = int(os.getenv("CHUNK_MAX_CHARS", "2000"))

    enriched_objects_key = _join_s3_key(work_prefix, "structured/enriched_objects.jsonl")
    objects_key = _join_s3_key(work_prefix, "structured/objects.jsonl")

    if _s3_exists(bucket, enriched_objects_key):
        input_key = enriched_objects_key
        input_source = "enriched"
    else:
        input_key = objects_key
        input_source = "raw"

    out_key = _join_s3_key(work_prefix, "search/chunks/chunks.jsonl")

    records = list(_s3_get_jsonl(bucket, input_key))
    object_count = len(records)
    default_source_type = (
        event.get("source_type").strip().lower()
        if isinstance(event.get("source_type"), str) and event.get("source_type").strip()
        else "unknown"
    )
    objects, image_descriptions = _normalize_records(records, default_source_type)
    segments = _build_segments(objects, image_descriptions, max_chars)
    chunks_out = _emit_chunks(
        document_id=document_id,
        input_source=input_source,
        segments=segments,
        max_chars=max_chars,
    )

    _s3_put_jsonl(bucket, out_key, chunks_out)

    out = dict(event)
    out["bucket"] = bucket
    out["work_prefix"] = work_prefix
    out["document_id"] = document_id
    out["chunk_build"] = {
        "input": input_source,
        "objects_seen": object_count,
        "segments_built": len(segments),
        "chunks_written": len(chunks_out),
        "max_chars": max_chars,
        "finished_at": _now_iso(),
    }
    return out
