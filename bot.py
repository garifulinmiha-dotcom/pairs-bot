import http.server
import socketserver
import threading
import os

PORT = int(os.environ.get("PORT", 10000))

class SimpleHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"User script is running!")

def run_server():
    with socketserver.TCPServer(("", PORT), SimpleHandler) as httpd:
        httpd.serve_forever()

server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()

"""
Тихий бот для заходов на пары в Google Meet.
Chrome НЕ показывается на экране: всё работает в фоне.

Запуск:  python bot.py
Вход в Google (один раз):  python bot.py login

Пары можно слать на сегодня и на завтра, по одной или сразу несколько:
    завтра
    1 пара 09:00-10:30
    Ссылка на встречу: https://meet.google.com/abc-defg-hij
    2 пара 10:45-12:15
    Ссылка на встречу: https://meet.google.com/abc-defg-hij
Слова: сегодня / завтра / послезавтра (без слова = сегодня).
"""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta

from telethon import TelegramClient, events
from playwright.async_api import async_playwright

# ===================== НАСТРОЙКИ =====================
API_ID = 32975921
API_HASH = "ebf1c054bafe1495ef9681f1cbc418fa"
GROUP_NAME = "пары"                # название группы с парами
SESSION_NAME = "pary_session"      # файл сессии Telegram
HEADLESS = True                    # True = без окна (тихо)
# =====================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "chrome_profile")

TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})")
LINK_RE = re.compile(r"https://meet\.google\.com/[a-z0-9\-]+", re.I)
DAY_RE = re.compile(r"послезавтра|завтра|сегодня", re.I)

# "Продолжить с отключением микрофона и камеры" и варианты
NO_MIC_PAT = (r"отключением микрофона|без микрофона|without microphone|"
              r"continue without|вимкненим мікрофон|без мікрофона")
# Кнопки "выключить" (они есть, только когда микрофон/камера ВКЛЮЧЕНЫ)
MIC_ON_PAT = r"^(Отключить микрофон|Turn off microphone|Вимкнути мікрофон)"
CAM_ON_PAT = r"^(Отключить камеру|Turn off camera|Вимкнути камеру)"
# Названия кнопок микрофона / камеры
MIC_LBL = re.compile(r"микрофон|microphone|мікрофон", re.I)
CAM_LBL = re.compile(r"камер|camera", re.I)
# Кнопка входа на встречу
JOIN_PAT = (r"^(Присоединиться|Присоединиться сейчас|Join now|Приєднатися|"
            r"Попросить о присоединении|Ask to join|Попросити про приєднання)$")
LEAVE_PAT = re.compile(r"Покинуть|Выйти из|Leave call|Вийти з|Завершити", re.I)
# Комната ожидания ("Подождите, пока организатор примет ваш запрос")
WAIT_PAT = re.compile(r"пока организатор|организатор примет|let you in|Waiting for the host|"
                      r"організатор прийме|поки організатор", re.I)
# Преподаватель отклонил вход
DENIED_PAT = re.compile(r"отклонил|отказано|не можете присоединиться|You can.t join|denied|"
                        r"відхилив|відмовлено", re.I)

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
PW = None
ctx = None
report_chat = None
planned = set()
schedule = []   # все известные пары: (название, начало, конец)


def log(kind, text):
    print(f"[{kind}] {text}", flush=True)


async def report(text, photo=None):
    try:
        if photo:
            await client.send_file(report_chat, photo, caption=text)
        else:
            await client.send_message(report_chat, text)
    except Exception as e:
        log("Ошибка", f"Telegram: {e}")


def next_pair_text(end):
    """Текст про следующую пару (ищем среди уже добавленных)."""
    future = sorted([s for s in schedule if s[1] >= end], key=lambda s: s[1])
    if future:
        label, start, _ = future[0]
        if start.date() == end.date():
            when = f"начало в {start:%H:%M}"
        else:
            when = f"начало {start:%d.%m} в {start:%H:%M}"
        return f"➡️ Следующая пара: {label} ({when})"
    return "➡️ Следующая пара пока не добавлена."


# ---------------------- БРАУЗЕР ----------------------
async def launch_browser(pw, headless):
    args = [
        "--disable-blink-features=AutomationControlled",
        "--mute-audio",
        "--no-first-run",
        # поддельные микрофон/камера: настоящие НЕ включаются,
        # а запрос разрешений не зависает
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
    ]
    if not headless and "login" not in sys.argv:
        args.append("--window-position=-32000,-32000")
    kwargs = dict(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        args=args,
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 800},
        locale="ru-RU",
    )
    try:
        return await pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception:
        log("Инфо", "Chrome не найден, использую встроенный Chromium")
        return await pw.chromium.launch_persistent_context(**kwargs)


async def do_login():
    async with async_playwright() as pw:
        context = await launch_browser(pw, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://accounts.google.com/")
        print("Войди в Google в открывшемся окне, потом нажми Enter здесь...")
        await asyncio.get_running_loop().run_in_executor(None, input)
        await context.close()
        print("Готово. Теперь запускай: python bot.py")


async def fix_user_agent(page):
    """Убираем 'Headless' из user-agent."""
    try:
        ua = await page.evaluate("navigator.userAgent")
        if "Headless" in ua:
            cdp = await ctx.new_cdp_session(page)
            await cdp.send("Network.setUserAgentOverride",
                           {"userAgent": ua.replace("HeadlessChrome", "Chrome")})
    except Exception:
        pass


async def count_btn(page, pattern):
    try:
        return await page.get_by_role("button", name=re.compile(pattern, re.I)).count()
    except Exception:
        return 0


async def click_btn(page, pattern, timeout=1500):
    try:
        loc = page.get_by_role("button", name=re.compile(pattern, re.I))
        if await loc.count() > 0:
            await loc.first.click(timeout=timeout)
            return True
    except Exception:
        pass
    return False


async def click_any(page, pattern, timeout=2000):
    """Нажать кнопку / ссылку / текст, подходящий под regex."""
    rx = re.compile(pattern, re.I)
    for loc in (page.get_by_role("button", name=rx),
                page.get_by_role("link", name=rx),
                page.get_by_text(rx)):
        try:
            if await loc.count() > 0:
                await loc.first.click(timeout=timeout)
                return True
        except Exception:
            pass
    return False


async def media_on_buttons(page):
    """Кнопки микрофона/камеры, которые сейчас ВКЛЮЧЕНЫ."""
    found = []
    try:
        els = page.locator('[data-is-muted="false"]')
        for i in range(await els.count()):
            el = els.nth(i)
            label = (await el.get_attribute("aria-label")) or ""
            if MIC_LBL.search(label) or CAM_LBL.search(label):
                found.append(el)
    except Exception:
        pass
    return found


async def mute_all(page):
    """Выключает микрофон и камеру. True, если всё выключено."""
    for _ in range(3):
        on = await media_on_buttons(page)
        by_name = (await count_btn(page, MIC_ON_PAT)) + (await count_btn(page, CAM_ON_PAT))
        if not on and not by_name:
            return True
        for el in on:
            try:
                await el.click(timeout=1500)
            except Exception:
                pass
        if await count_btn(page, MIC_ON_PAT):
            await click_btn(page, MIC_ON_PAT)
        if await count_btn(page, CAM_ON_PAT):
            await click_btn(page, CAM_ON_PAT)
        await page.wait_for_timeout(700)
    on = await media_on_buttons(page)
    by_name = (await count_btn(page, MIC_ON_PAT)) + (await count_btn(page, CAM_ON_PAT))
    return not on and not by_name


async def dump_buttons(page):
    """Список кнопок на странице — для диагностики."""
    try:
        names = await page.evaluate(
            """() => [...document.querySelectorAll('button,[role=button],a')]
                 .map(e => (e.getAttribute('aria-label') || e.innerText || '').trim())
                 .filter(Boolean).slice(0, 25)""")
        return " | ".join(n.replace("\n", " ")[:40] for n in names)
    except Exception:
        return ""


async def take_shot(page, name):
    path = os.path.join(BASE_DIR, name)
    try:
        await page.screenshot(path=path)
        return path
    except Exception:
        return None


async def try_join(page, link, end, label):
    """Возвращает: 'joined' / 'denied' / 'waiting' / 'failed'."""
    await page.goto(link, wait_until="domcontentloaded")

    # 1) окно входа: выключаем микрофон и камеру и жмём "Присоединиться"
    clicked_join = False
    for _ in range(40):
        if datetime.now() >= end:
            return "failed"
        await page.wait_for_timeout(1500)

        await click_any(page, r"^(Понятно|Got it|Зрозуміло)$", 800)
        # если вылезло окно про микрофон/камеру -> продолжить без них
        await click_any(page, NO_MIC_PAT, 1000)

        # пока микрофон/камера включены — не заходим
        if not await mute_all(page):
            continue

        if await click_any(page, JOIN_PAT, 2000):
            clicked_join = True
            break

    if not clicked_join:
        return "failed"

    # 2) ждём, пока преподаватель впустит (комната ожидания)
    told_waiting = False
    while datetime.now() < end:
        await page.wait_for_timeout(2500)
        try:
            body = await page.inner_text("body")
        except Exception:
            body = ""

        if DENIED_PAT.search(body):
            return "denied"

        if WAIT_PAT.search(body):
            if not told_waiting:
                told_waiting = True
                log("Инфо", f"{label}: жду, пока впустят")
                await report(f"⏳ Попросился на пару ({label}), жду, пока преподаватель впустит...")
            continue

        # настоящая пара: есть кнопка выхода, а комнаты ожидания уже нет
        if await count_btn(page, LEAVE_PAT.pattern):
            return "joined"

        # кнопка входа всё ещё на экране — нажимаем ещё раз
        await click_any(page, JOIN_PAT, 1000)

    return "waiting" if told_waiting else "failed"


async def attend(label, link, start, end):
    global ctx
    wait = (start - datetime.now()).total_seconds()
    if wait > 0:
        log("Инфо", f"{label}: жду {int(wait)} сек до {start:%d.%m %H:%M}")
        await asyncio.sleep(wait)

    # если за ночь браузер закрылся — запускаем заново
    try:
        page = await ctx.new_page()
    except Exception:
        log("Инфо", "Браузер был закрыт, запускаю заново")
        ctx = await launch_browser(PW, HEADLESS)
        page = await ctx.new_page()

    await fix_user_agent(page)
    status = "failed"
    try:
        for attempt in range(1, 4):
            if datetime.now() >= end:
                break
            log("Инфо", f"{label}: попытка входа {attempt}")
            try:
                status = await try_join(page, link, end, label)
            except Exception as e:
                log("Ошибка", f"{label}: {e}")
                status = "failed"
            if status in ("joined", "denied", "waiting"):
                break
            await page.wait_for_timeout(3000)

        if status != "joined":
            shot = await take_shot(page, "fail.png")
            btns = await dump_buttons(page)
            if status == "denied":
                head = "🔴 Преподаватель не впустил на пару"
            elif status == "waiting":
                head = "🔴 Не дождался, пока впустят на пару"
            else:
                head = "🔴 Не удалось зайти на пару"
            await report(f"{head} ({label}). Смотри скрин.\n{link}\n"
                         f"Кнопки на странице: {btns[:700]}", shot)
            log("Ошибка", f"{label}: {status}")
            return

        # ---- реально на паре: проверяем микрофон/камеру, скрин, сообщение ----
        await page.wait_for_timeout(4000)
        muted = await mute_all(page)
        await page.wait_for_timeout(1000)
        shot_in = await take_shot(page, "joined.png")
        mute_text = ("🔇 Микрофон и камера выключены." if muted
                     else "⚠️ Не уверен, что микрофон/камера выключены, проверь скрин.")
        await report(
            f"✅ Зашёл на пару ({label}).\n"
            f"{mute_text}\n"
            f"🕒 Сижу до {end:%H:%M}.\n"
            f"{next_pair_text(end)}",
            shot_in,
        )
        log("Инфо", f"{label}: на паре, сижу до {end:%H:%M}")

        ticks = 0
        while datetime.now() < end:
            await asyncio.sleep(10)
            if page.is_closed():
                break
            ticks += 1
            if ticks % 6 == 0:          # раз в минуту: микрофон/камера должны быть выключены
                await mute_all(page)

        # ---- выходим: скрин + сообщение ----
        shot_out = None
        if not page.is_closed():
            shot_out = await take_shot(page, "left.png")
            await click_btn(page, LEAVE_PAT.pattern, 2000)
            await asyncio.sleep(1)
        await report(
            f"🏁 Вышел с пары ({label}).\n"
            f"{next_pair_text(end)}",
            shot_out,
        )
        log("Инфо", f"{label}: вышел")
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ---------------------- TELEGRAM ----------------------
def parse_message(text):
    """Возвращает (список пар, причина если список пуст).
    Пара = (название, ссылка, начало, конец). В одном сообщении может быть
    сколько угодно пар. Слова "завтра" / "послезавтра" / "сегодня" перед
    временем переносят пары на нужный день."""
    if not text:
        return [], "пустое сообщение"
    times = list(TIME_RE.finditer(text))
    links = list(LINK_RE.finditer(text))
    if not times:
        return [], "не нашёл время вида 09:59-10:03"
    if not links:
        return [], "не нашёл ссылку meet.google.com"

    days = []
    for m in DAY_RE.finditer(text):
        word = m.group(0).lower()
        days.append((m.start(), 2 if word == "послезавтра" else (1 if word == "завтра" else 0)))

    now = datetime.now()
    pairs = []
    for i, tm in enumerate(times):
        seg_end = times[i + 1].start() if i + 1 < len(times) else len(text)
        seg = text[tm.start():seg_end]
        lm = LINK_RE.search(seg)
        if lm:
            link = lm.group(0)
        else:
            before = [l for l in links if l.start() < tm.start()]
            link = (before[-1] if before else links[0]).group(0)

        offset = 0
        for pos, off in days:
            if pos < tm.start():
                offset = off

        h1, m1, h2, m2 = map(int, tm.groups())
        base = (now + timedelta(days=offset)).replace(second=0, microsecond=0)
        start = base.replace(hour=h1, minute=m1)
        end = base.replace(hour=h2, minute=m2)
        if end <= start:
            end += timedelta(days=1)
        if end <= now:
            continue

        nums = re.findall(r"(\d+)\s*пара", text[max(0, tm.start() - 30):tm.start()], re.I)
        label = (f"{nums[-1]} пара " if nums else "") + f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"
        pairs.append((label, link, start, end))

    if not pairs:
        return [], "эти пары уже закончились по времени на ноутбуке"
    return pairs, ""


@client.on(events.NewMessage())
async def on_message(event):
    try:
        chat = await event.get_chat()
        title = (getattr(chat, "title", "") or "").strip().lower()
        me = await client.get_me()
        if not (title == GROUP_NAME.lower() or event.chat_id == me.id):
            return
        text = event.raw_text or ""
        if text.startswith(("🔴", "✅", "🏁", "🕒", "⚠️", "⏳")):
            return

        pairs, reason = parse_message(text)
        if not pairs:
            # отвечаем только если в сообщении похоже на пару
            if TIME_RE.search(text) or LINK_RE.search(text):
                log("Инфо", f"Не добавил сообщение: {reason}")
                await report(f"⚠️ Не смог добавить пару: {reason}")
            return

        for label, link, start, end in pairs:
            key = (link, start)
            if key in planned:
                continue
            planned.add(key)
            schedule.append((label, start, end))
            log("Инфо", f"Нашёл пару: {label} {start:%d.%m} {link}")
            await report(f"🕒 Запланировал: {label} ({start:%d.%m})")
            asyncio.create_task(attend(label, link, start, end))
    except Exception as e:
        log("Ошибка", f"on_message: {e}")


async def main():
    global PW, ctx, report_chat
    log("Система", "Запускаю Телеграм...")
    await client.start()

    report_chat = "me"
    async for d in client.iter_dialogs():
        if (d.name or "").strip().lower() == GROUP_NAME.lower():
            report_chat = d.entity
            break

    async with async_playwright() as pw:
        PW = pw
        ctx = await launch_browser(pw, HEADLESS)
        log("Юзербот", f"Запущен (тихий режим). Жду ссылки на пары в группе «{GROUP_NAME}».")
        await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "login":
            asyncio.run(do_login())
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
