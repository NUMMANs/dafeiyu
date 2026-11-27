import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from adapters.qq_onebot_adapter import handle_onebot_ws
from bot_core import bot_core
from config import BASE_DIR, STICKERS_DIR, UPLOADS_DIR, settings
from pet_engine import pet_engine
from sticker_manager import GENERATED_DIR, sticker_manager

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="大肥鱼 · Grok 微信群锐评核查控制台")

app.mount("/stickers", StaticFiles(directory=str(STICKERS_DIR)), name="stickers")
app.mount("/generated_stickers", StaticFiles(directory=str(GENERATED_DIR)), name="generated_stickers")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.websocket("/onebot/ws")
async def onebot_ws_endpoint(websocket: WebSocket):
    await handle_onebot_ws(websocket)


@app.get("/api/status")
def api_status(group_id: str = "大肥鱼模拟群"):
    st = pet_engine.get_status()
    return {
        "pet": st.to_dict(),
        "leaderboard": [],
        "stickers": sticker_manager.list_all(),
        "config": {
            "bot_name": settings.BOT_NAME,
            "has_api_key": settings.has_valid_api_key,
            "chat_model": settings.DEEPSEEK_CHAT_MODEL,
            "vision_model": settings.DEEPSEEK_VISION_MODEL,
            "run_mode": settings.RUN_MODE,
            "wx_groups": settings.WX_GROUP_WHITELIST,
            "group_at_only": settings.GROUP_AT_ONLY,
        },
    }


@app.get("/api/history")
def api_history(group_id: str = "大肥鱼模拟群", limit: int = 30):
    items = pet_engine.get_recent_history(group_id=group_id, limit=limit)
    for it in items:
        if it.get("image_path"):
            p = Path(it["image_path"])
            try:
                rel = p.resolve().relative_to(UPLOADS_DIR.resolve())
                it["image_url"] = f"/uploads/{rel.as_posix()}"
            except Exception:
                it["image_url"] = None
        else:
            it["image_url"] = None
    return {"messages": items}


@app.post("/api/chat")
async def api_chat(
    group_id: str = Form("大肥鱼模拟群"),
    sender: str = Form("群主"),
    text: str = Form(""),
    force_trigger: bool = Form(True),
    image: UploadFile | None = File(None),
):
    saved_path: Path | None = None
    uploaded_url: str | None = None

    if image is not None and image.filename:
        ext = Path(image.filename).suffix.lower() or ".png"
        if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            ext = ".png"
        filename = f"{uuid.uuid4().hex}{ext}"
        saved_path = UPLOADS_DIR / filename
        data = await image.read()
        saved_path.write_bytes(data)
        uploaded_url = f"/uploads/{filename}"

    resp = bot_core.handle_message(
        group_id=group_id,
        sender=sender.strip() or "群友",
        text=text,
        image_path=saved_path,
        force_trigger=force_trigger,
    )

    st = resp.status or pet_engine.get_status()
    return {
        "replied": resp.should_reply,
        "reply_text": resp.text,
        "sticker_url": resp.sticker_url,
        "mood": resp.mood,
        "uploaded_image_url": uploaded_url,
        "pet": st.to_dict(),
        "leaderboard": [],
    }
