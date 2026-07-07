from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse
import math
import os
import tempfile
import time

import requests


class SocialApiError(RuntimeError):
    """Raised when a social network API rejects a request."""


def _json_or_raise(response: requests.Response, platform: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SocialApiError(
            f"{platform}: réponse non JSON ({response.status_code})."
        ) from exc

    if not response.ok:
        message = payload.get("error", payload)
        raise SocialApiError(f"{platform}: {message}")
    return payload


@dataclass(slots=True)
class MetaClient:
    graph_version: str
    access_token: str

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_version}"

    def get_pages(self) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.base_url}/me/accounts",
            params={
                "fields": "id,name,access_token",
                "access_token": self.access_token,
            },
            timeout=30,
        )
        return _json_or_raise(response, "Meta").get("data", [])

    def get_instagram_account(self, page_id: str, page_token: str) -> str | None:
        response = requests.get(
            f"{self.base_url}/{page_id}",
            params={
                "fields": "instagram_business_account",
                "access_token": page_token,
            },
            timeout=30,
        )
        payload = _json_or_raise(response, "Meta")
        account = payload.get("instagram_business_account")
        return account.get("id") if account else None

    def publish_facebook(
        self,
        page_id: str,
        caption: str,
        media_url: str | None,
        media_type: str,
    ) -> dict[str, Any]:
        common = {"access_token": self.access_token}
        if media_url and media_type == "image":
            endpoint = f"{self.base_url}/{page_id}/photos"
            data = {**common, "url": media_url, "caption": caption}
        elif media_url and media_type == "video":
            endpoint = f"{self.base_url}/{page_id}/videos"
            data = {**common, "file_url": media_url, "description": caption}
        else:
            endpoint = f"{self.base_url}/{page_id}/feed"
            data = {**common, "message": caption}

        response = requests.post(endpoint, data=data, timeout=90)
        return _json_or_raise(response, "Facebook")

    def publish_instagram(
        self,
        instagram_user_id: str,
        caption: str,
        media_url: str,
        media_type: str,
    ) -> dict[str, Any]:
        if not media_url:
            raise SocialApiError("Instagram exige une image ou une vidéo publique.")

        creation_data: dict[str, Any] = {
            "caption": caption,
            "access_token": self.access_token,
        }
        if media_type == "video":
            creation_data.update({"media_type": "REELS", "video_url": media_url})
        else:
            creation_data["image_url"] = media_url

        create_response = requests.post(
            f"{self.base_url}/{instagram_user_id}/media",
            data=creation_data,
            timeout=90,
        )
        creation = _json_or_raise(create_response, "Instagram")
        creation_id = creation.get("id")
        if not creation_id:
            raise SocialApiError("Instagram: identifiant de création absent.")

        self._wait_for_instagram_media(creation_id)

        publish_response = requests.post(
            f"{self.base_url}/{instagram_user_id}/media_publish",
            data={
                "creation_id": creation_id,
                "access_token": self.access_token,
            },
            timeout=90,
        )
        return _json_or_raise(publish_response, "Instagram")

    def _wait_for_instagram_media(
        self,
        creation_id: str,
        timeout_seconds: int = 180,
        poll_interval_seconds: int = 5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_status: dict[str, Any] = {}

        while time.monotonic() < deadline:
            status_response = requests.get(
                f"{self.base_url}/{creation_id}",
                params={
                    "fields": "status_code,status",
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            last_status = _json_or_raise(status_response, "Instagram")
            status_code = str(last_status.get("status_code", "")).upper()

            if status_code in {"FINISHED", "PUBLISHED"}:
                return last_status
            if status_code in {"ERROR", "EXPIRED"}:
                details = last_status.get("status") or status_code
                raise SocialApiError(
                    f"Instagram: le traitement du média a échoué ({details})."
                )

            time.sleep(poll_interval_seconds)

        details = last_status.get("status") or last_status.get("status_code") or "IN_PROGRESS"
        raise SocialApiError(
            "Instagram: le média n'est pas prêt après "
            f"{timeout_seconds} secondes (statut: {details})."
        )


@dataclass(slots=True)
class TikTokClient:
    access_token: str
    _MAX_CHUNK_SIZE: ClassVar[int] = 64 * 1024 * 1024
    _VIDEO_CONTENT_TYPES: ClassVar[dict[str, str]] = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
    }

    def creator_info(self) -> dict[str, Any]:
        response = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={},
            timeout=30,
        )
        return _json_or_raise(response, "TikTok").get("data", {})

    def publish(
        self,
        caption: str,
        media_url: str,
        media_type: str,
        privacy_level: str = "SELF_ONLY",
    ) -> dict[str, Any]:
        if not media_url:
            raise SocialApiError("TikTok exige une URL publique de média.")

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        if media_type == "image":
            endpoint = "https://open.tiktokapis.com/v2/post/publish/content/init/"
            body = {
                "post_info": {
                    "title": caption[:90],
                    "description": caption,
                    "disable_comment": False,
                    "privacy_level": privacy_level,
                    "auto_add_music": True,
                },
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "photo_cover_index": 0,
                    "photo_images": [media_url],
                },
                "post_mode": "DIRECT_POST",
                "media_type": "PHOTO",
            }
            response = requests.post(endpoint, headers=headers, json=body, timeout=90)
            return _json_or_raise(response, "TikTok")

        temp_path: str | None = None
        try:
            temp_path, video_size, content_type = self._download_video(media_url)
            chunk_size = video_size if video_size < self._MAX_CHUNK_SIZE else self._MAX_CHUNK_SIZE
            total_chunk_count = math.ceil(video_size / chunk_size)
            endpoint = "https://open.tiktokapis.com/v2/post/publish/video/init/"
            body = {
                "post_info": {
                    "title": caption,
                    "privacy_level": privacy_level,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                    "video_cover_timestamp_ms": 1000,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunk_count,
                },
            }

            response = requests.post(endpoint, headers=headers, json=body, timeout=90)
            payload = _json_or_raise(response, "TikTok")
            data = payload.get("data", {})
            upload_url = data.get("upload_url")
            publish_id = data.get("publish_id")
            if not upload_url or not publish_id:
                raise SocialApiError("TikTok: URL d'upload ou identifiant de publication absent.")

            self._upload_video_file(temp_path, upload_url, video_size, chunk_size, content_type)
            return payload
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass

    def _download_video(self, media_url: str) -> tuple[str, int, str]:
        parsed = urlparse(media_url)
        if parsed.scheme.lower() != "https":
            raise SocialApiError("TikTok exige une URL vidéo HTTPS.")

        extension = Path(parsed.path).suffix.lower()
        content_type = self._VIDEO_CONTENT_TYPES.get(extension)
        if not content_type:
            raise SocialApiError("TikTok accepte uniquement les vidéos MP4, MOV ou WebM.")

        temp_path: str | None = None
        try:
            with requests.get(media_url, stream=True, timeout=90) as response:
                if not response.ok:
                    raise SocialApiError(f"TikTok: téléchargement vidéo impossible ({response.status_code}).")

                response_content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if response_content_type and not (
                    response_content_type == content_type
                    or response_content_type == "application/octet-stream"
                    or response_content_type.startswith("video/")
                ):
                    raise SocialApiError("TikTok: le fichier téléchargé n'est pas une vidéo valide.")

                with tempfile.NamedTemporaryFile(delete=False, suffix=extension) as temp_file:
                    temp_path = temp_file.name
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            temp_file.write(chunk)

            video_size = os.path.getsize(temp_path)
            if video_size <= 0:
                raise SocialApiError("TikTok: la vidéo téléchargée est vide.")
            return temp_path, video_size, content_type
        except Exception:
            if temp_path:
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass
            raise

    def _upload_video_file(
        self,
        temp_path: str,
        upload_url: str,
        video_size: int,
        chunk_size: int,
        content_type: str,
    ) -> None:
        with open(temp_path, "rb") as video_file:
            start = 0
            while start < video_size:
                chunk = video_file.read(chunk_size)
                if not chunk:
                    break

                end = start + len(chunk) - 1
                response = requests.put(
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Type": content_type,
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{video_size}",
                    },
                    timeout=180,
                )
                if not response.ok:
                    self._raise_upload_error(response)
                start = end + 1

    def _raise_upload_error(self, response: requests.Response) -> None:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SocialApiError(f"TikTok: upload vidéo refusé ({response.status_code}).") from exc
        raise SocialApiError(f"TikTok: upload vidéo refusé ({payload.get('error', payload)}).")
