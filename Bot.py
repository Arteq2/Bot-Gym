import asyncio
import logging
import aiosqlite
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

import os
TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ================= FSM =================
class CreateWorkout(StatesGroup):
    waiting_for_name = State()
    waiting_for_exercises = State()

class EditWorkout(StatesGroup):
    waiting_for_new_name = State()
    waiting_for_new_exercises = State()

class LogSession(StatesGroup):
    choosing_exercise = State()
    waiting_for_sets = State()

MAIN_MENU_BUTTONS = {
    "🏋️ Начать тренировку",
    "📝 Создать тренировку",
    "⚙️ Управление тренировками",
    "📊 Прогресс и результаты",
}

# ================= DB =================
async def init_db():
    async with aiosqlite.connect('gym_bot.db') as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS workouts
                            (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT)''')
        # version_id — к какой версии тренировки относится упражнение.
        # Когда юзер редактирует список упражнений, старые exercise-записи
        # НЕ удаляются — просто создаются новые с новым version_id.
        # Это позволяет старым сессиям по-прежнему отображать правильные названия.
        await db.execute('''CREATE TABLE IF NOT EXISTS exercises
                            (id INTEGER PRIMARY KEY,
                             workout_id INTEGER,
                             version_id INTEGER DEFAULT 1,
                             name TEXT)''')
        # current_version — текущая версия упражнений тренировки
        await db.execute('''CREATE TABLE IF NOT EXISTS workout_versions
                            (workout_id INTEGER PRIMARY KEY,
                             current_version INTEGER DEFAULT 1)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS sessions
                            (id INTEGER PRIMARY KEY,
                             workout_id INTEGER,
                             user_id INTEGER,
                             exercise_version INTEGER DEFAULT 1,
                             date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS sets
                            (id INTEGER PRIMARY KEY,
                             session_id INTEGER,
                             exercise_id INTEGER,
                             reps INTEGER,
                             weight REAL)''')
        await db.commit()

# ================= HELPERS =================
def fmt_weight(w: float) -> str:
    return str(int(w)) if float(w).is_integer() else str(w)

def parse_date(d) -> datetime:
    try:
        return datetime.strptime(str(d)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now()

def days_label(days: int) -> str:
    if days == 0:
        return "сегодня"
    elif days == 1:
        return "вчера"
    else:
        return f"{days} дн. назад"

async def get_current_version(db, workout_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT current_version FROM workout_versions WHERE workout_id=?", (workout_id,)
    )
    return rows[0][0] if rows else 1

async def get_session_summary(db, session_id: int) -> str:
    """Подходы сессии — использует упражнения той версии, что была при сессии."""
    async with db.execute('''
        SELECT e.name, s.weight, s.reps
        FROM sets s
        JOIN exercises e ON s.exercise_id = e.id
        WHERE s.session_id = ?
        ORDER BY s.id ASC
    ''', (session_id,)) as c:
        rows = await c.fetchall()

    if not rows:
        return "_(Нет записанных подходов)_"

    ex_data = {}
    for ex_name, weight, reps in rows:
        ex_data.setdefault(ex_name, []).append(f"{fmt_weight(weight)}кг×{reps}")

    return "\n".join(f"🔹 *{name}*: " + ", ".join(sets) for name, sets in ex_data.items())

async def get_exercise_progress(db, workout_id: int, user_id: int) -> str:
    """
    Для каждого упражнения текущей версии тренировки показывает:
    лучший подход за последнюю сессию и предпоследнюю, и разницу.
    """
    version = await get_current_version(db, workout_id)

    # Упражнения текущей версии
    exercises = await db.execute_fetchall(
        "SELECT id, name FROM exercises WHERE workout_id=? AND version_id=? ORDER BY id",
        (workout_id, version)
    )
    if not exercises:
        return "_Нет упражнений_"

    # Последние 2 сессии с подходами
    sessions = await db.execute_fetchall('''
        SELECT s.id, s.date FROM sessions s
        WHERE s.workout_id=? AND s.user_id=?
          AND EXISTS (SELECT 1 FROM sets WHERE session_id=s.id)
        ORDER BY s.date DESC LIMIT 2
    ''', (workout_id, user_id))

    if not sessions:
        return "_Нет записанных тренировок_"

    last_sid = sessions[0][0]
    prev_sid = sessions[1][0] if len(sessions) > 1 else None

    lines = []
    for ex_id, ex_name in exercises:
        # Лучший подход = максимальный вес, при равном весе — максимальные повторы
        async with db.execute('''
            SELECT weight, reps FROM sets
            WHERE session_id=? AND exercise_id=?
            ORDER BY weight DESC, reps DESC LIMIT 1
        ''', (last_sid, ex_id)) as c:
            last_best = await c.fetchone()

        if not last_best:
            lines.append(f"🔹 *{ex_name}*: _не выполнялось_")
            continue

        lw, lr = last_best
        line = f"🔹 *{ex_name}*: {fmt_weight(lw)}кг×{lr}"

        if prev_sid:
            async with db.execute('''
                SELECT weight, reps FROM sets
                WHERE session_id=? AND exercise_id=?
                ORDER BY weight DESC, reps DESC LIMIT 1
            ''', (prev_sid, ex_id)) as c:
                prev_best = await c.fetchone()

            if prev_best:
                pw, pr = prev_best
                dw = lw - pw
                dr = lr - pr
                parts = []
                if dw > 0:
                    parts.append(f"⬆️+{fmt_weight(dw)}кг")
                elif dw < 0:
                    parts.append(f"⬇️{fmt_weight(dw)}кг")
                if dr > 0:
                    parts.append(f"⬆️+{dr} пов.")
                elif dr < 0:
                    parts.append(f"⬇️{dr} пов.")
                if parts:
                    line += "  " + " ".join(parts)
                else:
                    line += "  _(без изменений)_"
            else:
                line += "  _(новое упражнение)_"
        lines.append(line)

    last_date = parse_date(sessions[0][1]).strftime('%d.%m.%Y')
    header = f"_Последняя тренировка: {last_date}_"
    if prev_sid:
        prev_date = parse_date(sessions[1][1]).strftime('%d.%m.%Y')
        header += f" _vs {prev_date}_"

    return header + "\n\n" + "\n".join(lines)

# ================= KEYBOARDS =================
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏋️ Начать тренировку")],
        [KeyboardButton(text="📝 Создать тренировку"), KeyboardButton(text="⚙️ Управление тренировками")],
        [KeyboardButton(text="📊 Прогресс и результаты")]
    ],
    resize_keyboard=True
)

in_session_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⬅️ К списку упражнений")],
        [KeyboardButton(text="🛑 Завершить тренировку")]
    ],
    resize_keyboard=True
)

async def send_exercise_list(target: Message, state: FSMContext):
    data = await state.get_data()
    exercises = data.get("exercises", [])
    set_counts = data.get("set_counts", {})

    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{e['name']} {'✅' if str(e['id']) in set_counts else ''}",
            callback_data=f"ex_{e['id']}"
        )]
        for e in exercises
    ])
    await target.answer(
        "Выбери упражнение:\n_(✅ — есть записанные подходы)_",
        reply_markup=in_session_kb,
        parse_mode="Markdown"
    )
    await target.answer("👇", reply_markup=inline_kb)
    await state.set_state(LogSession.choosing_exercise)

# =================================================================================
# ПЕРЕХВАТЧИКИ КНОПОК ГЛАВНОГО МЕНЮ ВО ВРЕМЯ FSM
# =================================================================================

@dp.message(
    StateFilter(CreateWorkout.waiting_for_name, CreateWorkout.waiting_for_exercises,
                EditWorkout.waiting_for_new_name, EditWorkout.waiting_for_new_exercises),
    F.text.in_(MAIN_MENU_BUTTONS)
)
async def interrupt_edit_or_create(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("⚠️ Действие отменено.", reply_markup=main_kb)
    await route_main_menu(message, state)

@dp.message(
    StateFilter(LogSession.waiting_for_sets, LogSession.choosing_exercise),
    F.text.in_(MAIN_MENU_BUTTONS)
)
async def interrupt_log_session(message: Message, state: FSMContext):
    await state.update_data(pending_action=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛑 Завершить и перейти", callback_data="confirm_interrupt"),
        InlineKeyboardButton(text="↩️ Продолжить", callback_data="cancel_interrupt")
    ]])
    await message.answer(
        "⚠️ *Тренировка ещё идёт!*\n\nЗавершить и перейти в меню?",
        reply_markup=kb, parse_mode="Markdown"
    )

@dp.callback_query(F.data == "confirm_interrupt")
async def confirm_interrupt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    session_id = data.get("session_id")
    workout_name = data.get("workout_name", "Тренировка")
    pending_action = data.get("pending_action")
    await state.clear()

    if session_id:
        async with aiosqlite.connect('gym_bot.db') as db:
            summary = await get_session_summary(db, session_id)
        await callback.message.answer(
            f"🏁 *{workout_name} завершена!*\n\n{summary}",
            reply_markup=main_kb, parse_mode="Markdown"
        )
    else:
        await callback.message.answer("Тренировка завершена.", reply_markup=main_kb)

    if pending_action:
        await route_main_menu(callback.message, state, text_override=pending_action)

@dp.callback_query(F.data == "cancel_interrupt")
async def cancel_interrupt(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Продолжаем 💪\nВведи подход или выбери упражнение ⬅️",
        reply_markup=in_session_kb
    )

async def route_main_menu(message: Message, state: FSMContext, text_override: str = None):
    text = text_override or message.text
    if text == "🏋️ Начать тренировку":
        await start_training(message, state)
    elif text == "📝 Создать тренировку":
        await create_workout(message, state)
    elif text == "⚙️ Управление тренировками":
        await manage_workouts(message, state)
    elif text == "📊 Прогресс и результаты":
        await progress_choose_workout(message)

# ================= /start =================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 Привет! Я твой личный трекер тренировок.\n\nВыбери действие:", reply_markup=main_kb)

# ================= СОЗДАТЬ ТРЕНИРОВКУ =================
@dp.message(F.text == "📝 Создать тренировку")
async def create_workout(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Введи название тренировки:\n_(например: День груди, Ноги, Спина)_", parse_mode="Markdown")
    await state.set_state(CreateWorkout.waiting_for_name)

@dp.message(CreateWorkout.waiting_for_name)
async def save_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer(
        "Введи упражнения *через запятую*:\n_(например: Жим лёжа, Жим гантелей, Разводка)_",
        parse_mode="Markdown"
    )
    await state.set_state(CreateWorkout.waiting_for_exercises)

@dp.message(CreateWorkout.waiting_for_exercises)
async def save_exercises(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["name"]
    exercises = [e.strip() for e in message.text.split(",") if e.strip()]
    if not exercises:
        await message.answer("❌ Список пустой. Введи упражнения через запятую:")
        return

    async with aiosqlite.connect('gym_bot.db') as db:
        cur = await db.execute("INSERT INTO workouts (user_id, name) VALUES (?, ?)", (message.from_user.id, name))
        wid = cur.lastrowid
        for ex in exercises:
            await db.execute("INSERT INTO exercises (workout_id, version_id, name) VALUES (?, 1, ?)", (wid, ex))
        await db.execute("INSERT INTO workout_versions (workout_id, current_version) VALUES (?, 1)", (wid,))
        await db.commit()

    await state.clear()
    ex_list = "\n".join(f"  • {e}" for e in exercises)
    await message.answer(f"✅ Тренировка *{name}* создана!\n\n{ex_list}", reply_markup=main_kb, parse_mode="Markdown")

# ================= УПРАВЛЕНИЕ ТРЕНИРОВКАМИ =================
@dp.message(F.text == "⚙️ Управление тренировками")
async def manage_workouts(message: Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect('gym_bot.db') as db:
        rows = await db.execute_fetchall(
            "SELECT id, name FROM workouts WHERE user_id=? ORDER BY id DESC",
            (message.from_user.id,)
        )
    if not rows:
        await message.answer("У тебя нет тренировок. Сначала создай одну 📝")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r[1], callback_data=f"manage_{r[0]}")]
        for r in rows
    ])
    await message.answer("Выбери тренировку:", reply_markup=kb)

@dp.callback_query(F.data.startswith("manage_"))
async def manage_one(callback: CallbackQuery):
    await callback.answer()
    wid = int(callback.data.split("_")[1])

    async with aiosqlite.connect('gym_bot.db') as db:
        row = await db.execute_fetchall(
            "SELECT name FROM workouts WHERE id=? AND user_id=?", (wid, callback.from_user.id)
        )
        if not row:
            await callback.message.answer("Тренировка не найдена.")
            return
        name = row[0][0]
        version = await get_current_version(db, wid)
        exercises = await db.execute_fetchall(
            "SELECT name FROM exercises WHERE workout_id=? AND version_id=? ORDER BY id",
            (wid, version)
        )

    ex_list = "\n".join(f"  • {e[0]}" for e in exercises)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"rename_{wid}"),
         InlineKeyboardButton(text="📋 Изменить упражнения", callback_data=f"editex_{wid}")],
        [InlineKeyboardButton(text="🗑 Удалить тренировку", callback_data=f"del_{wid}")]
    ])
    await callback.message.answer(
        f"⚙️ *{name}*\n\nУпражнения:\n{ex_list}",
        reply_markup=kb, parse_mode="Markdown"
    )

# ---- Переименование ----
@dp.callback_query(F.data.startswith("rename_"))
async def rename_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    wid = int(callback.data.split("_")[1])
    await state.update_data(edit_wid=wid)
    await state.set_state(EditWorkout.waiting_for_new_name)
    await callback.message.answer("Введи новое название тренировки:")

@dp.message(EditWorkout.waiting_for_new_name)
async def rename_save(message: Message, state: FSMContext):
    data = await state.get_data()
    wid = data["edit_wid"]
    new_name = message.text.strip()

    async with aiosqlite.connect('gym_bot.db') as db:
        await db.execute("UPDATE workouts SET name=? WHERE id=? AND user_id=?",
                         (new_name, wid, message.from_user.id))
        await db.commit()

    await state.clear()
    await message.answer(f"✅ Тренировка переименована в *{new_name}*", reply_markup=main_kb, parse_mode="Markdown")

# ---- Изменение упражнений (с сохранением истории) ----
@dp.callback_query(F.data.startswith("editex_"))
async def editex_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    wid = int(callback.data.split("_")[1])

    async with aiosqlite.connect('gym_bot.db') as db:
        version = await get_current_version(db, wid)
        exercises = await db.execute_fetchall(
            "SELECT name FROM exercises WHERE workout_id=? AND version_id=? ORDER BY id",
            (wid, version)
        )

    current = ", ".join(e[0] for e in exercises)
    await state.update_data(edit_wid=wid)
    await state.set_state(EditWorkout.waiting_for_new_exercises)
    await callback.message.answer(
        f"Текущие упражнения:\n_{current}_\n\n"
        "Введи новый список упражнений *через запятую*.\n"
        "_(Старые результаты сохранятся — история не пропадёт)_",
        parse_mode="Markdown"
    )

@dp.message(EditWorkout.waiting_for_new_exercises)
async def editex_save(message: Message, state: FSMContext):
    data = await state.get_data()
    wid = data["edit_wid"]
    exercises = [e.strip() for e in message.text.split(",") if e.strip()]

    if not exercises:
        await message.answer("❌ Список пустой. Введи упражнения через запятую:")
        return

    async with aiosqlite.connect('gym_bot.db') as db:
        # Проверяем владельца
        row = await db.execute_fetchall(
            "SELECT id FROM workouts WHERE id=? AND user_id=?", (wid, message.from_user.id)
        )
        if not row:
            await message.answer("Тренировка не найдена.")
            await state.clear()
            return

        # Увеличиваем версию — старые упражнения остаются нетронутыми
        version = await get_current_version(db, wid)
        new_version = version + 1
        await db.execute(
            "UPDATE workout_versions SET current_version=? WHERE workout_id=?",
            (new_version, wid)
        )
        # Добавляем новые упражнения с новой версией
        for ex in exercises:
            await db.execute(
                "INSERT INTO exercises (workout_id, version_id, name) VALUES (?, ?, ?)",
                (wid, new_version, ex)
            )
        await db.commit()

    await state.clear()
    ex_list = "\n".join(f"  • {e}" for e in exercises)
    await message.answer(
        f"✅ Упражнения обновлены!\n\n{ex_list}\n\n"
        "_Предыдущие результаты сохранены и доступны в разделе Прогресс_",
        reply_markup=main_kb, parse_mode="Markdown"
    )

# ---- Удаление ----
@dp.callback_query(F.data.startswith("del_"))
async def delete_workout_confirm(callback: CallbackQuery):
    await callback.answer()
    wid = int(callback.data.split("_")[1])

    async with aiosqlite.connect('gym_bot.db') as db:
        row = await db.execute_fetchall(
            "SELECT name FROM workouts WHERE id=? AND user_id=?", (wid, callback.from_user.id)
        )
        if not row:
            await callback.message.answer("Тренировка не найдена.")
            return
        name = row[0][0]

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delconfirm_{wid}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="delcancel")
    ]])
    await callback.message.answer(
        f"Удалить тренировку *{name}*?\n_(Все результаты тоже удалятся)_",
        reply_markup=kb, parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("delconfirm_"))
async def delete_workout_do(callback: CallbackQuery):
    await callback.answer()
    wid = int(callback.data.split("_")[1])

    async with aiosqlite.connect('gym_bot.db') as db:
        row = await db.execute_fetchall(
            "SELECT name FROM workouts WHERE id=? AND user_id=?", (wid, callback.from_user.id)
        )
        if not row:
            await callback.message.answer("Тренировка не найдена.")
            return
        name = row[0][0]
        session_ids = await db.execute_fetchall("SELECT id FROM sessions WHERE workout_id=?", (wid,))
        for (sid,) in session_ids:
            await db.execute("DELETE FROM sets WHERE session_id=?", (sid,))
        await db.execute("DELETE FROM sessions WHERE workout_id=?", (wid,))
        await db.execute("DELETE FROM exercises WHERE workout_id=?", (wid,))
        await db.execute("DELETE FROM workout_versions WHERE workout_id=?", (wid,))
        await db.execute("DELETE FROM workouts WHERE id=?", (wid,))
        await db.commit()

    await callback.message.edit_text(f"🗑 Тренировка *{name}* удалена.", parse_mode="Markdown")

@dp.callback_query(F.data == "delcancel")
async def delete_cancel(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("Отменено.")

# ================= НАЧАТЬ ТРЕНИРОВКУ =================
@dp.message(F.text == "🏋️ Начать тренировку")
async def start_training(message: Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect('gym_bot.db') as db:
        rows = await db.execute_fetchall(
            "SELECT id, name FROM workouts WHERE user_id=? ORDER BY id DESC",
            (message.from_user.id,)
        )
    if not rows:
        await message.answer("У тебя нет тренировок. Сначала создай одну 📝")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r[1], callback_data=f"start_{r[0]}")]
        for r in rows
    ])
    await message.answer("Выбери тренировку:", reply_markup=kb)

@dp.callback_query(F.data.startswith("start_"))
async def start_session(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    wid = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    async with aiosqlite.connect('gym_bot.db') as db:
        row = await db.execute_fetchall(
            "SELECT name FROM workouts WHERE id=? AND user_id=?", (wid, user_id)
        )
        if not row:
            await callback.message.answer("Тренировка не найдена.")
            return
        workout_name = row[0][0]
        version = await get_current_version(db, wid)

        cur = await db.execute(
            "INSERT INTO sessions (workout_id, user_id, exercise_version) VALUES (?, ?, ?)",
            (wid, user_id, version)
        )
        sid = cur.lastrowid
        ex_rows = await db.execute_fetchall(
            "SELECT id, name FROM exercises WHERE workout_id=? AND version_id=? ORDER BY id",
            (wid, version)
        )
        await db.commit()

    exercises = [{"id": e[0], "name": e[1]} for e in ex_rows]
    await state.update_data(session_id=sid, exercises=exercises, set_counts={}, workout_name=workout_name, workout_id=wid)

    await callback.message.answer(
        f"💪 *{workout_name}* — поехали!\n\nВыбирай упражнение и записывай подходы.",
        reply_markup=in_session_kb, parse_mode="Markdown"
    )
    await send_exercise_list(callback.message, state)

# ================= ВЫБОР УПРАЖНЕНИЯ =================
@dp.callback_query(F.data.startswith("ex_"))
async def select_ex(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    if "session_id" not in data:
        await callback.message.answer("Сессия не найдена. Начни тренировку заново.")
        return

    ex_id = int(callback.data.split("_")[1])
    ex = next((e for e in data.get("exercises", []) if int(e["id"]) == ex_id), None)
    if not ex:
        await callback.message.answer("Упражнение не найдено.")
        return

    set_counts = data.get("set_counts", {})
    count = set_counts.get(str(ex_id), 0)

    await state.update_data(current_ex_id=ex_id, current_ex_name=ex["name"])
    await state.set_state(LogSession.waiting_for_sets)

    hint = "Введи первый подход 👇" if count == 0 else \
           f"Записано подходов: *{count}* ✅\nВведи ещё один или нажми ⬅️ для другого упражнения"

    await callback.message.answer(
        f"🏋️ *{ex['name']}*\n\n{hint}\n\n"
        f"Формат: `вес повторы`\nПример: `80 8` — 80кг на 8 повторов",
        reply_markup=in_session_kb, parse_mode="Markdown"
    )

# ================= ЗАПИСЬ ПОДХОДА =================
@dp.message(LogSession.waiting_for_sets, F.text == "⬅️ К списку упражнений")
async def back_to_list(message: Message, state: FSMContext):
    await state.update_data(current_ex_id=None, current_ex_name=None)
    await send_exercise_list(message, state)

@dp.message(StateFilter(LogSession.waiting_for_sets, LogSession.choosing_exercise),
            F.text == "🛑 Завершить тренировку")
async def finish_workout(message: Message, state: FSMContext):
    data = await state.get_data()
    session_id = data.get("session_id")
    workout_name = data.get("workout_name", "Тренировка")
    workout_id = data.get("workout_id")
    user_id = message.from_user.id
    await state.clear()

    if not session_id:
        await message.answer("Тренировка завершена.", reply_markup=main_kb)
        return

    async with aiosqlite.connect('gym_bot.db') as db:
        summary = await get_session_summary(db, session_id)
        progress = await get_exercise_progress(db, workout_id, user_id) if workout_id else ""

    text = f"🏁 *{workout_name} завершена!*\n\n{summary}"
    if progress:
        text += f"\n\n📈 *Динамика:*\n{progress}"
    text += "\n\nОтличная работа! 💪"

    await message.answer(text, reply_markup=main_kb, parse_mode="Markdown")

@dp.message(LogSession.waiting_for_sets)
async def add_set(message: Message, state: FSMContext):
    data = await state.get_data()
    if "session_id" not in data:
        await message.answer("Ошибка сессии. Начни тренировку заново.", reply_markup=main_kb)
        await state.clear()
        return

    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            raise ValueError
        w = float(parts[0].replace(",", "."))
        r = int(parts[1])
        if w <= 0 or r <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("❌ Неверный формат.\n\nВведи: `вес повторы`\nПример: `80 8`", parse_mode="Markdown")
        return

    async with aiosqlite.connect('gym_bot.db') as db:
        await db.execute(
            "INSERT INTO sets (session_id, exercise_id, reps, weight) VALUES (?, ?, ?, ?)",
            (data["session_id"], data["current_ex_id"], r, w)
        )
        await db.commit()

    set_counts = data.get("set_counts", {})
    ex_key = str(data["current_ex_id"])
    set_counts[ex_key] = set_counts.get(ex_key, 0) + 1
    await state.update_data(set_counts=set_counts)
    total = set_counts[ex_key]

    await message.answer(
        f"✅ *{data['current_ex_name']}*: {fmt_weight(w)}кг × {r}\n\n"
        f"_Подход {total} записан._\n"
        f"Введи следующий подход или нажми ⬅️ для другого упражнения.",
        parse_mode="Markdown"
    )

# ================= ПРОГРЕСС И РЕЗУЛЬТАТЫ =================
@dp.message(F.text == "📊 Прогресс и результаты")
async def progress_choose_workout(message: Message):
    user_id = message.from_user.id
    async with aiosqlite.connect('gym_bot.db') as db:
        rows = await db.execute_fetchall('''
            SELECT DISTINCT w.id, w.name
            FROM workouts w
            JOIN sessions s ON s.workout_id = w.id
            JOIN sets st ON st.session_id = s.id
            WHERE w.user_id = ?
            ORDER BY w.id DESC
        ''', (user_id,))

    if not rows:
        await message.answer("Нет записанных тренировок.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r[1], callback_data=f"prog_{r[0]}")]
        for r in rows
    ])
    await message.answer("Выбери тренировку:", reply_markup=kb)

@dp.callback_query(F.data.startswith("prog_"))
async def progress_show_menu(callback: CallbackQuery):
    await callback.answer()
    wid = int(callback.data.split("_")[1])
    async with aiosqlite.connect('gym_bot.db') as db:
        row = await db.execute_fetchall(
            "SELECT name FROM workouts WHERE id=? AND user_id=?", (wid, callback.from_user.id)
        )
        if not row:
            await callback.message.answer("Тренировка не найдена.")
            return
        name = row[0][0]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Прогресс по упражнениям", callback_data=f"exprg_{wid}")],
        [InlineKeyboardButton(text="🕐 История тренировок", callback_data=f"hist_{wid}")]
    ])
    await callback.message.answer(f"*{name}* — что показать?", reply_markup=kb, parse_mode="Markdown")

# --- Прогресс по упражнениям ---
@dp.callback_query(F.data.startswith("exprg_"))
async def show_exercise_progress(callback: CallbackQuery):
    await callback.answer()
    wid = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    async with aiosqlite.connect('gym_bot.db') as db:
        row = await db.execute_fetchall(
            "SELECT name FROM workouts WHERE id=? AND user_id=?", (wid, user_id)
        )
        if not row:
            await callback.message.answer("Тренировка не найдена.")
            return
        name = row[0][0]
        progress = await get_exercise_progress(db, wid, user_id)

    await callback.message.answer(
        f"📈 *Прогресс — {name}*\n_(лучший подход: последняя vs предыдущая)_\n\n{progress}",
        parse_mode="Markdown"
    )

# --- История тренировок (3 точки: последняя / ~неделя / ~месяц) ---
@dp.callback_query(F.data.startswith("hist_"))
async def show_history(callback: CallbackQuery):
    await callback.answer()
    wid = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    async with aiosqlite.connect('gym_bot.db') as db:
        row = await db.execute_fetchall(
            "SELECT name FROM workouts WHERE id=? AND user_id=?", (wid, user_id)
        )
        if not row:
            await callback.message.answer("Тренировка не найдена.")
            return
        workout_name = row[0][0]

        sessions = await db.execute_fetchall('''
            SELECT s.id, s.date FROM sessions s
            WHERE s.workout_id=? AND s.user_id=?
              AND EXISTS (SELECT 1 FROM sets WHERE session_id=s.id)
            ORDER BY s.date DESC
        ''', (wid, user_id))

        if not sessions:
            await callback.message.answer(f"По тренировке *{workout_name}* нет результатов.", parse_mode="Markdown")
            return

        now = datetime.now()

        last = sessions[0]
        last_date = parse_date(last[1])
        last_summary = await get_session_summary(db, last[0])

        week_session = next((s for s in sessions if parse_date(s[1]) <= now - timedelta(days=7)), sessions[-1])
        month_session = next((s for s in sessions if parse_date(s[1]) <= now - timedelta(days=30)), sessions[-1])

        blocks = []
        d = (now - last_date).days
        blocks.append(f"🕐 *Последняя* ({last_date.strftime('%d.%m.%Y')}, {days_label(d)})\n{last_summary}")

        if week_session[0] != last[0]:
            wd = parse_date(week_session[1])
            w_summary = await get_session_summary(db, week_session[0])
            blocks.append(f"📅 *~Неделю назад* ({wd.strftime('%d.%m.%Y')}, {days_label((now-wd).days)})\n{w_summary}")
        else:
            blocks.append("📅 *~Неделю назад*\n_Нет данных — только одна тренировка_")

        if month_session[0] != last[0] and month_session[0] != week_session[0]:
            md = parse_date(month_session[1])
            m_summary = await get_session_summary(db, month_session[0])
            blocks.append(f"🗓 *~Месяц назад* ({md.strftime('%d.%m.%Y')}, {days_label((now-md).days)})\n{m_summary}")
        elif month_session[0] == week_session[0] and month_session[0] != last[0]:
            blocks.append("🗓 *~Месяц назад*\n_Совпадает с блоком выше_")
        else:
            blocks.append("🗓 *~Месяц назад*\n_Нет данных_")

    sep = "\n\n" + "─" * 20 + "\n\n"
    await callback.message.answer(
        f"🗂 *История — {workout_name}*\n\n" + sep.join(blocks),
        parse_mode="Markdown"
    )

# ================= RUN =================
async def main():
    await init_db()
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
