"""VLM精标数据工具的 handler。

通过 httpx 直连真实 VLM API（dmz-ess-vlm Docker 容器）。
VLM API 不按 user emailAddress 鉴权（与台架预约不同），
所以不需要身份注入。
"""
import json
import logging

import httpx

from config.settings import settings

log = logging.getLogger(__name__)

BASE = settings.VLM_API_BASE_URL
_PREFIX = "/vlm/agent"


def _ok(resp) -> str:
 if resp.is_success:
  return json.dumps(resp.json(), ensure_ascii=False)
 return json.dumps({"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}, ensure_ascii=False)


def list_event_names(args: dict, **_) -> str:
 r = httpx.get(f"{BASE}{_PREFIX}/eventNames", timeout=10)
 return _ok(r)


def list_camera_types(args: dict, **_) -> str:
 r = httpx.get(f"{BASE}{_PREFIX}/cameraTypes", timeout=10)
 return _ok(r)


def list_bags(args: dict, **_) -> str:
 params = {k: v for k, v in args.items() if v is not None}
 r = httpx.get(f"{BASE}{_PREFIX}/bags", params=params, timeout=15)
 return _ok(r)


def get_bag(args: dict, **_) -> str:
 bag_id = args.get("bagId")
 r = httpx.get(f"{BASE}{_PREFIX}/bags/{bag_id}", timeout=10)
 return _ok(r)


def list_frames(args: dict, **_) -> str:
 bag_id = args.get("bagId")
 params = {k: v for k, v in args.items() if k != "bagId" and v is not None}
 r = httpx.get(f"{BASE}{_PREFIX}/bags/{bag_id}/frames", params=params, timeout=15)
 return _ok(r)


def get_frame(args: dict, **_) -> str:
 frame_id = args.get("frameId")
 r = httpx.get(f"{BASE}{_PREFIX}/frames/{frame_id}", timeout=10)
 return _ok(r)


def playback_bag(args: dict, **_) -> str:
 bag_id = args.get("bagId")
 r = httpx.get(f"{BASE}{_PREFIX}/bags/{bag_id}/playback", timeout=30)
 return _ok(r)


def download_bag_metadata(args: dict, **_) -> str:
 "Return download URL metadata only - does not stream binary."
 bag_id = args.get("bagId")
 r = httpx.get(f"{BASE}{_PREFIX}/bags/{bag_id}/download", timeout=10)
 return _ok(r)


def frame_image_url(args: dict, **_) -> str:
 "Return image URL metadata only - does not stream binary."
 frame_id = args.get("frameId")
 r = httpx.get(f"{BASE}{_PREFIX}/frames/{frame_id}/image", timeout=10)
 return _ok(r)


def sync_execute(args: dict, **_) -> str:
 body = args if args else None
 r = httpx.post(f"{BASE}{_PREFIX}/sync/execute", json=body, timeout=60)
 return _ok(r)


def trigger_sync_async(args: dict, **_) -> str:
 r = httpx.post(f"{BASE}{_PREFIX}/sync/async", timeout=10)
 return _ok(r)


def sync_status(args: dict, **_) -> str:
 r = httpx.get(f"{BASE}{_PREFIX}/sync/status", timeout=10)
 return _ok(r)

