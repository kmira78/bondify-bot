#!/usr/bin/env python3
"""Bondify — OFZ curve + bond calculator."""
import asyncio
import io
import os
import logging
import re
from datetime import date, datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters,
)

from moex import fetch_bonds, warmup_cache, compute_ofz_duration
from zcyc import fetch_zcyc, zcyc_yield
from charts import generate_ofz_curve
from bond_calc import BondSpec, calc_from_price, calc_from_ytm as bond_calc_from_ytm

TOKEN = os.environ["BOND_BOT_TOKEN"]
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALLOWED_USERS = {144573290, 214033368, 327097927, 126366325, 330173037}

YTMD_MODE, YTMD_PARAMS = range(2)

# ── Keyboards ──────────────────────────────────────────────────────────────

def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t, callback_data=d) for t, d in row]
        for row in rows
    ])


MAIN_MENU_KB = kb([
    [("📈 Карта ОФЗ", "cmd_ofz"), ("🧮 Облигационный калькулятор", "cmd_ytmdcalc")],
])

BACK_KB = kb([[("↩️ Главное меню", "menu")]])

# ── Access check ───────────────────────────────────────────────────────────

async def _check_access(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if uid not in ALLOWED_USERS:
        if update.message:
            await update.message.reply_text(f"Доступ закрыт. Ваш ID: {uid}")
        return False
    return True

# ── Menu ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    ctx.user_data.clear()
    await update.message.reply_text(
        "👋 <b>Bondify — анализ облигаций</b>\n\nДанные MOEX в реальном времени.",
        parse_mode="HTML", reply_markup=MAIN_MENU_KB,
    )


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    await update.message.reply_text(
        "📌 <b>Главное меню</b>", parse_mode="HTML", reply_markup=MAIN_MENU_KB,
    )


async def cb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.message and (q.message.photo or q.message.document or q.message.video):
        await q.message.delete()
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📌 <b>Главное меню</b>",
            parse_mode="HTML",
            reply_markup=MAIN_MENU_KB,
        )
    else:
        await q.edit_message_text(
            "📌 <b>Главное меню</b>", parse_mode="HTML", reply_markup=MAIN_MENU_KB,
        )
    return ConversationHandler.END


# ── OFZ chart ──────────────────────────────────────────────────────────────

async def _ofz_chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE, from_callback=False):
    if from_callback:
        q = update.callback_query
        await q.answer()
        msg = await q.edit_message_text("⏳ Загружаю данные ОФЗ и КБД...")
    else:
        msg = await update.message.reply_text("⏳ Загружаю данные ОФЗ и КБД...")

    bonds, zcyc_pts = await asyncio.gather(fetch_bonds("ofz"), fetch_zcyc())

    # Filter: fixed coupon only, exclude ОФЗ-ПК (SU52xxx, SU46xxx), compute Macaulay duration
    EXCLUDE_PREFIXES = ("SU52", "SU46")
    enriched = []
    for b in bonds:
        if b.get("coupon_type") != "fixed" or not b.get("yield") or not b.get("price"):
            continue
        if b["secid"].upper().startswith(EXCLUDE_PREFIXES):
            continue
        dur = compute_ofz_duration(b)
        if dur and dur > 0:
            enriched.append({**b, "duration_years": dur})

    if not enriched:
        await msg.edit_text("❌ Нет данных ОФЗ с фиксированным купоном.", reply_markup=BACK_KB)
        return

    try:
        png = generate_ofz_curve(enriched, zcyc_pts)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка графика: {e}", reply_markup=BACK_KB)
        return

    ytm_min = min(float(b["yield"]) for b in enriched)
    ytm_max = max(float(b["yield"]) for b in enriched)

    caption = (
        f"📈 <b>Карта рынка ОФЗ (фикс. купон)</b>\n"
        f"Бумаг: {len(enriched)} | YTM: {ytm_min:.1f}% – {ytm_max:.1f}%"
    )
    chat_id = update.effective_chat.id
    await ctx.bot.send_photo(chat_id=chat_id, photo=io.BytesIO(png),
                             caption=caption, parse_mode="HTML",
                             reply_markup=BACK_KB)
    await msg.delete()


async def cmd_ofz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    await _ofz_chart(update, ctx, from_callback=False)


async def cb_ofz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _ofz_chart(update, ctx, from_callback=True)


# ── Professional bond calculator (YTM_D_Calc) ──────────────────────────────

_PROMPT_PRICE = (
    "Введи параметры <b>одной строкой</b> через <code>;</code> (6 полей):\n\n"
    "<code>ставка%; период_дн; кол-во_купонов; дата_1го_купона; амортизация; чистая_цена%</code>\n\n"
    "<b>Без амортизации:</b>\n"
    "<code>12.5; 182; 10; 15.05.2027; -; 98.5</code>\n\n"
    "<b>С амортизацией</b> (купон:%, через запятую):\n"
    "<code>12.5; 182; 10; 15.05.2027; 5:10,8:20,10:70; 98.5</code>\n\n"
    "Дата: <code>ДД.ММ.ГГГГ</code> или <code>-</code> (сегодня + период)"
)

_PROMPT_YTM = (
    "Введи параметры <b>одной строкой</b> через <code>;</code> (6 полей):\n\n"
    "<code>ставка%; период_дн; кол-во_купонов; дата_1го_купона; амортизация; YTM%</code>\n\n"
    "<b>Без амортизации:</b>\n"
    "<code>12.5; 182; 10; 15.05.2027; -; 17.5</code>\n\n"
    "<b>С амортизацией:</b>\n"
    "<code>12.5; 182; 10; 15.05.2027; 5:10,8:20,10:70; 17.5</code>\n\n"
    "Дата: <code>ДД.ММ.ГГГГ</code> или <code>-</code> (сегодня + период)"
)


def _parse_float(s: str) -> float:
    return float(s.replace(",", ".").strip())


def _parse_date_ytmd(s: str) -> date:
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"не понял дату '{s}'")


def _parse_amort(s: str, n_coupons: int) -> dict:
    s = s.strip().lower()
    if s in ("", "-", "нет", "no", "n", "—"):
        return {}
    out: dict = {}
    for part in re.split(r"[,\n]+", s):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^\s*(\d+)\s*[:=]\s*([\d.,]+)\s*%?\s*$", part)
        if not m:
            raise ValueError(f"амортизация — не понял '{part}', формат: купон:процент")
        k, v = int(m.group(1)), _parse_float(m.group(2))
        if not (1 <= k <= n_coupons):
            raise ValueError(f"купон #{k} вне диапазона 1..{n_coupons}")
        out[k] = out.get(k, 0.0) + v
    if out and abs(sum(out.values()) - 100.0) > 1e-6:
        raise ValueError(f"амортизация — сумма {sum(out.values()):.2f}, должна быть 100")
    return out


async def cb_ytmdcalc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.pop("ytmd_mode", None)
    await q.edit_message_text(
        "🧮 <b>Облигационный калькулятор</b>\n\n"
        "YTM, НКД, дюрация Маколея, мод. дюрация, PVBP, выпуклость, G-спреды.\n"
        "Методология Cbonds · Actual/365F\n\n"
        "Что считаем?",
        parse_mode="HTML",
        reply_markup=kb([
            [("📉 Цена → YTM", "ytmd_price"), ("📈 YTM → Цена", "ytmd_ytm")],
            [("↩️ Меню", "menu")],
        ]),
    )
    return YTMD_MODE


async def cmd_ytmdcalc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    ctx.user_data.pop("ytmd_mode", None)
    await update.message.reply_text(
        "🧮 <b>Облигационный калькулятор</b>\n\n"
        "YTM, НКД, дюрация Маколея, мод. дюрация, PVBP, выпуклость, G-спреды.\n"
        "Методология Cbonds · Actual/365F\n\n"
        "Что считаем?",
        parse_mode="HTML",
        reply_markup=kb([
            [("📉 Цена → YTM", "ytmd_price"), ("📈 YTM → Цена", "ytmd_ytm")],
            [("↩️ Меню", "menu")],
        ]),
    )
    return YTMD_MODE


async def ytmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = q.data
    ctx.user_data["ytmd_mode"] = mode
    prompt = _PROMPT_PRICE if mode == "ytmd_price" else _PROMPT_YTM
    await q.edit_message_text(prompt, parse_mode="HTML")
    return YTMD_PARAMS


async def ytmd_params(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    parts = [p.strip() for p in raw.split(";")]
    if len(parts) != 6:
        await update.message.reply_text(
            f"❌ Жду 6 полей через <code>;</code>, получил {len(parts)}. Повтори:",
            parse_mode="HTML",
        )
        return YTMD_PARAMS

    s_rate, s_period, s_n, s_date, s_amort, s_val = parts
    try:
        coupon_rate = _parse_float(s_rate)
        if not (0 <= coupon_rate <= 1000):
            raise ValueError("ставка вне 0..1000")
        period = int(_parse_float(s_period))
        if not (1 <= period <= 3650):
            raise ValueError("период вне 1..3650 дней")
        n_coupons = int(_parse_float(s_n))
        if not (1 <= n_coupons <= 200):
            raise ValueError("кол-во купонов вне 1..200")
        first_dt = (date.today() + timedelta(days=period)
                    if s_date.strip() == "-" else _parse_date_ytmd(s_date))
        if first_dt <= date.today():
            raise ValueError("дата 1-го купона должна быть в будущем")
        amort = _parse_amort(s_amort, n_coupons)
        v = _parse_float(s_val)
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Ошибка: {e}\n\nПовтори всю строку:", parse_mode="HTML"
        )
        return YTMD_PARAMS

    spec = BondSpec(
        coupon_rate=coupon_rate,
        coupon_period_days=period,
        n_coupons=n_coupons,
        settle_date=date.today(),
        first_coupon_date=first_dt,
        amort_schedule=amort,
    )
    try:
        mode = ctx.user_data.get("ytmd_mode", "ytmd_price")
        if mode == "ytmd_price":
            r = calc_from_price(spec, clean_price=v)
        else:
            r = bond_calc_from_ytm(spec, ytm=v / 100.0)
    except Exception as e:
        log.exception("ytmd calc failed")
        await update.message.reply_text(f"❌ Ошибка расчёта: {e}", reply_markup=BACK_KB)
        return ConversationHandler.END

    ytm_pct = r.ytm * 100
    dur = r.duration_years

    # G-spread calculations (run in parallel)
    zcyc_pts, ofz_bonds = await asyncio.gather(
        fetch_zcyc(),
        fetch_bonds("ofz"),
    )

    # G-spread to КБД
    kbd_y = zcyc_yield(dur, zcyc_pts) if zcyc_pts else None
    if kbd_y is not None:
        gs_kbd_bp = (ytm_pct - kbd_y) * 100
        gspread_kbd = f"{gs_kbd_bp:+.0f} б.п. (КБД {kbd_y:.2f}%)"
    else:
        gspread_kbd = "н/д"

    # G-spread to nearest fixed OFZ by Macaulay duration
    EXCLUDE_PREFIXES = ("SU52", "SU46")
    fixed_ofz = [b for b in ofz_bonds
                 if b.get("coupon_type") == "fixed" and b.get("yield") and b.get("price")
                 and not b["secid"].upper().startswith(EXCLUDE_PREFIXES)]
    ofz_dur_list = []
    for b in fixed_ofz:
        d = compute_ofz_duration(b)
        if d is not None:
            ofz_dur_list.append((d, float(b["yield"]), b["secid"]))

    if ofz_dur_list:
        nearest = min(ofz_dur_list, key=lambda x: abs(x[0] - dur))
        gs_ofz_bp = (ytm_pct - nearest[1]) * 100
        gspread_ofz = f"{gs_ofz_bp:+.0f} б.п. ({nearest[2]} d={nearest[0]:.2f}л, YTM={nearest[1]:.2f}%)"
    else:
        gspread_ofz = "н/д"

    # Cash flow table
    flows_lines = ["#   Дата        Дни   Купон  Амортиз  Ост.ном"]
    for f in r.flows:
        flows_lines.append(
            f"{f.idx:>2}  {f.pay_date.strftime('%d.%m.%Y')}  {f.days_from_settle:>4}  "
            f"{f.coupon:>6.3f}  {f.amort:>7.2f}  {f.n_outstanding_before:>8.2f}"
        )
    if len(flows_lines) > 26:
        flows_lines = flows_lines[:26] + [f"... и ещё {len(r.flows) - 25} строк"]

    result_text = (
        f"📊 <b>Результат</b>\n\n"
        f"<code>"
        f"Чистая цена:        {r.clean_price:>10.4f} %\n"
        f"НКД:                {r.accrued:>10.4f} %\n"
        f"Грязная цена:       {r.dirty_price:>10.4f} %\n"
        f"YTM (годовая):      {ytm_pct:>10.4f} %\n"
        f"Дюрация Маколея:    {r.duration_days:>10.2f} дн\n"
        f"                    {dur:>10.4f} лет\n"
        f"Мод. дюрация:       {r.mod_duration:>10.4f}\n"
        f"PVBP (на 1 б.п.):   {r.pvbp:>10.6f} %\n"
        f"Выпуклость:         {r.convexity:>10.4f}\n"
        f"</code>\n"
        f"<b>G-спред к КБД:</b>  <code>{gspread_kbd}</code>\n"
        f"<b>G-спред к ОФЗ:</b>  <code>{gspread_ofz}</code>\n\n"
        f"<b>Денежный поток:</b>\n"
        f"<code>{flows_lines[0]}\n"
        + "\n".join(flows_lines[1:])
        + "</code>"
    )
    await update.message.reply_text(result_text, parse_mode="HTML", reply_markup=kb([
        [("🔄 Ещё расчёт", "cmd_ytmdcalc"), ("↩️ Меню", "menu")],
    ]))
    return ConversationHandler.END


# ── App setup ──────────────────────────────────────────────────────────────

async def post_init(app):
    log.info("Warming up MOEX + ZCYC cache...")
    asyncio.create_task(warmup_cache())
    asyncio.create_task(fetch_zcyc())


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("ofz",   cmd_ofz))

    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("ytmdcalc",  cmd_ytmdcalc),
            CallbackQueryHandler(cb_ytmdcalc, pattern="^cmd_ytmdcalc$"),
        ],
        states={
            YTMD_MODE:   [CallbackQueryHandler(ytmd_mode, pattern="^ytmd_(price|ytm)$")],
            YTMD_PARAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ytmd_params)],
        },
        fallbacks=[CallbackQueryHandler(cb_menu, pattern="^menu$")],
        per_message=False,
    ))

    app.add_handler(CallbackQueryHandler(cb_ofz,  pattern="^cmd_ofz$"))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern="^menu$"))

    log.info("Starting bondify-clone...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
