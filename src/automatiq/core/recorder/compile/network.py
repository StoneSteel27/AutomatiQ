"""Network request compilation — builds per-request transaction folders."""

import json
import logging
import os
import shutil
import traceback
from urllib.parse import urlparse

from ... import events
from .serializers import (
    MAGIKA_AVAILABLE,
    detect_content_type,
    extract_cookies_sent,
    extract_cookies_set,
    get_header_val,
    make_serializable,
    sanitize_filename,
    save_content,
)

logger = logging.getLogger(__name__)


def process_network_requests(
    requests_file_path: str, temp_data_dir: str, requests_dir: str, output_dir: str
) -> tuple[list[dict], dict, dict]:
    timeline_requests = []
    detection_stats = {"request_detected": 0, "response_detected": 0, "mismatches": 0}

    # Track statistics
    stats = {"methods": {}, "domains": {}, "status_codes": {}, "with_auth": 0, "with_cookies": 0}

    if not os.path.exists(requests_file_path):
        return timeline_requests, detection_stats, stats

    with open(requests_file_path, encoding="utf-8") as f:
        for idx, line in enumerate(f):
            try:
                item = json.loads(line)

                # Update statistics
                method = item.get("method", "UNKNOWN")
                stats["methods"][method] = stats["methods"].get(method, 0) + 1
                domain = urlparse(item.get("url", "")).netloc
                if domain:
                    stats["domains"][domain] = stats["domains"].get(domain, 0) + 1

                status = item.get("response_data", {}).get("status") if item.get("response_data") else None
                if status:
                    stats["status_codes"][str(status)] = stats["status_codes"].get(str(status), 0) + 1

                req_headers = item.get("headers", {})
                if get_header_val(req_headers, "authorization"):
                    stats["with_auth"] += 1
                if item.get("cookies_sent_details"):
                    stats["with_cookies"] += 1

                parsed_url = urlparse(item.get("url", ""))
                domain = parsed_url.netloc or "unknown"
                folder_name = f"{idx:03d}_{item.get('method', 'UNK')}_{sanitize_filename(domain)}"
                req_root = os.path.join(requests_dir, folder_name)
                os.makedirs(req_root, exist_ok=True)

                req_headers = item.get("headers", {})

                request_detection = None
                if item.get("post_data"):
                    request_detection = detect_content_type(item["post_data"])
                    if request_detection:
                        detection_stats["request_detected"] += 1

                res_data = item.get("response_data") or {}
                res_headers = res_data.get("headers") or {}

                response_detection = None
                declared_mime = res_data.get("mime_type", "unknown")

                transaction_data = {
                    "metadata": {
                        "index": idx,
                        "unique_id": item.get("unique_id"),
                        "method": item.get("method"),
                        "url": item.get("url"),
                        "status": res_data.get("status"),
                        "redirected": bool(item.get("redirected")),
                        "redirected_to_url": item.get("redirected_to_url"),
                        "timing": {
                            "request_sent_unix": item.get("timestamp_unix"),
                            "response_received_unix": item.get("response_timing", {}).get("received_unix"),
                            "loading_finished_unix": item.get("response_timing", {}).get("finished_unix"),
                            "duration_ms": item.get("response_timing", {}).get("total_duration_ms"),
                        },
                        "security": {
                            "has_authorization": bool(get_header_val(req_headers, "authorization")),
                            "has_proxy_authorization": bool(get_header_val(req_headers, "proxy-authorization")),
                            "has_challenge": bool(get_header_val(res_headers, "www-authenticate")),
                        },
                    },
                    "request": {
                        "headers": req_headers,
                        "cookies_sent": extract_cookies_sent(item),
                        "cookies_sent_detailed": item.get("cookies_sent_details", []),
                        "content_detection": request_detection,
                        "has_payload": bool(item.get("post_data")),
                    },
                    "response": {
                        "headers": res_headers,
                        "cookies_set": extract_cookies_set(item),
                        "cookies_set_detailed": item.get("cookies_received_details", {}),
                        "content_detection": response_detection,  # Will be updated if detected from file
                        "has_body": bool(res_data.get("body_file")),
                        "mime_mismatch": False,  # Will be updated if detected from file
                    },
                }

                with open(os.path.join(req_root, "transaction.json"), "w") as f:
                    json.dump(make_serializable(transaction_data), f, indent=2)

                if item.get("post_data"):
                    ext = request_detection.get("extension", "bin") if request_detection else "bin"
                    save_content(os.path.join(req_root, f"req_payload.{ext}"), item["post_data"])

                if res_data and res_data.get("body_file"):
                    body_file_path = os.path.join(temp_data_dir, res_data["body_file"])
                    if os.path.exists(body_file_path):
                        # Detect content type FIRST so we use the correct extension when copying
                        if not response_detection and MAGIKA_AVAILABLE:
                            try:
                                with open(body_file_path, "rb") as bf:
                                    chunk = bf.read(1024)
                                    if chunk:
                                        response_detection = detect_content_type(chunk)
                                        if response_detection:
                                            detection_stats["response_detected"] += 1
                                            transaction_data["response"]["content_detection"] = response_detection
                                            detected_mime = response_detection.get("mime_type", "unknown")
                                            if declared_mime != detected_mime and declared_mime != "unknown":
                                                detection_stats["mismatches"] += 1
                                                transaction_data["response"]["mime_mismatch"] = True
                                            with open(os.path.join(req_root, "transaction.json"), "w") as tf:
                                                json.dump(make_serializable(transaction_data), tf, indent=2)
                            except Exception as magika_e:
                                events.log_warn.send(
                                    "recorder",
                                    text=f"Could not detect content type of file {body_file_path}: {magika_e}",
                                )

                        ext = response_detection.get("extension", "bin") if response_detection else "bin"
                        dest_path = os.path.join(req_root, f"res_body.{ext}")
                        shutil.copy(body_file_path, dest_path)

                timeline_requests.append(
                    {
                        "timestamp": item.get("timestamp_unix", 0),
                        "timestamp_iso": item.get("timestamp_iso"),
                        "event_type": "network_request",
                        "method": item.get("method"),
                        "url": item.get("url"),
                        "status": res_data.get("status", -1),
                        "folder": f"requests/{folder_name}",
                        "redirected": bool(item.get("redirected")),
                        "redirected_to_url": item.get("redirected_to_url"),
                    }
                )

            except Exception as e:
                events.log_error.send("recorder", text=f"Failed to process request at index {idx}: {e}")
                events.log_traceback.send("recorder")
                error_filename = os.path.join(output_dir, f"CRASH_REPORT_{idx:03d}.txt")
                try:
                    with open(error_filename, "w", encoding="utf-8") as debug_f:
                        debug_f.write(f"ERROR: {str(e)}\n" + "-" * 50 + "\n")
                        debug_f.write(traceback.format_exc() + "\n" + "-" * 50 + "\n")
                except OSError as write_exc:
                    events.log_warn.send(
                        "recorder", text=f"Could not write crash report to {error_filename}: {write_exc}"
                    )
                    events.log_traceback.send("recorder")
                continue

    return timeline_requests, detection_stats, stats
