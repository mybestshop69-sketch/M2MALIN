from __future__ import annotations

import os
import sys
from typing import Any, Callable

import requests


EXPECTED_APP_ID = "1551714796659004"
REQUIRED_SCOPES = {
    "pages_messaging",
    "pages_manage_metadata",
    "pages_show_list",
    "pages_read_engagement",
}


def check_meta_app_credentials(
    app_id: str,
    app_secret: str,
    expected_app_id: str = EXPECTED_APP_ID,
    graph_version: str = "v23.0",
    get: Callable[..., Any] = requests.get,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "app_id_valid": False,
        "app_id_detected": "",
        "expected_app_id": expected_app_id,
        "app_secret_valid": False,
    }
    if not app_id or not app_secret:
        return result

    try:
        response = get(
            f"https://graph.facebook.com/{graph_version}/{app_id}",
            params={"fields": "id", "access_token": f"{app_id}|{app_secret}"},
            timeout=10,
        )
        if not getattr(response, "ok", False):
            return result
        payload = response.json()
    except Exception:
        return result

    detected = str(payload.get("id") or "")
    result["app_id_detected"] = detected
    result["app_secret_valid"] = detected == app_id
    result["app_id_valid"] = app_id == expected_app_id and detected == expected_app_id
    return result


def summarize_token_debug(payload: dict[str, Any], expected_app_id: str = EXPECTED_APP_ID) -> dict[str, Any]:
    data = payload.get("data") or {}
    scopes = set(str(data.get("scopes") or "").split(","))
    app_id = str(data.get("app_id") or "")
    return {
        "token_app_valid": app_id == expected_app_id,
        "token_app_id_detected": app_id,
        "page_id": str(data.get("profile_id") or ""),
        "expires_at": data.get("expires_at"),
        "missing_scopes": sorted(REQUIRED_SCOPES - scopes),
    }


def main() -> int:
    expected_app_id = os.getenv("EXPECTED_META_APP_ID", EXPECTED_APP_ID)
    app_id = os.getenv("META_APP_ID", "")
    app_secret = os.getenv("META_APP_SECRET", "")
    graph_version = os.getenv("META_GRAPH_VERSION", "v23.0")
    result = check_meta_app_credentials(app_id, app_secret, expected_app_id, graph_version)

    print(f"app_id_valid={str(result['app_id_valid']).lower()}")
    print(f"app_id_detected={result['app_id_detected']}")
    print(f"expected_app_id={result['expected_app_id']}")
    print(f"app_secret_valid={str(result['app_secret_valid']).lower()}")
    return 0 if result["app_id_valid"] and result["app_secret_valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
