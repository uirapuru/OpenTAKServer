"""taklab: video analysis on demand - settings, signed file URL, service client.

Contract
    The taklab-vision service (taklab repo, src/taklab/vision/server.py) exposes
    /v1/env/* routes for one environment. OTS calls them with
    ``Authorization: Bearer <token>`` and ``X-Taklab-Vision-Client: <env>``.
    The worker downloads a recording through a short-lived signed URL served by
    OTS itself (/api/taklab/vision/recordings/<id>/file).

Settings
    Only from the environment (the Helm chart): OTS_TAKLAB_VISION_URL,
    OTS_TAKLAB_VISION_TOKEN, OTS_TAKLAB_VISION_CLIENT, plus OTS_TAKLAB_HOST for
    the public host. Any of the first three empty means the feature is off:
    settings() returns None and callers answer 404/disabled.

Security
    The signature is HMAC-SHA256 with the service token over
    "taklab-vision-file:<recording id>:<expires>", so it binds exactly one
    recording and one expiry. It is compared in constant time. The token, the
    signature and the full signed URL must never be logged.
"""

import hashlib
import hmac
import os
from dataclasses import dataclass
from urllib.parse import quote

import requests

FILE_URL_TTL_S = 21600
SIGNATURE_CONTEXT = "taklab-vision-file:"
_EXPIRY_SLACK_S = 60

_session = requests.Session()


@dataclass(frozen=True)
class VisionSettings:
    url: str
    token: str
    client: str
    host: str


class VisionUnavailable(Exception):
    """The vision service could not be reached."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def settings() -> "VisionSettings | None":
    url = _env("OTS_TAKLAB_VISION_URL")
    token = _env("OTS_TAKLAB_VISION_TOKEN")
    client = _env("OTS_TAKLAB_VISION_CLIENT")
    if not (url and token and client):
        return None
    return VisionSettings(url, token, client, _env("OTS_TAKLAB_HOST"))


def file_signature(token: str, rec_id: str, expires: int) -> str:
    message = f"{SIGNATURE_CONTEXT}{rec_id}:{expires}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def signed_file_url(cfg: VisionSettings, rec_id: str, now: float) -> str:
    expires = int(now) + FILE_URL_TTL_S
    signature = file_signature(cfg.token, rec_id, expires)
    return (
        f"https://{cfg.host}/api/taklab/vision/recordings/{quote(rec_id, safe='')}/file"
        f"?expires={expires}&signature={signature}"
    )


def signature_valid(
    cfg: VisionSettings, rec_id: str, expires_raw: "str | None", signature: "str | None", now: float
) -> bool:
    if not expires_raw or not signature:
        return False
    try:
        expires = int(expires_raw)
    except (TypeError, ValueError):
        return False
    if not (now < expires <= now + FILE_URL_TTL_S + _EXPIRY_SLACK_S):
        return False
    expected = file_signature(cfg.token, rec_id, expires)
    return hmac.compare_digest(expected.encode(), str(signature).encode())


def call(
    cfg: VisionSettings, method: str, path: str, *, json_body: "dict | None" = None, timeout=(5, 20)
) -> requests.Response:
    headers = {"Authorization": f"Bearer {cfg.token}", "X-Taklab-Vision-Client": cfg.client}
    try:
        return _session.request(
            method,
            cfg.url.rstrip("/") + path,
            headers=headers,
            json=json_body,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        # Only the exception class: the message may carry the URL.
        raise VisionUnavailable(type(exc).__name__) from None
