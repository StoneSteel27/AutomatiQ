"""Serialization & content-detection helpers for workspace compilation."""

import base64
import logging
import re

from ... import config, events

logger = logging.getLogger(__name__)

try:
    from magika import Magika

    magika_detector = Magika()
    MAGIKA_AVAILABLE = True
    events.log_debug.send("recorder", text="Magika AI detector initialized successfully.")
except ImportError:
    magika_detector = None
    MAGIKA_AVAILABLE = False
    events.log_warn.send("recorder", text="Magika not installed. Skipping advanced content type detection.")

WORKSPACE_DIR = str(config.WORKSPACE_DIR)


def sanitize_filename(name: str) -> str:
    name = name.replace("https://", "").replace("http://", "")
    return re.sub(r"[^\w\-\.]", "_", name)[:100]


def make_serializable(obj):
    if isinstance(obj, str | int | float | bool | type(None)):
        return obj
    if isinstance(obj, bytes):
        return ""
    if isinstance(obj, list):
        return [make_serializable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    return str(obj)


def get_header_val(headers, key):
    if not headers:
        return None
    key = key.lower()
    for k, v in headers.items():
        if k.lower() == key:
            return v
    return None


def extract_cookies_sent(item):
    names = set()
    details = item.get("cookies_sent_details", [])
    for ac in details:
        if ac.get("blockedReasons"):
            continue
        cookie = ac.get("cookie") or {}
        name = cookie.get("name")
        if name:
            names.add(name)
    if not names:
        raw = get_header_val(item.get("headers", {}), "cookie")
        if raw:
            for part in raw.split(";"):
                if "=" in part:
                    names.add(part.split("=")[0].strip())
    return sorted(names)


def extract_cookies_set(item):
    names = set()
    resp = item.get("response_data") or {}
    headers = resp.get("headers") or {}
    raw = get_header_val(headers, "set-cookie")
    if raw:
        for line in raw.split("\n"):
            if "=" in line:
                names.add(line.split("=")[0].strip())
    return sorted(list(names))


def detect_content_type(content, is_base64=False):
    if content is None or not MAGIKA_AVAILABLE or magika_detector is None:
        return None
    try:
        if is_base64:
            byte_content = base64.b64decode(content)
        elif isinstance(content, str):
            byte_content = content.encode("utf-8")
        elif isinstance(content, bytes):
            byte_content = content
        else:
            return None
        result = magika_detector.identify_bytes(byte_content)
        return {
            "label": result.output.label,
            "mime_type": result.output.mime_type,
            "extension": result.output.extensions[0] if result.output.extensions else "bin",
            "all_extensions": result.output.extensions,
            "description": result.output.description,
            "confidence": result.score,
            "is_text": result.output.is_text,
            "group": result.output.group,
        }
    except Exception as e:
        events.log_warn.send("recorder", text=f"Magika error: {e}")
        events.log_traceback.send("recorder")
        return {"label": "unknown", "mime_type": "application/octet-stream", "extension": "bin", "error": str(e)}


def save_content(path, content, is_base64=False):
    if content is None:
        return
    mode = "wb"
    if is_base64:
        try:
            data = base64.b64decode(content)
        except Exception as exc:
            events.log_warn.send("recorder", text=f"Base64 decode failed for {path}, saving raw content instead: {exc}")
            events.log_traceback.send("recorder")
            data = str(content).encode("utf-8")
    elif isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = str(content).encode("utf-8")
    try:
        with open(path, mode) as f:
            f.write(data)
    except OSError as exc:
        events.log_error.send("recorder", text=f"Failed to write content to {path}: {exc}")
        events.log_traceback.send("recorder")
