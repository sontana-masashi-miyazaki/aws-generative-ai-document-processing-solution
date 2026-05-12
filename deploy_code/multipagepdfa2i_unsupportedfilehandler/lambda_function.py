from typing import Any, Dict


def lambda_handler(event, context):
    # Optional handler for Step Functions branches that want a terminal "unsupported" outcome.
    # Do not log document content; only metadata.
    doc_id = (event or {}).get("document_id")
    source_type = (event or {}).get("source_type")
    print(f"UnsupportedFileHandler: document_id={doc_id} source_type={source_type}")

    out: Dict[str, Any] = dict(event or {})
    out["unsupported_file"] = True
    out["unsupported_reason"] = out.get("unsupported_reason") or "UNSUPPORTED_FILE_TYPE"
    return out
