"""独立多源联网搜图与图片 URL 下载插件（支持国内直连 360 搜图 + 百度图片兜底）。"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import random
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

from PIL import Image

from config import DATA_DIR

logger = logging.getLogger("dafeiyu.image_fetcher")

DOWNLOADED_IMAGES_DIR = DATA_DIR / "downloaded_images"
DOWNLOADED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_RECENT_URLS: deque[str] = deque(maxlen=40)
_LOCK = threading.Lock()


def _pick_referer(url: str) -> str:
    low = url.lower()
    if "hdslb.com" in low or "bilibili" in low:
        return "https://www.bilibili.com/"
    if "baidu.com" in low or "bdimg.com" in low or "bcebos.com" in low:
        return "https://image.baidu.com/"
    if "so.com" in low or "qhimg.com" in low or "qhres" in low:
        return "https://image.so.com/"
    if "mihoyo.com" in low or "hoyolab.com" in low or "miyoushe.com" in low:
        return "https://www.miyoushe.com/"
    if "sinaimg.cn" in low or "weibo.com" in low:
        return "https://weibo.com/"
    if "pixiv" in low or "pximg.net" in low:
        return "https://www.pixiv.net/"
    return ""


def _prune_cache(max_files: int = 100) -> None:
    try:
        files = [p for p in DOWNLOADED_IMAGES_DIR.iterdir() if p.is_file() and not p.name.startswith(".")]
        if len(files) <= max_files:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for old in files[:-max_files]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def download_image_url(url: str, label: str = "image", min_side: int = 0) -> tuple[Path | None, str]:
    """下载指定 URL 图片，用 Pillow 验证有效并转为微信可直接展示卡片的格式。"""
    clean_url = (url or "").strip()
    if not clean_url.startswith(("http://", "https://")):
        return None, "图片链接必须以 http:// 或 https:// 开头"

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    referer = _pick_referer(clean_url)
    if referer:
        headers["Referer"] = referer

    try:
        req = urllib.request.Request(clean_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(12 * 1024 * 1024 + 1)
    except Exception as exc:
        return None, f"下载图片失败: {exc}"

    if len(raw) < 2048:
        return None, f"下载内容过小 ({len(raw)}B)，疑似无效图片或拦截页"
    if len(raw) > 12 * 1024 * 1024:
        return None, "图片体积超过 12MB 上限"
    if any(t in ctype for t in ("text/html", "application/json", "text/xml", "application/xml")):
        return None, f"返回内容不是图片 (Content-Type={ctype})"

    try:
        with Image.open(io.BytesIO(raw)) as verify_im:
            verify_im.verify()
        with Image.open(io.BytesIO(raw)) as im:
            width, height = im.size
            fmt = (im.format or "").upper()
            is_animated = bool(getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1)
    except Exception as exc:
        return None, f"图片格式损坏或非有效图像: {exc}"

    if min_side > 0 and (width < min_side or height < min_side):
        return None, f"图片分辨率过低 ({width}x{height})"

    digest = hashlib.md5(clean_url.encode("utf-8")).hexdigest()[:12]
    safe_prefix = "".join(c for c in label[:12] if c.isalnum() or "\u4e00" <= c <= "\u9fff") or "img"
    DOWNLOADED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    try:
        if is_animated and fmt == "GIF":
            out_path = DOWNLOADED_IMAGES_DIR / f"{safe_prefix}_{digest}.gif"
            tmp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, out_path)
        elif fmt in ("JPEG", "PNG") and not is_animated:
            ext = ".png" if fmt == "PNG" else ".jpg"
            out_path = DOWNLOADED_IMAGES_DIR / f"{safe_prefix}_{digest}{ext}"
            tmp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, out_path)
        else:
            # 非动图 WebP / BMP / 其他格式统一转存 JPG，确保 PC 微信作为图片消息发送
            out_path = DOWNLOADED_IMAGES_DIR / f"{safe_prefix}_{digest}.jpg"
            tmp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
            with Image.open(io.BytesIO(raw)) as im:
                if im.mode in ("RGBA", "LA", "P"):
                    rgba = im.convert("RGBA")
                    bg = Image.new("RGB", rgba.size, (255, 255, 255))
                    bg.paste(rgba, mask=rgba.split()[-1])
                    rgb = bg
                else:
                    rgb = im.convert("RGB")
                rgb.save(tmp, format="JPEG", quality=92)
            os.replace(tmp, out_path)
    except Exception as exc:
        return None, f"保存图片失败: {exc}"

    _prune_cache(100)
    logger.info("🖼️ 图片下载成功 [%s] %dx%d -> %s", label, width, height, out_path.name)
    return out_path, f"已下载图片 ({width}x{height}): {out_path.name}"


def _fetch_so_candidates(query: str) -> list[str]:
    sn = random.choice([0, 0, 12])
    api_url = f"https://image.so.com/j?q={urllib.parse.quote(query)}&sn={sn}&pn=18"
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": _USER_AGENT, "Referer": "https://image.so.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as exc:
        logger.warning("360搜图请求失败 (%s): %s", query, exc)
        return []

    hq: list[str] = []
    normal: list[str] = []
    for item in data.get("list") or []:
        img_url = str(item.get("img") or item.get("thumb") or "").strip()
        if not img_url.startswith(("http://", "https://")):
            continue
        try:
            w = int(item.get("width") or 0)
            h = int(item.get("height") or 0)
        except Exception:
            w, h = 0, 0
        if w >= 300 and h >= 300:
            hq.append(img_url)
        else:
            normal.append(img_url)

    random.shuffle(hq)
    random.shuffle(normal)
    return hq + normal


def _fetch_baidu_candidates(query: str) -> list[str]:
    api_url = (
        "https://image.baidu.com/search/acjson?tn=resultjson_com&ipn=rj"
        f"&word={urllib.parse.quote(query)}&pn=0&rn=18"
    )
    req = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": _USER_AGENT,
            "Referer": "https://image.baidu.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            raw_text = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw_text, strict=False)
    except Exception as exc:
        logger.warning("百度搜图请求失败 (%s): %s", query, exc)
        return []

    urls: list[str] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        for key in ("objURL", "hoverURL", "middleURL", "thumbURL"):
            cand = str(item.get(key) or "").strip()
            if cand.startswith(("http://", "https://")):
                urls.append(cand)
                break
    random.shuffle(urls)
    return urls


def search_and_download_image(query: str) -> tuple[Path | None, str]:
    """联网搜索并下载一张高质量匹配图片到本地缓存目录，返回 (Path, 描述)。"""
    clean_q = " ".join((query or "").split()).strip()
    if not clean_q:
        return None, "搜图关键词不能为空"

    with _LOCK:
        recent_snapshot = set(_RECENT_URLS)

    all_candidates: list[str] = []
    for fetcher in (_fetch_so_candidates, _fetch_baidu_candidates):
        batch = fetcher(clean_q)
        fresh = [u for u in batch if u not in recent_snapshot and u not in all_candidates]
        tried = fresh[:8]
        for url in tried:
            path, desc = download_image_url(url, label=clean_q, min_side=200)
            if path is not None:
                with _LOCK:
                    _RECENT_URLS.append(url)
                return path, f"已联网搜到「{clean_q}」图片（{desc}）"
        for u in batch:
            if u not in all_candidates:
                all_candidates.append(u)

    # 若新图均已耗尽或失败，放宽近期去重限制再兜底尝试
    for url in all_candidates[:6]:
        path, desc = download_image_url(url, label=clean_q, min_side=200)
        if path is not None:
            return path, f"已联网搜到「{clean_q}」图片（{desc}）"

    return None, f"暂时没找到可正常下载的「{clean_q}」高清图片（可能遇到图床防盗链或网络超时）"
