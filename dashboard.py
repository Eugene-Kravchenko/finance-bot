"""Пульт внутри бота: сегодня, 7/30 дней, сон, настроение и энергия, заметки и контекст,
сводка для врача и настройка доступа. Только показ фактов из записей."""
import asyncio
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import access
import charts
import config
import doctor
import stats
import storage

LOG = logging.getLogger(__name__)
MAX_TEXT = 3800


def menu_keyboard(role: str) -> InlineKeyboardMarkup:
    rows = [
        [Btn("Сегодня", callback_data="p:today"), Btn("7 дней", callback_data="p:7"),
         Btn("30 дней", callback_data="p:30")],
        [Btn("Сон", callback_data="p:sleep"), Btn("Настроение и энергия", callback_data="p:me")],
        [Btn("Заметки и контекст", callback_data="p:notes")],
        [Btn("Сводка для врача (PDF)", callback_data="p:doc")],
    ]
    if role == "owner":
        rows.append([Btn("Доступ для другого человека", callback_data="p:acc")])
    return InlineKeyboardMarkup(rows)


BACK = [Btn("« Пульт", callback_data="p:menu")]


def after_checkin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("Контекст дня", callback_data="p:ctx"), Btn("Добавить заметку", callback_data="p:note")],
        [Btn("Пульт", callback_data="p:menu")],
    ])


def _now() -> datetime:
    return datetime.now(config.TZ)


# ---------- тексты ----------

def today_text(t: access.Target) -> str:
    day = stats.last_days(t.owner_chat_id, 1)[0]
    lines = [f"Сегодня, {day.day:%d.%m}"]
    for s in day.slots:
        label = f"{s.at:%H:%M}"
        if s.status == "filled":
            for c in s.checkins:
                sleep = f", сон {stats.fmt_num(c.sleep_hours)} ч" if c.sleep_hours is not None else ""
                lines.append(f"{label} (отмечено в {c.at:%H:%M}) — настроение {c.mood}, "
                             f"энергия {c.energy}, тревога {c.anxiety}{sleep}")
        else:
            lines.append(f"{label} — " + {
                "missed": "пропуск (нет отметки)", "upcoming": "ещё впереди",
                "paused": "пауза", "before_start": "до начала наблюдений",
            }[s.status])
    lines.append("Контекст: " + (", ".join(config.CONTEXT_TAGS.get(x, x) for x in day.context) or "не отмечен"))
    if t.show_private:
        n = len(day.notes) + sum(bool(c.note) for c in day.checkins)
        lines.append(f"Заметок за день: {n}")
    return "\n".join(lines)


def period_text(days: list[stats.Day], title: str) -> str:
    s = stats.summarize(days)
    lines = [
        f"{title} ({days[0].day:%d.%m}–{days[-1].day:%d.%m})",
        f"Отметок по расписанию: {s.filled} из {s.expected}, пропусков: {s.missed}"
        + (f", на паузе: {s.paused}" if s.paused else ""),
    ]
    if s.checkins > s.filled:
        lines.append(f"Всего отметок с дополнительными: {s.checkins}")
    for m in stats.METRICS:
        lines.append(f"• {stats.METRIC_LABELS[m]}: {stats.fmt_stat(s.metrics[m])}")
    lines.append(f"• Сон: {stats.fmt_stat(s.sleep, ' ч')}; дней без данных о сне: {s.days - s.sleep_days}")
    if s.meds:
        lines.append("• Лекарства по назначению: " + ", ".join(
            f"{config.MEDS_LABELS.get(k, k)} — {v}" for k, v in s.meds.items()))
    if s.context:
        lines.append("• Контекст: " + ", ".join(
            f"{config.CONTEXT_TAGS.get(k, k)} — {v} дн." for k, v in s.context.items()))
    lines += ["", "Пропуски не считаются нулями и не входят в средние.", config.DISCLAIMER]
    return "\n".join(lines)


def daily_ranges_text(days: list[stats.Day]) -> str:
    lines = ["По дням (мин–макс за день): настроение / энергия"]
    for d in reversed(days):
        if not d.checkins:
            state = "пауза" if d.count("paused") else ("нет отметок" if d.expected else "—")
            lines.append(f"{d.day:%d.%m}: {state}")
        else:
            lines.append(f"{d.day:%d.%m}: {stats.fmt_range(d.values('mood'))} / "
                         f"{stats.fmt_range(d.values('energy'))} "
                         f"({d.count('filled')} из {d.expected})")
    return "\n".join(lines)


def notes_text(t: access.Target, days: list[stats.Day]) -> str:
    lines = [f"Заметки и контекст за {len(days)} дней"]
    for d in reversed(days):
        items = []
        if d.context:
            items.append("контекст: " + ", ".join(config.CONTEXT_TAGS.get(x, x) for x in d.context))
        if t.show_private:
            for c in d.checkins:
                if c.note:
                    items.append(f"{c.at:%H:%M} {c.note}")
                if c.side_effects:
                    items.append(f"{c.at:%H:%M} побочные эффекты: {c.side_effects}")
            items += [f"{at:%H:%M} {text}" for at, text in d.notes]
        if items:
            lines.append(f"\n{d.day:%d.%m}")
            lines += [f"• {x}" for x in items]
    if not t.show_private:
        lines.append("\nТекст заметок скрыт: участница не открыла его.")
    if len(lines) == 1:
        lines.append("Записей нет.")
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n…(полностью — в сводке для врача)"


def context_keyboard(owner: int) -> InlineKeyboardMarkup:
    today = _now().date().isoformat()
    with storage.db() as conn:
        active = {r["value"] for r in conn.execute(
            "SELECT value FROM day_entries WHERE chat_id=? AND day=? AND kind='context' AND removed_at IS NULL",
            (owner, today))}
    rows = [[Btn(("✓ " if code in active else "") + label, callback_data=f"p:ctx:{code}")]
            for code, label in config.CONTEXT_TAGS.items()]
    rows.append([Btn("Готово", callback_data="p:menu")])
    return InlineKeyboardMarkup(rows)


def toggle_context(owner: int, code: str) -> None:
    now = _now()
    today = now.date().isoformat()
    with storage.db() as conn:
        row = conn.execute(
            """SELECT id FROM day_entries WHERE chat_id=? AND day=? AND kind='context'
               AND value=? AND removed_at IS NULL""", (owner, today, code)).fetchone()
        if row:
            conn.execute("UPDATE day_entries SET removed_at=? WHERE id=?", (now.isoformat(), row["id"]))
        else:
            conn.execute(
                "INSERT INTO day_entries(chat_id, day, kind, value, created_at) VALUES (?, ?, 'context', ?, ?)",
                (owner, today, code, now.isoformat()))


def access_text_and_keyboard(owner: int):
    row = access.get(owner)
    if not row or not row["viewer_chat_id"]:
        text = ("Сейчас записи видишь только ты.\n\n"
                "Можно дать доступ одному человеку: он сможет смотреть пульт и сводку для врача. "
                "Включить и выключить доступ можешь только ты, в любой момент.")
        if row and row["invite_code"]:
            text += (f"\n\nДействующий код: {row['invite_code']} "
                     f"(до {datetime.fromisoformat(row['invite_expires_at']):%d.%m %H:%M}). "
                     f"Человек отправляет боту: /join {row['invite_code']}")
        kb = [[Btn("Создать код приглашения", callback_data="p:acc:invite")]]
    else:
        name = row["viewer_name"] or "второй человек"
        state = "ВКЛЮЧЁН" if row["enabled"] else "выключен"
        notes = "видны" if row["share_notes"] else "скрыты"
        text = (f"Доступ для «{name}»: {state}.\n"
                f"Оценки, сон, лекарства и контекст — {'видны' if row['enabled'] else 'не видны'}.\n"
                f"Текст заметок, побочные эффекты и ответы о безопасности — {notes}.")
        kb = [
            [Btn("Выключить доступ" if row["enabled"] else "Включить доступ",
                 callback_data="p:acc:off" if row["enabled"] else "p:acc:on")],
            [Btn("Скрыть заметки" if row["share_notes"] else "Показывать заметки",
                 callback_data="p:acc:notes_off" if row["share_notes"] else "p:acc:notes_on")],
            [Btn("Удалить этого человека", callback_data="p:acc:remove")],
        ]
    return text, InlineKeyboardMarkup(kb + [BACK])


# ---------- обработчики ----------

async def pult_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_chat and update.message
    t = access.resolve_target(update.effective_chat.id)
    if not t:
        await update.message.reply_text("Нет доступа к записям.")
        return
    await update.message.reply_text("Пульт", reply_markup=menu_keyboard(t.role))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query and query.data and query.message and update.effective_chat and update.effective_user
    await query.answer()  # сразу снимаем «часики» у кнопки
    data = query.data
    chat_id = update.effective_chat.id

    if data.startswith("p:share:"):
        await _share_decision(update, data)
        return

    t = access.resolve_target(chat_id)
    if not t:
        await query.message.reply_text("Нет доступа к записям.")
        return
    msg = query.message
    owner = t.owner_chat_id

    if data == "p:menu":
        await msg.reply_text("Пульт", reply_markup=menu_keyboard(t.role))
    elif data == "p:today":
        await msg.reply_text(today_text(t), reply_markup=InlineKeyboardMarkup([BACK]))
    elif data in ("p:7", "p:30"):
        n = int(data[2:])
        days = await asyncio.to_thread(stats.last_days, owner, n)
        png = await asyncio.to_thread(charts.scales_png, days, f"{n} дней: каждая отметка",
                                      stats.METRICS)
        await msg.reply_text(period_text(days, f"{n} дней"))
        await msg.reply_photo(png, reply_markup=InlineKeyboardMarkup([BACK]))
    elif data == "p:sleep":
        days = await asyncio.to_thread(stats.last_days, owner, 30)
        s7, s30 = stats.summarize(days[-7:]), stats.summarize(days)
        text = (f"Сон\n7 дней: {stats.fmt_stat(s7.sleep, ' ч')}, без данных: {7 - s7.sleep_days} дн.\n"
                f"30 дней: {stats.fmt_stat(s30.sleep, ' ч')}, без данных: {30 - s30.sleep_days} дн.\n"
                "За день берётся ответ утреннего опроса (если его нет — первый ответ за день).")
        png = await asyncio.to_thread(charts.sleep_png, days, "Сон за 30 дней")
        await msg.reply_text(text)
        await msg.reply_photo(png, reply_markup=InlineKeyboardMarkup([BACK]))
    elif data == "p:me":
        days = await asyncio.to_thread(stats.last_days, owner, 14)
        png = await asyncio.to_thread(charts.scales_png, days, "Настроение и энергия, 14 дней")
        await msg.reply_text(daily_ranges_text(days[-7:]))
        await msg.reply_photo(png, reply_markup=InlineKeyboardMarkup([BACK]))
    elif data == "p:notes":
        days = await asyncio.to_thread(stats.last_days, owner, 14)
        kb = [BACK]
        if t.role == "owner":
            kb = [[Btn("Контекст дня", callback_data="p:ctx"),
                   Btn("Добавить заметку", callback_data="p:note")], BACK]
        await msg.reply_text(notes_text(t, days), reply_markup=InlineKeyboardMarkup(kb))
    elif data == "p:doc":
        await msg.reply_text("За какой период сделать сводку?", reply_markup=InlineKeyboardMarkup([
            [Btn("14 дней", callback_data="p:doc:14"), Btn("30 дней", callback_data="p:doc:30"),
             Btn("90 дней", callback_data="p:doc:90")], BACK]))
    elif data.startswith("p:doc:"):
        n = int(data.split(":")[2])
        await msg.reply_text("Собираю сводку…")
        pdf = await asyncio.to_thread(doctor.build_pdf, owner, n, t.show_private)
        end = _now().date()
        name = f"svodka_{end - timedelta(days=n - 1)}_{end}.pdf"
        await msg.reply_document(pdf, filename=name,
                                 caption=f"Сводка за {n} дней. {config.DISCLAIMER}")
    elif t.role != "owner":
        await msg.reply_text("Это действие доступно только участнице.")
    elif data == "p:ctx":
        await msg.reply_text("Контекст сегодняшнего дня (можно отметить несколько, повторное нажатие снимает):",
                             reply_markup=context_keyboard(owner))
    elif data.startswith("p:ctx:"):
        code = data.split(":")[2]
        if code in config.CONTEXT_TAGS:
            toggle_context(owner, code)
        await query.edit_message_reply_markup(context_keyboard(owner))
    elif data == "p:note":
        context.user_data["awaiting_note"] = True
        await msg.reply_text("Напиши заметку одним сообщением. Отмена — /cancel")
    elif data.startswith("p:acc"):
        await _access_action(update, owner, data)


async def _access_action(update: Update, owner: int, data: str) -> None:
    msg = update.callback_query.message
    if data == "p:acc:invite":
        code = access.create_invite(owner)
        await msg.reply_text(
            f"Код приглашения: {code}\nОн действует 24 часа и сработает один раз.\n"
            f"Человек отправляет боту: /join {code}\n"
            "После этого я спрошу тебя, разрешить ли доступ. Без твоего «да» он ничего не увидит.")
    elif data in ("p:acc:on", "p:acc:off"):
        access.set_enabled(owner, data.endswith("on"))
    elif data in ("p:acc:notes_on", "p:acc:notes_off"):
        access.set_share_notes(owner, data.endswith("on"))
    elif data == "p:acc:remove":
        access.remove_viewer(owner)
    text, kb = access_text_and_keyboard(owner)
    await msg.reply_text(text, reply_markup=kb)


async def _share_decision(update: Update, data: str) -> None:
    owner = update.effective_chat.id
    msg = update.callback_query.message
    if not access.is_participant(owner):
        return
    if data == "p:share:allow":
        access.set_enabled(owner, True)
        await msg.edit_text("Доступ включён. Выключить можно в Пульт → Доступ.")
    else:
        access.remove_viewer(owner)
        await msg.edit_text("Запрос отклонён, человек отвязан.")


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_chat and update.effective_user and update.message
    code = context.args[0] if context.args else ""
    user = update.effective_user
    name = user.full_name or (user.username or "без имени")
    owner = access.accept_invite(code, update.effective_chat.id, name)
    if owner is None:
        await update.message.reply_text("Код не подошёл или устарел. Попроси новый код.")
        return
    await update.message.reply_text("Запрос отправлен. Доступ появится, только если участница его подтвердит.")
    await context.bot.send_message(
        owner,
        f"{name} ввёл(а) твой код и просит доступ к записям (оценки, сон, лекарства, контекст; "
        "заметки по умолчанию скрыты). Разрешить?",
        reply_markup=InlineKeyboardMarkup([[Btn("Разрешить", callback_data="p:share:allow"),
                                             Btn("Отклонить", callback_data="p:share:deny")]]),
    )


async def note_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_note") or not update.message or not update.effective_chat:
        return
    owner = update.effective_chat.id
    if not access.is_participant(owner):
        return
    context.user_data.pop("awaiting_note", None)
    now = _now()
    with storage.db() as conn:
        conn.execute(
            "INSERT INTO day_entries(chat_id, day, kind, value, created_at) VALUES (?, ?, 'note', ?, ?)",
            (owner, now.date().isoformat(), update.message.text.strip(), now.isoformat()))
    await update.message.reply_text("Заметка сохранена.", reply_markup=after_checkin_keyboard())


async def cancel_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.pop("awaiting_note", None) and update.message:
        await update.message.reply_text("Заметка отменена.")


def register(application: Application, wrap) -> None:
    application.add_handler(CommandHandler("pult", wrap(pult_command)))
    application.add_handler(CommandHandler("join", wrap(join_command)))
    application.add_handler(CommandHandler("cancel", wrap(cancel_note)))
    application.add_handler(CallbackQueryHandler(wrap(on_button), pattern=r"^p:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wrap(note_text)))
