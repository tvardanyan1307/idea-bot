import asyncio
import logging
import os
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
import aiosqlite


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токен теперь читаем из переменной окружения BOT_TOKEN.
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. Установи переменную окружения BOT_TOKEN "
        "или добавь её в настройки хостинга."
    )


async def init_db() -> None:
    """
    Создаём таблицу идей и служебные таблицы для режимов комментария/редактирования.
    Обновляем старую базу при необходимости.
    """
    async with aiosqlite.connect("ideas.db") as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                text TEXT,
                created_at TEXT NOT NULL,
                tags TEXT,
                chat_id INTEGER,
                message_id INTEGER
            )
            """
        )

        # На всякий случай добавляем новые поля в старую базу.
        for column_def in ("tags TEXT", "chat_id INTEGER", "message_id INTEGER"):
            col_name = column_def.split()[0]
            try:
                await db.execute(f"ALTER TABLE ideas ADD COLUMN {column_def}")
            except aiosqlite.OperationalError:
                # Колонка уже существует.
                pass

        # Таблица ожидания комментария.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_comment (
                user_id INTEGER PRIMARY KEY,
                idea_id INTEGER NOT NULL
            )
            """
        )

        # Таблица ожидания редактирования.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_edit (
                user_id INTEGER PRIMARY KEY,
                idea_id INTEGER NOT NULL
            )
            """
        )

        await db.commit()


HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)


def extract_text_and_tags(raw: str) -> tuple[str, list[str]]:
    """
    Находит хэштеги вида #tag и возвращает:
    - чистый текст без хэштегов
    - список тегов без решётки
    """
    tags = HASHTAG_RE.findall(raw)
    text_without_tags = HASHTAG_RE.sub("", raw).strip()
    if not text_without_tags:
        text_without_tags = raw.strip()
    return text_without_tags, tags


async def add_comment_or_tags(message: Message, idea_id: int) -> None:
    """
    Добавляем к существующей идее дополнительный комментарий и/или теги.
    """
    raw_text = message.text or message.caption or ""
    if not raw_text.strip():
        await message.answer("Комментарий пустой, ничего не добавил.")
        return

    extra_text, extra_tags = extract_text_and_tags(raw_text)

    async with aiosqlite.connect("ideas.db") as db:
        cursor = await db.execute(
            "SELECT text, tags FROM ideas WHERE id = ? AND user_id = ?",
            (idea_id, message.from_user.id),
        )
        row = await cursor.fetchone()
        if not row:
            await message.answer(
                "Не нашёл идею, к которой можно добавить комментарий. "
                "Попробуй отправить новую идею."
            )
            return

        current_text, current_tags_str = row

        if extra_text:
            if current_text:
                new_text = current_text + "\n\nКомментарий: " + extra_text
            else:
                new_text = extra_text
        else:
            new_text = current_text

        current_tags = set()
        if current_tags_str:
            current_tags.update(t for t in current_tags_str.split(",") if t)
        current_tags.update(extra_tags)
        new_tags_str = ",".join(sorted(current_tags)) if current_tags else None

        await db.execute(
            "UPDATE ideas SET text = ?, tags = ? WHERE id = ?",
            (new_text, new_tags_str, idea_id),
        )
        await db.commit()

    await message.answer(f"Добавил комментарий/теги к идее #{idea_id} ✅")


async def edit_idea(message: Message, idea_id: int) -> None:
    """
    Полное редактирование описания/тегов идеи.
    Текст идеи перезаписывается, теги считаются из #хэштегов.
    """
    raw_text = message.text or message.caption or ""
    if not raw_text.strip():
        await message.answer("Пустое описание, ничего не изменил.")
        return

    text, tags = extract_text_and_tags(raw_text)
    tags_str = ",".join(tags) if tags else None

    async with aiosqlite.connect("ideas.db") as db:
        await db.execute(
            "UPDATE ideas SET text = ?, tags = ? WHERE id = ? AND user_id = ?",
            (text.strip(), tags_str, idea_id, message.from_user.id),
        )
        await db.commit()

    await message.answer(f"Обновил описание/теги для идеи #{idea_id} ✅")


async def save_idea(message: Message) -> None:
    """
    Любое входящее сообщение = новая идея.
    Поддерживаем теги в виде #tag прямо в тексте (если он есть).
    Сообщения без текста (просто медиа) тоже сохраняем.
    """
    raw_text = message.text or message.caption or ""

    if raw_text.strip():
        text, tags = extract_text_and_tags(raw_text)
        tags_str = ",".join(tags) if tags else None
    else:
        text = ""
        tags_str = None

    async with aiosqlite.connect("ideas.db") as db:
        cursor = await db.execute(
            """
            INSERT INTO ideas (user_id, username, text, created_at, tags, chat_id, message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.from_user.id,
                message.from_user.username,
                text.strip(),
                datetime.utcnow().isoformat(),
                tags_str,
                message.chat.id,
                message.message_id,
            ),
        )
        await db.commit()

        idea_id = cursor.lastrowid

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✍️ Коммент", callback_data=f"comment:{idea_id}"
                ),
                InlineKeyboardButton(
                    text="✏️ Редактировать", callback_data=f"edit:{idea_id}"
                ),
            ],
        ]
    )

    await message.answer(
        f"Сохранил как #{idea_id} ✅",
        reply_markup=keyboard,
    )


async def handle_list_ideas(message: Message) -> None:
    """
    Короткий список последних идей с возможностью раскрыть каждую.
    """
    async with aiosqlite.connect("ideas.db") as db:
        cursor = await db.execute(
            """
            SELECT id, text, created_at
            FROM ideas
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (message.from_user.id,),
        )
        rows = await cursor.fetchall()

    if not rows:
        await message.answer("У тебя пока нет сохранённых идей.")
        return

    lines: list[str] = []
    buttons: list[list[InlineKeyboardButton]] = []

    for idx, (idea_id, text, created_at) in enumerate(rows, start=1):
        main_part = text.split("\n\nКомментарий:", 1)[0].strip() if text else ""
        preview = main_part or text or "(без текста)"
        if len(preview) > 80:
            preview = preview[:77] + "..."

        line = f"{idx}. {preview}"
        lines.append(line)

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"Открыть {idx}",
                    callback_data=f"open_idea:{idea_id}",
                )
            ]
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        "Твои последние идеи (кратко):\n\n" + "\n".join(lines),
        reply_markup=keyboard,
    )


async def handle_open_idea(callback: CallbackQuery, bot: Bot) -> None:
    """
    Открываем полную идею по нажатию на кнопку.
    """
    data = callback.data or ""
    if not data.startswith("open_idea:"):
        return

    try:
        idea_id = int(data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Не получилось открыть идею.")
        return

    async with aiosqlite.connect("ideas.db") as db:
        cursor = await db.execute(
            """
            SELECT text, tags, chat_id, message_id, created_at
            FROM ideas
            WHERE id = ? AND user_id = ?
            """,
            (idea_id, callback.from_user.id),
        )
        row = await cursor.fetchone()

    if not row:
        await callback.answer("Идея не найдена.")
        return

    text, tags_str, chat_id, message_id, created_at = row

    if chat_id is not None and message_id is not None:
        try:
            await bot.copy_message(
                chat_id=callback.message.chat.id,
                from_chat_id=chat_id,
                message_id=message_id,
            )
        except Exception as e:
            logger.warning(
                "Не удалось скопировать сообщение для идеи %s: %r", idea_id, e
            )

    try:
        created_str = datetime.fromisoformat(created_at).strftime("%d.%m %H:%M")
    except Exception:
        created_str = created_at

    parts = [f"Идея #{idea_id} от {created_str}"]
    if text:
        parts.append(text)
    if tags_str:
        tags = [t for t in tags_str.split(",") if t]
        if tags:
            parts.append("Теги: " + " ".join(f"#{t}" for t in tags))

    await callback.message.answer("\n\n".join(parts))
    await callback.answer()


async def handle_comment_button(callback: CallbackQuery) -> None:
    """
    Включаем режим комментария к конкретной идее по кнопке.
    Следующее обычное сообщение пользователя станет комментарием к этой идее.
    Состояние храним в базе.
    """
    data = callback.data or ""
    if not data.startswith("comment:"):
        return

    try:
        idea_id = int(data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Не получилось включить комментарий.")
        return

    async with aiosqlite.connect("ideas.db") as db:
        cursor = await db.execute(
            "SELECT 1 FROM ideas WHERE id = ? AND user_id = ?",
            (idea_id, callback.from_user.id),
        )
        row = await cursor.fetchone()
        if not row:
            await callback.answer("Эта идея тебе не принадлежит.")
            return

        await db.execute(
            """
            INSERT INTO pending_comment (user_id, idea_id)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET idea_id=excluded.idea_id
            """,
            (callback.from_user.id, idea_id),
        )
        await db.commit()

    await callback.message.answer(
        f"Напиши одним сообщением комментарий/теги для идеи #{idea_id}.\n"
        "Можешь использовать текст и #теги.\n"
        "Команда /cancel отменит комментарий."
    )
    await callback.answer("Режим комментария включен")


async def handle_edit_button(callback: CallbackQuery) -> None:
    """
    Включаем режим редактирования описания/тегов идеи.
    Следующее обычное сообщение пользователя перезапишет текст и теги идеи.
    """
    data = callback.data or ""
    if not data.startswith("edit:"):
        return

    try:
        idea_id = int(data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Не получилось включить редактирование.")
        return

    async with aiosqlite.connect("ideas.db") as db:
        cursor = await db.execute(
            "SELECT 1 FROM ideas WHERE id = ? AND user_id = ?",
            (idea_id, callback.from_user.id),
        )
        row = await cursor.fetchone()
        if not row:
            await callback.answer("Эта идея тебе не принадлежит.")
            return

        await db.execute(
            """
            INSERT INTO pending_edit (user_id, idea_id)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET idea_id=excluded.idea_id
            """,
            (callback.from_user.id, idea_id),
        )
        await db.commit()

    await callback.message.answer(
        f"Пришли новое описание/теги для идеи #{idea_id}.\n"
        "Я заменю текущее описание на это сообщение и обновлю теги по #хэштегам.\n"
        "Команда /cancel отменит редактирование."
    )
    await callback.answer("Режим редактирования включен")


async def handle_start(message: Message) -> None:
    text = (
        "Привет! Я бот-банк идей.\n\n"
        "Просто пришли мне сообщение, пересылай пост или скриншот — я всё сохраню как отдельную идею.\n\n"
        "Описание и теги можно добавить:\n"
        "— сразу в тексте идеи через #tag, или\n"
        "— через кнопки ✍️ Коммент (добавить) или ✏️ Редактировать под сообщением \"Сохранил как #...\".\n\n"
        "Команда /list (или /ideas, /inbox) покажет последние идеи."
    )
    await message.answer(text)


async def handle_text_or_caption(message: Message) -> None:
    """
    Любое сообщение = новая идея,
    если только явно не включён режим комментария или редактирования.
    """
    if message.text and message.text.startswith("/"):
        return

    comment_idea_id = None
    edit_idea_id = None

    async with aiosqlite.connect("ideas.db") as db:
        cursor = await db.execute(
            "SELECT idea_id FROM pending_comment WHERE user_id = ?",
            (message.from_user.id,),
        )
        row = await cursor.fetchone()
        if row:
            comment_idea_id = row[0]
            await db.execute(
                "DELETE FROM pending_comment WHERE user_id = ?",
                (message.from_user.id,),
            )

        cursor = await db.execute(
            "SELECT idea_id FROM pending_edit WHERE user_id = ?",
            (message.from_user.id,),
        )
        row = await cursor.fetchone()
        if row:
            edit_idea_id = row[0]
            await db.execute(
                "DELETE FROM pending_edit WHERE user_id = ?",
                (message.from_user.id,),
            )

        await db.commit()

    if edit_idea_id is not None:
        await edit_idea(message, edit_idea_id)
    elif comment_idea_id is not None:
        await add_comment_or_tags(message, comment_idea_id)
    else:
        await save_idea(message)


async def main() -> None:
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="О боте"),
            BotCommand(command="ideas", description="Мои идеи"),
            BotCommand(command="list", description="Мои идеи (краткий список)"),
            BotCommand(command="inbox", description="Последние идеи"),
        ]
    )

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_list_ideas, Command(commands=["ideas", "list", "inbox"]))
    dp.callback_query.register(handle_open_idea, F.data.startswith("open_idea:"))
    dp.callback_query.register(handle_comment_button, F.data.startswith("comment:"))
    dp.callback_query.register(handle_edit_button, F.data.startswith("edit:"))
    dp.message.register(handle_text_or_caption)

    logger.info("Бот запущен. Нажми Ctrl+C, чтобы остановить.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
