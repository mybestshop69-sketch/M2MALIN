from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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

        publish_response = requests.post(
            f"{self.base_url}/{instagram_user_id}/media_publish",
            data={
                "creation_id": creation_id,
                "access_token": self.access_token,
            },
            timeout=90,
        )
        return _json_or_raise(publish_response, "Instagram")


@dataclass(slots=True)
class TikTokClient:
    access_token: str

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
        privacy_level: str = "PUBLIC_TO_EVERYONE",
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
        else:
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
                    "source": "PULL_FROM_URL",
                    "video_url": media_url,
                },
            }

        response = requests.post(endpoint, headers=headers, json=body, timeout=90)
        return _json_or_raise(response, "TikTok")
