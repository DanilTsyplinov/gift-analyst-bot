#!/usr/bin/env python3
"""
Telegram Gift Analyst Bot — минимальный работающий прототип.

Что умеет:
- /start, /help — базовая справка.
- /catalog — показывает доступные подарки и их цену в Stars (по Bot API).
- /connect_info — подсказка, как подключить бота к вашему Business‑аккаунту.
- Обработчик business_connection — сохраняет business_connection_id, когда вы подключаете бота через Telegram > Settings > Business > Connect a Bot.
- /portfolio — загружает список ваших подарков, если вы подключили Business‑аккаунт (getBusinessAccountGifts), строит краткую сводку.
- /analyze — примитивная «инвестиционная» логика: что конвертировать в Stars, что стоит попытаться апгрейдить до уникального (если возможно), что можно уже перевести новому владельцу (unique с доступной датой transfer).
- /watch <минПрофит> <мин%> — включает алерты по изменениям портфеля и каталога (по таймеру). Пока без рыночных «флор‑цен» — только эвристики на основе каталога/конвертации.

Важно:
- Бот видит ПОРТФЕЛЬ только у подключённого Business‑аккаунта владельца. Обычный личный аккаунт без Business‑подключения недоступен для бота.
- «Рыночные» цены перепродажи Telegram Gifts Bot API напрямую не отдаёт. Здесь предусмотрены точки расширения (market_providers) — можно подключить сторонний источник котировок, если он у вас есть.

Запуск:
  export BOT_TOKEN=8478392746:AAHvJWJIytZvZYSsVFNh_a5flPsA-tItmC8
  python tg_gift_analyst_bot.py

Зависимости:
  pip install "python-telegram-bot>=22.1,<23" pydantic

Автор: вы + ChatGPT. Лицензия: MIT.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    BusinessConnectionHandler,
)

STATE_PATH = os.environ.get("STATE_PATH", "./state.json")
DEFAULT_POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "1800"))  # 30 мин

# --------------------------
# Простая файловая «база данных»
# --------------------------
class State(BaseModel):
    # key: telegram user id (int)
    connections: Dict[str, str] = {}  # user_id -> business_connection_id
    chats: Dict[str, int] = {}        # user_id -> last chat_id для личных сообщений
    settings: Dict[str, Dict[str, Any]] = {}  # user_id -> {"min_profit_stars": int, "min_profit_pct": float}
    last_catalog: Dict[str, Any] = {}         # кеш каталога gifts
    last_portfolio: Dict[str, Any] = {}       # user_id -> snapshot

    @classmethod
    def load(cls) -> "State":
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(**data)
        return cls()

    def save(self) -> None:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))
        os.replace(tmp, STATE_PATH)

STATE = State.load()

# --------------------------
# Утилиты форматирования
# --------------------------

def fmt_stars(val: Optional[int]) -> str:
    return f"{val} ⭐" if isinstance(val, int) else "—"


def human_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


# --------------------------
# Плагины/заглушки цен с маркетплейса (для будущего)
# --------------------------
class MarketQuote(BaseModel):
    gift_id: str
    floor_stars: Optional[int] = None
    last_trade_stars: Optional[int] = None
    ts: float = time.time()


async def fetch_market_quotes_example(gift_ids: List[str]) -> Dict[str, MarketQuote]:
    """Заглушка: вернёт пустые котировки. Подключите сюда ваш источник данных."""
    return {gid: MarketQuote(gift_id=gid) for gid in gift_ids}


# --------------------------
# Бизнес‑логика анализа портфеля
# --------------------------
@dataclass
class Suggestion:
    title: str
    details: str


async def build_catalog(context: ContextTypes.DEFAULT_TYPE) -> List[Dict[str, Any]]:
    """Получить доступные подарки (getAvailableGifts)."""
    bot = context.bot
    gifts_obj = await bot.get_available_gifts()
    catalog: List[Dict[str, Any]] = []
    for g in gifts_obj.gifts:
        # Gift: id, sticker, star_count, total_count?, remaining_count?, upgrade_star_count?
        catalog.append(
            {
                "id": g.id,
                "title": getattr(getattr(g, "sticker", None), "emoji", None) or "Gift",
                "star_count": getattr(g, "star_count", None),
                "total_count": getattr(g, "total_count", None),
                "remaining_count": getattr(g, "remaining_count", None),
                "upgrade_star_count": getattr(g, "upgrade_star_count", None),
            }
        )
    STATE.last_catalog = {g["id"]: g for g in catalog}
    STATE.save()
    return catalog


async def fetch_portfolio(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Dict[str, Any]:
    """Загрузить все подарки бизнес‑аккаунта пользователя (getBusinessAccountGifts + пагинация)."""
    bot = context.bot
    bc = STATE.connections.get(str(user_id))
    if not bc:
        return {"gifts": [], "total_count": 0}

    all_gifts = []
    offset = None
    total = 0
    while True:
        resp = await bot.get_business_account_gifts(business_connection_id=bc, offset=offset, limit=50)
        total = resp.total_count
        for og in resp.gifts:
            item: Dict[str, Any] = {"type": og.type}
            # REGULAR
            if og.type == "regular":
                item.update(
                    {
                        "class": "regular",
                        "gift_id": getattr(og.gift, "id", None),
                        "gift_title": getattr(getattr(og.gift, "sticker", None), "emoji", None) or "Gift",
                        "convert_star_count": getattr(og, "convert_star_count", None),
                        "can_be_upgraded": getattr(og, "can_be_upgraded", None),
                        "prepaid_upgrade_star_count": getattr(og, "prepaid_upgrade_star_count", None),
                        "text": getattr(og, "text", None),
                    }
                )
            else:  # UNIQUE
                ug = og.gift  # UniqueGift
                item.update(
                    {
                        "class": "unique",
                        "gift_id": getattr(ug, "id", None),
                        "gift_title": getattr(getattr(ug, "sticker", None), "emoji", None) or "Unique",
                        "rank": getattr(ug, "rank", None),
                        "is_sticker": True,
                        "can_be_transferred": getattr(og, "can_be_transferred", None),
                        "transfer_star_count": getattr(og, "transfer_star_count", None),
                        "next_transfer_date": getattr(og, "next_transfer_date", None).isoformat() if getattr(og, "next_transfer_date", None) else None,
                    }
                )
            all_gifts.append(item)
        if not resp.next_offset:
            break
        offset = resp.next_offset

    portfolio = {"total_count": total, "gifts": all_gifts, "ts": time.time()}
    STATE.last_portfolio[str(user_id)] = portfolio
    STATE.save()
    return portfolio


async def analyze_portfolio(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Tuple[List[Suggestion], Dict[str, Any]]:
    """Сформировать список рекомендаций на основе каталога/портфеля/котировок."""
    catalog = STATE.last_catalog or {c["id"]: c for c in await build_catalog(context)}
    portfolio = STATE.last_portfolio.get(str(user_id)) or await fetch_portfolio(context, user_id)

    # Подтянуть котировки (заглушка)
    unique_ids = [g["gift_id"] for g in portfolio["gifts"] if g.get("class") == "unique" and g.get("gift_id")]
    quotes = await fetch_market_quotes_example(unique_ids)

    suggestions: List[Suggestion] = []

    # 1) REGULAR: конвертировать в Stars, если доступно и > минимального порога
    settings = STATE.settings.get(str(user_id), {"min_profit_stars": 0, "min_profit_pct": 0.0})
    min_profit_stars = int(settings.get("min_profit_stars", 0))

    regulars = [g for g in portfolio["gifts"] if g.get("class") == "regular"]
    for r in regulars:
        conv = r.get("convert_star_count")
        if isinstance(conv, int) and conv >= min_profit_stars:
            suggestions.append(
                Suggestion(
                    title=f"Конвертировать в Stars: {r['gift_title']}",
                    details=f"Можно получить {fmt_stars(conv)} за обычный подарок."
                )
            )
        elif r.get("can_be_upgraded"):
            # Оценка апгрейда: если у соответствующего Gift есть upgrade_star_count
            g = catalog.get(r.get("gift_id"))
            up = g.get("upgrade_star_count") if g else None
            if isinstance(up, int):
                suggestions.append(
                    Suggestion(
                        title=f"Подумать об апгрейде: {r['gift_title']}",
                        details=f"Апгрейд до уникального стоит {fmt_stars(up)}. Оцените рынок перед апгрейдом."
                    )
                )

    # 2) UNIQUE: можно ли уже перевести (продать/подарить)
    for u in [g for g in portfolio["gifts"] if g.get("class") == "unique"]:
        next_transfer_iso = u.get("next_transfer_date")
        can_transfer = True
        if next_transfer_iso:
            try:
                can_transfer = datetime.fromisoformat(next_transfer_iso).timestamp() <= time.time()
            except Exception:
                can_transfer = True
        if can_transfer:
            q = quotes.get(u["gift_id"]) if u.get("gift_id") else None
            floor = q.floor_stars if q else None
            details = "Можно переводить сейчас."
            if floor:
                details += f" Ориентир по флоору: ~{fmt_stars(floor)}."
            suggestions.append(
                Suggestion(
                    title=f"Уникальный готов к продаже: {u['gift_title']}",
                    details=details,
                )
            )

    if not suggestions:
        suggestions.append(Suggestion(title="Пока без явных действий", details="Ждём изменений в каталоге/портфеле."))

    return suggestions, {"portfolio": portfolio, "catalog": list(catalog.values())}


# --------------------------
# Хендлеры бота
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        STATE.chats[str(user.id)] = update.effective_chat.id
        STATE.save()
    text = (
        "Привет! Я бот‑аналитик подарков в Telegram.\n\n"
        "Что я умею:\n"
        "• /catalog — показать доступные подарки и цены в Stars\n"
        "• /connect_info — как подключить меня к Business‑аккаунту\n"
        "• /portfolio — показать ваши подарки (только для Business)\n"
        "• /analyze — рекомендации (конвертация/апгрейд/трансфер)\n"
        "• /watch <минStars> <мин%> — алерты по изменениям (напр. /watch 100 5)\n"
        "• /help — справка\n"
    )
    await update.effective_message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def connect_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (
        "Чтобы я видел ваши подарки, подключите меня как бизнес‑бота к вашему аккаунту:\n\n"
        "1) Telegram > Settings > Business > Connect a Bot.\n"
        "2) Выберите этого бота и дайте доступ к Gifts/Stars.\n"
        "3) Я получу business_connection и смогу читать список подарков.\n\n"
        "После подключения отправьте /portfolio."
    )
    await update.effective_message.reply_text(txt)


async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Update.business_connection
    bc = update.business_connection
    user = update.effective_user
    if not bc or not user:
        return
    STATE.connections[str(user.id)] = bc.id
    STATE.chats[str(user.id)] = update.effective_chat.id if update.effective_chat else STATE.chats.get(str(user.id))
    STATE.save()
    await context.bot.send_message(chat_id=STATE.chats[str(user.id)], text="Бизнес‑подключение сохранено ✅. Теперь доступна команда /portfolio.")


async def catalog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    catalog = await build_catalog(context)
    # Отобразим первые 10 позиций
    head = "Каталог подарков (фрагмент):\n"
    lines = []
    for g in catalog[:10]:
        lim = "∞" if g.get("total_count") is None else str(g.get("remaining_count"))
        lines.append(f"{g['title']} — {fmt_stars(g['star_count'])} (остаток: {lim})")
    await update.effective_message.reply_text(head + "\n".join(lines))


async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    pf = await fetch_portfolio(context, user.id)
    total = pf.get("total_count", 0)
    regs = sum(1 for g in pf["gifts"] if g.get("class") == "regular")
    unqs = sum(1 for g in pf["gifts"] if g.get("class") == "unique")
    await update.effective_message.reply_text(
        f"У вас {total} подарков: {regs} обычных и {unqs} уникальных.\n" "Используйте /analyze для рекомендаций."
    )


async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    suggestions, _ = await analyze_portfolio(context, user.id)
    txt = "\n\n".join([f"• <b>{s.title}</b>\n{s.details}" for s in suggestions])
    await update.effective_message.reply_text(txt, parse_mode=ParseMode.HTML)


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    args = context.args
    min_stars = int(args[0]) if len(args) >= 1 and args[0].isdigit() else 0
    min_pct = float(args[1]) if len(args) >= 2 else 0.0

    STATE.settings[str(user.id)] = {"min_profit_stars": min_stars, "min_profit_pct": min_pct}
    STATE.save()

    # Запустим периодическую задачу
    job_name = f"watch_{user.id}"
    # Сначала отменим старую, если была
    jobs = context.job_queue.get_jobs_by_name(job_name)
    for j in jobs:
        j.schedule_removal()

    context.job_queue.run_repeating(callback=watch_tick, interval=DEFAULT_POLL_INTERVAL_SEC, name=job_name, data={"user_id": user.id})

    await update.effective_message.reply_text(
        f"Алерты включены: мин профит {min_stars}⭐, мин {min_pct}%. Буду проверять раз в {DEFAULT_POLL_INTERVAL_SEC//60} мин."
    )


async def watch_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data.get("user_id")
    try:
        suggestions, _ = await analyze_portfolio(context, user_id)
        if suggestions:
            chat_id = STATE.chats.get(str(user_id))
            if chat_id:
                txt = "\n\n".join([f"• <b>{s.title}</b>\n{s.details}" for s in suggestions])
                await context.bot.send_message(chat_id=chat_id, text=f"Обновления по портфелю:\n\n{txt}", parse_mode=ParseMode.HTML)
    except Exception as e:
        # Мягкое логирование в консоль
        print("[watch_tick] error:", e)


def require_token() -> str:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не найден BOT_TOKEN в переменных окружения")
    return token


async def main() -> None:
    app: Application = (
        ApplicationBuilder().token(require_token()).build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("connect_info", connect_info))
    app.add_handler(BusinessConnectionHandler(on_business_connection))
    app.add_handler(CommandHandler("catalog", catalog_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))

    # Пуллинг
    print("Gift Analyst Bot запущен. Нажмите Ctrl+C для остановки.")
    await app.initialize()
    await app.start()
    try:
        await app.updater.start_polling(allowed_updates=["message", "business_connection"])  # type: ignore[attr-defined]
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()  # type: ignore[attr-defined]
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Остановка…")
