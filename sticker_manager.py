from collections import deque
import hashlib
import json
import logging
import random
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import DATA_DIR, STICKERS_DIR, settings

logger = logging.getLogger("dafeiyu.stickers")

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VALID_MOODS = (
    "happy",
    "smug",
    "hungry",
    "shocked",
    "angry",
    "sleepy",
    "gaming",
    "love",
)

GENERATED_DIR = DATA_DIR / "generated_stickers"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
WEB_MEME_DIR = STICKERS_DIR / "web_cache"
WEB_MEME_DIR.mkdir(parents=True, exist_ok=True)
STOLEN_DIR = STICKERS_DIR / "stolen"
STOLEN_DIR.mkdir(parents=True, exist_ok=True)

# 按情绪映射网络表情包搜索词。
# 原来的词表是「永雏塔菲 / 猫猫 / 熊猫头」——那是别人的角色，搜回来的图跟大肥鱼毫无关系，
# 群里看着就像发错人的图。现在只搜鱼 / 咸鱼 / 日常情绪梗。
WEB_MEME_QUERIES: dict[str, list[str]] = {
    "happy": ["大肥鱼 表情包", "咸鱼 搞笑 表情包", "开心 表情包", "干饭 表情包"],
    "smug": ["大肥鱼 表情包", "得意 表情包", "咸鱼 表情包", "鄙视 表情包"],
    "hungry": ["饿了 表情包", "干饭 表情包", "求投喂 表情包", "委屈 表情包"],
    "shocked": ["震惊 表情包", "吃瓜 表情包", "懵逼 表情包", "大肥鱼 表情包"],
    "angry": ["生气 表情包", "无语 表情包", "嫌弃 表情包", "炸毛 表情包"],
    "sleepy": ["摸鱼 表情包", "睡觉 表情包", "摆烂 表情包", "躺平 表情包"],
    "gaming": ["打游戏 表情包", "破防 表情包", "队友 问号 表情包", "大肥鱼 表情包"],
    "love": ["贴贴 表情包", "比心 表情包", "撒娇 表情包", "大肥鱼 表情包"],
}

# 本地配字。原来这些是「乐死塔菲了喵」「关注塔菲谢谢喵」——塔菲不是大肥鱼，
# 白纸黑字压在图上发出去，等于她自己在喊别人的名字。全部换成她自己的口吻。
DEFAULT_CAPTIONS: dict[str, list[str]] = {
    "happy": ["乐死我了", "今天也是条好鱼", "好耶！", "这波舒服"],
    "smug": ["就这？", "菜就多练", "我早说了吧", "小场面"],
    "hungry": ["饿扁了 给口饭", "V我50吃白饭", "谁能 /投喂 一下"],
    "shocked": ["什么逆天东西", "看戏中", "绷不住了"],
    "angry": ["气死我了", "差不多得了", "叉出去"],
    "sleepy": ["摸鱼中 勿扰", "先睡了", "已读乱回"],
    "gaming": ["？队友呢", "0-8 快乐风男", "点了吧下一把"],
    "love": ["贴贴", "谢谢老板", "大气"],
}

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
]


def _load_cn_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for fp in FONT_CANDIDATES:
        if Path(fp).is_file():
            try:
                return ImageFont.truetype(fp, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def render_caption_sticker(base_path: Path, caption: str) -> Path:
    """给本地底图压上梗图「白字黑粗描边」灵魂配字"""
    clean_cap = (caption or "").strip()[:12]
    if not clean_cap or base_path.suffix.lower() == ".gif":
        return base_path

    key = hashlib.md5(f"{base_path.resolve()}::{clean_cap}".encode("utf-8")).hexdigest()[:12]
    out_path = GENERATED_DIR / f"meme_{key}.jpg"
    if out_path.is_file():
        return out_path

    try:
        with Image.open(base_path) as im:
            im = im.convert("RGB")
            im.thumbnail((512, 512), Image.Resampling.LANCZOS)
            w, h = im.size
            bar_h = int(h * 0.22)
            canvas = Image.new("RGB", (w, h + bar_h), (255, 255, 255))
            canvas.paste(im, (0, 0))

            draw = ImageDraw.Draw(canvas)
            font_size = max(26, min(58, int(w / max(len(clean_cap), 4) * 0.92)))
            font = _load_cn_font(font_size)
            bbox = draw.textbbox((0, 0), clean_cap, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            while tw > w - 24 and font_size > 20:
                font_size -= 3
                font = _load_cn_font(font_size)
                bbox = draw.textbbox((0, 0), clean_cap, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

            x = (w - tw) // 2
            y = h + (bar_h - th) // 2 - bbox[1]
            draw.text(
                (x, y),
                clean_cap,
                font=font,
                fill=(255, 255, 255),
                stroke_width=max(3, font_size // 11),
                stroke_fill=(25, 35, 55),
            )
            canvas.save(out_path, format="JPEG", quality=90)
        return out_path
    except Exception:
        return base_path


def _fetch_online_meme(query: str, recent_urls: deque[str]) -> Path | None:
    """
    实时从网上拉取真实网友制作的爆款表情包（永雏塔菲 / 抽象 / LoL / 熊猫头），
    自动过滤长图/坏图并缓存到本地 stickers/web_cache/。
    """
    try:
        sn = random.choice([0, 6, 12, 18])
        api_url = (
            "https://image.so.com/j?q="
            + urllib.parse.quote(query)
            + f"&sn={sn}&pn=12"
        )
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
                "Referer": "https://image.so.com/",
            },
        )
        with urllib.request.urlopen(req, timeout=4.5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        items = data.get("list") or []
        random.shuffle(items)
        for item in items:
            img_url = str(item.get("img") or item.get("thumb") or "").strip()
            if not img_url.startswith(("http://", "https://")) or img_url in recent_urls:
                continue
            try:
                w = int(item.get("width") or 0)
                h = int(item.get("height") or 0)
                if w > 0 and h > 0:
                    ratio = w / h
                    if not (0.55 <= ratio <= 1.75) or w < 150 or h < 150:
                        continue
            except Exception:
                pass

            # 下载真实图片并校验有效性
            digest = hashlib.md5(img_url.encode("utf-8")).hexdigest()[:12]
            existing = list(WEB_MEME_DIR.glob(f"web_{digest}.*"))
            if existing and existing[0].is_file():
                recent_urls.append(img_url)
                return existing[0]

            dl_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
            }
            if "hdslb.com" in img_url:
                dl_headers["Referer"] = "https://www.bilibili.com/"
            dl_req = urllib.request.Request(img_url, headers=dl_headers)
            try:
                with urllib.request.urlopen(dl_req, timeout=4.5) as img_resp:
                    raw = img_resp.read()
                    if len(raw) < 4096 or len(raw) > 2 * 1024 * 1024:
                        continue
                    ctype = (img_resp.headers.get("Content-Type") or "").lower()
            except Exception:
                continue

            ext = ".jpg"
            if "gif" in ctype or img_url.lower().endswith(".gif"):
                ext = ".gif"
            elif "png" in ctype or img_url.lower().endswith(".png"):
                ext = ".png"
            elif "webp" in ctype or img_url.lower().endswith(".webp"):
                ext = ".webp"

            out_path = WEB_MEME_DIR / f"web_{digest}{ext}"
            out_path.write_bytes(raw)

            # 用 Pillow 验证是正常可读的图片（防止下到防盗链 HTML）
            try:
                with Image.open(out_path) as im:
                    im.verify()
            except Exception:
                out_path.unlink(missing_ok=True)
                continue

            # 控制缓存最多保留 120 张网络爆款表情包
            cached = sorted(WEB_MEME_DIR.glob("web_*.*"), key=lambda p: p.stat().st_mtime)
            if len(cached) > 120:
                for old in cached[:-120]:
                    old.unlink(missing_ok=True)

            recent_urls.append(img_url)
            logger.info("🎭 抓取网络真实表情包成功 [%s]: %s", query, out_path.name)
            return out_path
    except Exception as exc:
        logger.warning("抓取网络表情包失败 (%s): %s", query, exc)
    return None


class StickerManager:
    """
    大肥鱼表情包引擎（真实网络爆款表情包主力 + 偷群友图 + 本地定制图兜底）：
    - 80% 实时抓发真实网上的「永雏塔菲 / 抽象 / LoL / 熊猫头」爆款表情包
    - 20% 混搭偷来的群友梗图 & 本地定制配字大肥鱼
    - 12 张 LRU 防重，彻底告别单调
    """

    def __init__(self, root: Path = STICKERS_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        for mood in VALID_MOODS:
            (self.root / mood).mkdir(exist_ok=True)
        WEB_MEME_DIR.mkdir(exist_ok=True)
        STOLEN_DIR.mkdir(exist_ok=True)
        self._recent_bases: deque[str] = deque(maxlen=12)
        self._recent_urls: deque[str] = deque(maxlen=25)

    def _collect_for_mood(self, mood: str) -> list[Path]:
        mood = mood.lower().strip()
        candidates: list[Path] = []
        subdir = self.root / mood
        if subdir.is_dir():
            for p in subdir.iterdir():
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                    candidates.append(p)
        for ext in SUPPORTED_EXTS:
            direct = self.root / f"{mood}{ext}"
            if direct.is_file():
                candidates.append(direct)
        return candidates

    def _collect_all(self) -> list[Path]:
        all_paths: list[Path] = []
        for mood in VALID_MOODS:
            all_paths.extend(self._collect_for_mood(mood))
        return all_paths

    def _collect_stolen(self) -> list[Path]:
        if not STOLEN_DIR.is_dir():
            return []
        return [p for p in STOLEN_DIR.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]

    def maybe_steal_meme(self, image_path: Path | None) -> bool:
        """群友发的小图/梗图顺手偷进 stolen 库（最多 30 张）"""
        if not image_path or not image_path.is_file():
            return False
        try:
            if image_path.stat().st_size > 650 * 1024:
                return False
            with Image.open(image_path) as im:
                w, h = im.size
            ratio = w / max(h, 1)
            if not (0.65 <= ratio <= 1.55) or w < 120 or h < 120:
                return False
            if len(self._collect_stolen()) >= 30:
                return False
            digest = hashlib.md5(image_path.read_bytes()).hexdigest()[:10]
            dst = STOLEN_DIR / f"stolen_{digest}{image_path.suffix.lower() or '.jpg'}"
            if not dst.is_file():
                dst.write_bytes(image_path.read_bytes())
                logger.info("🐟 偷偷收藏了群友的表情包: %s", dst.name)
                return True
        except Exception:
            return False
        return False

    def pick_sticker(self, mood: str, caption: str | None = None, keyword: str = "") -> Path | None:
        """挑一张表情包。只有她自己点名要图时才会走到这里（见 bot_core._resolve_sticker）。

        keyword 是她写的 `[图:xxx]`：比情绪标签准得多，优先按它搜。
        """
        mood = (mood or "happy").lower().strip()
        if mood not in VALID_MOODS:
            mood = "happy"
        keyword = (keyword or "").strip()[:16]

        # 1. 她自己点了题的图：直接按关键词去搜，命中率最高
        if keyword and settings.STICKER_WEB_FETCH_ON:
            web_img = _fetch_online_meme(f"{keyword} 表情包", self._recent_urls)
            if web_img and web_img.is_file():
                self._recent_bases.append(str(web_img.resolve()))
                return web_img

        # 2. 按情绪抓网络梗图（每次抓一张，审核在发之前做）
        if settings.STICKER_WEB_FETCH_ON:
            query = random.choice(WEB_MEME_QUERIES.get(mood, WEB_MEME_QUERIES["happy"]))
            web_img = _fetch_online_meme(query, self._recent_urls)
            if web_img and web_img.is_file():
                self._recent_bases.append(str(web_img.resolve()))
                return web_img
            # 抓不到就从缓存里抽一张没用过的
            cached_web = [p for p in WEB_MEME_DIR.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]
            if cached_web:
                fresh_web = [p for p in cached_web if str(p.resolve()) not in self._recent_bases]
                chosen_web = random.choice(fresh_web or cached_web)
                self._recent_bases.append(str(chosen_web.resolve()))
                return chosen_web

        # 3. 反手发群友梗图（默认关：这些图来路不明，往往带着别人群的水印和梗）
        if settings.STICKER_STEAL_ON:
            stolen = self._collect_stolen()
            if stolen and random.random() < 0.35:
                fresh_stolen = [p for p in stolen if str(p.resolve()) not in self._recent_bases]
                chosen = random.choice(fresh_stolen or stolen)
                self._recent_bases.append(str(chosen.resolve()))
                return chosen

        # 4. 本地定制大肥鱼 + 配字兜底
        candidates = self._collect_for_mood(mood) or self._collect_all()
        if not candidates:
            return None
        fresh = [p for p in candidates if str(p.resolve()) not in self._recent_bases]
        base = random.choice(fresh or candidates)
        self._recent_bases.append(str(base.resolve()))
        final_caption = (caption or "").strip() or random.choice(
            DEFAULT_CAPTIONS.get(mood, DEFAULT_CAPTIONS["happy"])
        )
        return render_caption_sticker(base, final_caption)

    def to_web_url(self, sticker_path: Path | None) -> str | None:
        if not sticker_path:
            return None
        try:
            rel = sticker_path.resolve().relative_to(self.root.resolve())
            return f"/stickers/{rel.as_posix()}"
        except ValueError:
            pass
        try:
            rel = sticker_path.resolve().relative_to(GENERATED_DIR.resolve())
            return f"/generated_stickers/{rel.as_posix()}"
        except ValueError:
            return None

    def list_all(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for mood in VALID_MOODS:
            paths = self._collect_for_mood(mood)
            result[mood] = [self.to_web_url(p) for p in paths if self.to_web_url(p)]
        return result


sticker_manager = StickerManager()
