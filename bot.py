#!/usr/bin/env python3
import asyncio, json, logging, os, re
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import aiohttp, pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from icalendar import Calendar
from telegram import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                           ContextTypes, MessageHandler, filters)

BOT_TOKEN = os.environ["BOT_TOKEN"]
STORAGE   = Path(__file__).parent / "data" / "subscribers.json"
ICS_FILE  = Path(__file__).parent / "f1_2026.ics"
MSK       = pytz.timezone("Europe/Moscow")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
STORAGE.parent.mkdir(exist_ok=True)
http: Optional[aiohttp.ClientSession] = None
_app: Optional[Application] = None
scheduler = AsyncIOScheduler(timezone=pytz.utc)

# ── STORAGE ──────────────────────────────────────────────────────────────────
def _load():
    return json.loads(STORAGE.read_text("utf-8")) if STORAGE.exists() else {"users":[],"chats":[],"muted":[]}
def _save(d): STORAGE.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
def is_subscribed(cid): d=_load(); return cid in d["users"] or cid in d["chats"]
def is_muted(cid): return cid in _load().get("muted",[])
def toggle_notif(cid, priv):
    d=_load(); key="users" if priv else "chats"
    if cid not in d[key]: d[key].append(cid)
    muted=d.setdefault("muted",[])
    if cid in muted: muted.remove(cid); _save(d); return True
    muted.append(cid); _save(d); return False
def subscribe(cid, priv):
    d=_load(); key="users" if priv else "chats"
    if cid not in d[key]: d[key].append(cid); _save(d); return True
    return False
def active_subs():
    d=_load(); muted=set(d.get("muted",[])); return [c for c in d["users"]+d["chats"] if c not in muted]

async def broadcast(text: str, parse_mode: str = "HTML"):
    """Рассылка всем подписчикам с обработкой ошибок и автоочисткой мёртвых чатов."""
    if not _app:
        return
    dead = []
    migrated = []   # (old_id, new_id)
    for cid in active_subs():
        try:
            await _app.bot.send_message(cid, text, parse_mode=parse_mode)
        except Exception as e:
            err = str(e)
            if "ChatMigrated" in err or "chat_id_invalid" in err.lower():
                # Группа стала супергруппой — пробуем найти новый ID из ошибки
                import re as _re
                m = _re.search(r"migrate_to_chat_id.*?(-[0-9]+)", err)
                new_id = int(m.group(1)) if m else None
                if new_id:
                    migrated.append((cid, new_id))
                    try:
                        await _app.bot.send_message(new_id, text, parse_mode=parse_mode)
                        log.info("ChatMigrated: %s → %s, обновляем", cid, new_id)
                    except Exception as e2:
                        log.warning("После миграции %s: %s", new_id, e2)
                else:
                    dead.append(cid)
            elif any(x in err for x in ["Forbidden", "bot was kicked", "user is deactivated",
                                         "chat not found", "blocked"]):
                dead.append(cid)
                log.info("Мёртвый чат убран: %s (%s)", cid, err[:60])
            else:
                log.warning("Broadcast→%s: %s", cid, err)

    # Обновляем storage
    if dead or migrated:
        d = _load()
        for cid in dead:
            for key in ("users", "chats", "muted"):
                if cid in d.get(key, []): d[key].remove(cid)
        for old_id, new_id in migrated:
            for key in ("users", "chats"):
                if old_id in d.get(key, []):
                    d[key].remove(old_id)
                    if new_id not in d[key]: d[key].append(new_id)
            if old_id in d.get("muted", []):
                d["muted"].remove(old_id)
        _save(d)

# ── FLAGS ─────────────────────────────────────────────────────────────────────
_FL = {"саудовской аравии":"🇸🇦","великобритании":"🇬🇧","барселоны-каталонии":"🇪🇸",
       "сан-паулу":"🇧🇷","лас-вегаса":"🇺🇸","абу-даби":"🇦🇪","австралии":"🇦🇺",
       "австрии":"🇦🇹","азербайджана":"🇦🇿","барселоны":"🇪🇸","испании":"🇪🇸",
       "бахрейна":"🇧🇭","бельгии":"🇧🇪","венгрии":"🇭🇺","италии":"🇮🇹",
       "канады":"🇨🇦","катара":"🇶🇦","китая":"🇨🇳","майами":"🇺🇸","мехико":"🇲🇽",
       "монако":"🇲🇨","нидерландов":"🇳🇱","сша":"🇺🇸","сингапура":"🇸🇬","японии":"🇯🇵"}
def flag(name):
    low=name.lower()
    for k in sorted(_FL,key=len,reverse=True):
        if k in low: return _FL[k]
    return "🏁"

# ── LOCALE ────────────────────────────────────────────────────────────────────
MR={1:"Январь",2:"Февраль",3:"Март",4:"Апрель",5:"Май",6:"Июнь",
    7:"Июль",8:"Август",9:"Сентябрь",10:"Октябрь",11:"Ноябрь",12:"Декабрь"}
MG={1:"января",2:"февраля",3:"марта",4:"апреля",5:"мая",6:"июня",
    7:"июля",8:"августа",9:"сентября",10:"октября",11:"ноября",12:"декабря"}

# ── ICS PARSING ───────────────────────────────────────────────────────────────
SM={"1-я сессия свободных заездов":("🔧","П1"),"2-я сессия свободных заездов":("🔧","П2"),
    "3-я сессия свободных заездов":("🔧","П3"),"квалификация к спринту":("⚡","Кв.Спринта"),
    "спринт":("⚡","Спринт"),"квалификация":("🔥","Квалификация"),"гонка":("🏁","Гонка")}
def sess_meta(s):
    low=s.lower()
    for k,v in SM.items():
        if k in low: return v
    return "🏎️",s
def gp_base(s): return s.split(". ")[0] if ". " in s else s

def parse_ics():
    evs=[]
    with open(ICS_FILE,"rb") as f: cal=Calendar.from_ical(f.read())
    for c in cal.walk():
        if c.name!="VEVENT": continue
        summ=str(c.get("SUMMARY",""))
        loc=str(c.get("LOCATION","")).replace("\\,",",")
        st=c.get("DTSTART").dt
        if not isinstance(st,datetime): st=datetime(st.year,st.month,st.day,tzinfo=pytz.utc)
        elif st.tzinfo is None: st=pytz.utc.localize(st)
        evs.append({"summary":summ,"location":loc,"start_utc":st})
    evs.sort(key=lambda e:e["start_utc"]); return evs

def build_weekends():
    evs=parse_ics(); wm=OrderedDict()
    for ev in evs:
        base=gp_base(ev["summary"])
        if base not in wm:
            parts=ev["location"].split(","); city=parts[0].strip()
            country=parts[-1].strip() if len(parts)>1 else city
            wm[base]={"gp_name":base,"country":country,"city":city,"flag":flag(base),
                      "location":ev["location"],"sessions":[],"id":re.sub(r"[^a-z0-9]","_",base.lower())[:28]}
        e,s=sess_meta(ev["summary"])
        wm[base]["sessions"].append({"summary":ev["summary"],"short":s,"emoji":e,
                                      "start_utc":ev["start_utc"],"location":ev["location"]})
    wknds=list(wm.values())
    for w in wknds: w["start_utc"]=w["sessions"][0]["start_utc"]; w["end_utc"]=w["sessions"][-1]["start_utc"]
    wknds.sort(key=lambda w:w["start_utc"]); return wknds

def _weekend_end_msk(w) -> datetime:
    """Уикенд считается «текущим» до понедельника 06:00 МСК после гонки."""
    race_start = w["end_utc"]   # end_utc = старт последней сессии (гонки)
    race_end   = race_start + timedelta(hours=4)   # гонка ~2ч + запас
    # Переводим в МСК, находим ближайший пн 06:00
    msk_end = race_end.astimezone(MSK)
    days_to_mon = (7 - msk_end.weekday()) % 7   # 0 если уже пн
    if days_to_mon == 0 and msk_end.hour >= 6:
        days_to_mon = 7
    mon_0600 = msk_end.replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=days_to_mon)
    return mon_0600.astimezone(pytz.utc)

def cur_weekend(wks):
    now = datetime.now(tz=pytz.utc)
    for w in wks:
        window_start = w["start_utc"] - timedelta(hours=6)
        window_end   = _weekend_end_msk(w)
        if window_start <= now <= window_end:
            return w
    return None
def nxt_weekend(wks):
    now=datetime.now(tz=pytz.utc)
    for w in wks:
        if w["start_utc"]>now: return w
    return None
def nxt_session(wks):
    now=datetime.now(tz=pytz.utc)
    for w in wks:
        for s in w["sessions"]:
            if s["start_utc"]>now:
                return {**s,"gp_name":w["gp_name"],"flag":w["flag"],"country":w["country"],"city":w["city"]}
    return None

# ── WEATHER ───────────────────────────────────────────────────────────────────
# Русские названия городов → английские для wttr.in
_CITY_EN = {
    "мельбурн":"Melbourne","шанхай":"Shanghai","сузука":"Suzuka",
    "сахир":"Sakhir","джидда":"Jeddah","майами":"Miami",
    "монте-карло":"Monte Carlo","монако":"Monaco","монреаль":"Montreal",
    "барселона":"Barcelona","шпильберг":"Spielberg","сильверстоун":"Silverstone",
    "будапешт":"Budapest","спа":"Spa","зандворт":"Zandvoort","монца":"Monza",
    "баку":"Baku","сингапур":"Singapore","остин":"Austin",
    "мехико":"Mexico City","сан-паулу":"Sao Paulo","лас-вегас":"Las Vegas",
    "лусаил":"Lusail","абу-даби":"Abu Dhabi",
}
def _city_en(city):
    low=city.lower()
    for k,v in _CITY_EN.items():
        if k in low: return v
    return city

_WX={"Sunny":"☀️ Солнечно","Clear":"☀️ Ясно","Partly cloudy":"⛅ Переменная облачность",
     "Cloudy":"☁️ Облачно","Overcast":"☁️ Пасмурно","Mist":"🌫 Туман","Fog":"🌫 Туман",
     "Light rain":"🌦 Лёгкий дождь","Moderate rain":"🌧 Дождь","Heavy rain":"🌧 Сильный дождь",
     "Patchy rain possible":"🌦 Возможен дождь","Light drizzle":"🌦 Морось",
     "Thundery outbreaks":"⛈ Гроза","Light snow":"🌨 Лёгкий снег","Heavy snow":"🌨 Снег"}
def _wx(en):
    for k,v in _WX.items():
        if k.lower() in en.lower(): return v
    return en

async def get_weather(city, target_dt: datetime = None):
    """
    Получает текущую погоду + прогноз на target_dt (если передан).
    Пробует до 3 раз с паузой — wttr.in бывает нестабилен.
    Резервный источник: open-meteo.com (работает всегда, без ключа).
    """
    city_q = _city_en(city)

    # ── Попытка 1-3: wttr.in ─────────────────────────────────────────────────
    data = None
    for attempt in range(3):
        try:
            url = f"https://wttr.in/{city_q}?format=j1"
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=12),
                                headers={"User-Agent": "F1Bot/1.0"}) as r:
                text = await r.text()
                # wttr.in иногда отдаёт HTML вместо JSON при перегрузке
                if text.strip().startswith("{"):
                    import json as _json
                    data = _json.loads(text)
                    break
                else:
                    log.warning("wttr.in вернул не-JSON (попытка %d): %s...", attempt+1, text[:80])
        except Exception as e:
            log.warning("wttr.in попытка %d для '%s': %s", attempt+1, city_q, e)
        if attempt < 2:
            await asyncio.sleep(2)

    if data:
        try:
            cur = data["current_condition"][0]
            cur_rain = max(int(h.get("chanceofrain", 0))
                           for day in data["weather"][:1]
                           for h in day["hourly"][:3])
            cur_w = {
                "temp":  cur["temp_C"],
                "feels": cur["FeelsLikeC"],
                "desc":  _wx(cur["weatherDesc"][0]["value"]),
                "hum":   cur["humidity"],
                "wind":  cur["windspeedKmph"],
                "rain":  cur_rain,
            }

            fc_w = None
            if target_dt is not None:
                now_utc    = datetime.now(tz=pytz.utc)
                delta_days = (target_dt.astimezone(pytz.utc) - now_utc).total_seconds() / 86400
                if 0 < delta_days <= 5:
                    target_date = target_dt.astimezone(pytz.utc).strftime("%Y-%m-%d")
                    target_hour = target_dt.astimezone(pytz.utc).hour
                    best_h, best_diff = None, 99
                    for day in data["weather"]:
                        if day.get("date", "") != target_date:
                            continue
                        for h in day["hourly"]:
                            diff = abs(int(h["time"]) // 100 - target_hour)
                            if diff < best_diff:
                                best_diff, best_h = diff, h
                    if best_h:
                        fc_w = {
                            "temp":  best_h["tempC"],
                            "feels": best_h["FeelsLikeC"],
                            "desc":  _wx(best_h["weatherDesc"][0]["value"]),
                            "hum":   best_h["humidity"],
                            "wind":  best_h["windspeedKmph"],
                            "rain":  int(best_h.get("chanceofrain", 0)),
                        }

            return {"current": cur_w, "forecast": fc_w}
        except Exception as e:
            log.warning("Парсинг wttr.in для '%s': %s", city_q, e)

    # ── Резерв: Open-Meteo (геокодинг + погода, без ключа) ───────────────────
    log.info("Переключаемся на open-meteo для '%s'", city_q)
    try:
        # Шаг 1: получаем координаты через open-meteo geocoding
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city_q}&count=1&language=en"
        async with http.get(geo_url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            geo = await r.json(content_type=None)
        if not geo.get("results"):
            raise ValueError(f"Город не найден: {city_q}")
        loc  = geo["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]

        # Шаг 2: текущая погода + почасовой прогноз
        wx_url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,apparent_temperature,weathercode,windspeed_10m,relativehumidity_2m"
            f"&hourly=temperature_2m,apparent_temperature,weathercode,windspeed_10m,relativehumidity_2m,precipitation_probability"
            f"&forecast_days=5&timezone=UTC"
        )
        async with http.get(wx_url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            wx = await r.json(content_type=None)

        def _wmo(code):
            # WMO weather code → описание
            WMO = {0:"☀️ Ясно",1:"🌤 Преимущественно ясно",2:"⛅ Переменная облачность",
                   3:"☁️ Пасмурно",45:"🌫 Туман",48:"🌫 Иней",51:"🌦 Лёгкая морось",
                   53:"🌦 Морось",55:"🌧 Сильная морось",61:"🌦 Лёгкий дождь",
                   63:"🌧 Дождь",65:"🌧 Сильный дождь",71:"🌨 Лёгкий снег",
                   73:"🌨 Снег",75:"❄️ Сильный снег",80:"🌦 Ливень",
                   81:"🌧 Сильный ливень",95:"⛈ Гроза",96:"⛈ Гроза с градом"}
            return WMO.get(int(code), f"Код {code}")

        cur_c = wx["current"]
        cur_w = {
            "temp":  str(round(cur_c["temperature_2m"])),
            "feels": str(round(cur_c["apparent_temperature"])),
            "desc":  _wmo(cur_c["weathercode"]),
            "hum":   str(round(cur_c["relativehumidity_2m"])),
            "wind":  str(round(cur_c["windspeed_10m"])),
            "rain":  0,
        }

        fc_w = None
        if target_dt is not None:
            now_utc    = datetime.now(tz=pytz.utc)
            delta_days = (target_dt.astimezone(pytz.utc) - now_utc).total_seconds() / 86400
            if 0 < delta_days <= 5:
                target_iso = target_dt.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:00")
                times = wx["hourly"]["time"]
                if target_iso in times:
                    idx = times.index(target_iso)
                else:
                    # ближайший час
                    from datetime import datetime as _dt
                    target_ts = target_dt.astimezone(pytz.utc).replace(minute=0, second=0, microsecond=0)
                    idx = min(range(len(times)),
                              key=lambda i: abs((_dt.fromisoformat(times[i]).replace(tzinfo=pytz.utc) - target_ts).total_seconds()))
                h = wx["hourly"]
                fc_w = {
                    "temp":  str(round(h["temperature_2m"][idx])),
                    "feels": str(round(h["apparent_temperature"][idx])),
                    "desc":  _wmo(h["weathercode"][idx]),
                    "hum":   str(round(h["relativehumidity_2m"][idx])),
                    "wind":  str(round(h["windspeed_10m"][idx])),
                    "rain":  int(h["precipitation_probability"][idx] or 0),
                }

        log.info("open-meteo успешно для '%s'", city_q)
        return {"current": cur_w, "forecast": fc_w}

    except Exception as e:
        log.error("open-meteo для '%s': %s", city_q, e)
        return {"current": {}, "forecast": None}


def fmt_weather_block(wdata: dict, target_dt: datetime = None) -> str:
    """Показывает прогноз на время сессии; если недоступен — текущую погоду."""
    cur = wdata.get("current", {})
    fc  = wdata.get("forecast")

    def _fmt(w, label):
        if not w:
            return f"{label}: данные недоступны"
        rain = f"☔ {w['rain']}%" if int(w["rain"]) > 5 else "☀️ без осадков"
        return (label + "\n"
                + f"{w['desc']}\n"
                + f"🌡 <b>{w['temp']}°C</b> (ощущается {w['feels']}°C)\n"
                + f"💧 {w['hum']}% · 💨 {w['wind']} км/ч · {rain}")

    # Если есть прогноз на время сессии — показываем только его
    if fc and target_dt:
        msk_t = target_dt.astimezone(MSK).strftime("%H:%M МСК")
        return _fmt(fc, f"🔮 <b>Прогноз погоды на {msk_t}:</b>")

    # Прогноз недоступен (> 5 дней) — показываем текущую
    return _fmt(cur, "🌡 <b>Погода сейчас:</b>")


# Обратная совместимость — старый fmt_weather для live-монитора
def fmt_weather(w):
    if isinstance(w, dict) and "current" in w:
        w = w.get("current", {})
    if not w: return "🌡 Погода временно недоступна"
    rain = f"☔ {w['rain']}%" if int(w["rain"]) > 5 else "☀️ Без осадков"
    return f"🌡 <b>{w['temp']}°C</b> (ощущается {w['feels']}°C) · {w['desc']}\n💧 {w['hum']}% · 💨 {w['wind']} км/ч · {rain}"

# ── JOLPICA API ───────────────────────────────────────────────────────────────
J="https://api.jolpi.ca/ergast/f1"
async def jget(path):
    try:
        async with http.get(f"{J}{path}",timeout=aiohttp.ClientTimeout(total=8)) as r:
            return await r.json(content_type=None)
    except: return None

async def driver_standings():
    d=await jget("/current/driverStandings.json")
    if not d: return []
    lst=d["MRData"]["StandingsTable"]["StandingsLists"]
    return lst[0]["DriverStandings"] if lst else []

async def last_quali():
    d=await jget("/current/last/qualifying.json")
    if not d: return "",[]
    races=d["MRData"]["RaceTable"]["Races"]
    if not races: return "",[]
    return races[0].get("raceName",""),races[0].get("QualifyingResults",[])

async def last_race():
    d=await jget("/current/last/results.json")
    if not d: return "",[]
    races=d["MRData"]["RaceTable"]["Races"]
    if not races: return "",[]
    return races[0].get("raceName",""),races[0].get("Results",[])

async def race_by_round(season: int, rnd: int):
    d=await jget(f"/{season}/{rnd}/results.json")
    if not d: return "",[]
    races=d["MRData"]["RaceTable"]["Races"]
    if not races: return "",[]
    return races[0].get("raceName",""),races[0].get("Results",[])

async def quali_by_round(season: int, rnd: int):
    d=await jget(f"/{season}/{rnd}/qualifying.json")
    if not d: return "",[]
    races=d["MRData"]["RaceTable"]["Races"]
    if not races: return "",[]
    return races[0].get("raceName",""),races[0].get("QualifyingResults",[])

# ── API LAYER: OpenF1 + F1LiveTiming (параллельно) ───────────────────────────
O  = "https://api.openf1.org/v1"
MV = "https://api.multiviewer.app/api/v1"

_TIMEOUT_FAST = aiohttp.ClientTimeout(total=5)
_TIMEOUT_SLOW = aiohttp.ClientTimeout(total=10)

async def _get_json(url: str, timeout=None) -> list | dict | None:
    """Быстрый GET с таймаутом, возвращает None при ошибке."""
    try:
        async with http.get(url, timeout=timeout or _TIMEOUT_FAST) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception as e:
        log.debug("GET %s: %s", url, e)
    return None

async def oget(path) -> list:
    """OpenF1 GET → всегда список."""
    result = await _get_json(f"{O}{path}")
    if isinstance(result, list): return result
    return []

async def mvget(path) -> dict | None:
    """Multiviewer GET → dict или None."""
    return await _get_json(f"{MV}{path}")

# ── Параллельный fetch с race-condition: берём первый непустой ответ ──────────
async def _parallel_first(*coros):
    """Запускает корутины параллельно, возвращает первый непустой результат."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    result = None
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            val = task.result()
            if val:
                result = val
                break
        # если первый пустой — ждём остальных
        if not result and pending:
            done2, _ = await asyncio.wait(pending, timeout=4)
            for task in done2:
                val = task.result()
                if val:
                    result = val
                    break
    finally:
        for t in tasks:
            if not t.done(): t.cancel()
    return result

async def f1_latest_sess():
    d = await oget("/sessions?session_key=latest")
    return d[0] if d else None

async def f1_drivers(sk: int) -> dict:
    d = await oget(f"/drivers?session_key={sk}")
    return {x["driver_number"]: x for x in d}

# ── Race Control: OpenF1 основной, Multiviewer запасной ──────────────────────
async def _rc_openf1(sk: int) -> list:
    return await oget(f"/race_control?session_key={sk}")

async def _rc_multiviewer(year: int, session_path: str) -> list:
    """
    Multiviewer хранит Race Control в SessionInfo.
    Возвращает список событий в формате совместимом с OpenF1.
    """
    data = await mvget(f"/session/{year}/{session_path}/RaceControlMessages")
    if not data or not isinstance(data, dict):
        return []
    messages = data.get("Messages", {})
    result = []
    for key, m in messages.items():
        if not isinstance(m, dict):
            continue
        result.append({
            "date":    m.get("Utc", ""),
            "message": m.get("Message", ""),
            "flag":    m.get("Flag", ""),
            "category":m.get("Category", ""),
            "scope":   m.get("Scope", ""),
        })
    return result

async def f1_rc(sk: int, mv_path: str = None, year: int = 2026) -> list:
    """Параллельно тянет Race Control из OpenF1 и Multiviewer, мёржит."""
    coros = [_rc_openf1(sk)]
    if mv_path:
        coros.append(_rc_multiviewer(year, mv_path))
    results = await asyncio.gather(*coros, return_exceptions=True)

    merged, seen_uid = [], set()
    for batch in results:
        if isinstance(batch, Exception) or not batch:
            continue
        for m in batch:
            uid = f"{m.get('date','')}{m.get('message','')}"
            if uid not in seen_uid:
                seen_uid.add(uid)
                merged.append(m)
    return sorted(merged, key=lambda x: x.get("date", ""))

async def f1_pit(sk: int) -> list:
    return await oget(f"/pit?session_key={sk}")

async def f1_laps(sk: int) -> list:
    """Только круги с реальным временем (60–200 сек)."""
    laps = await oget(f"/laps?session_key={sk}")
    return [l for l in laps if l.get("lap_duration") and 60 < l["lap_duration"] < 200]

async def f1_positions(sk: int) -> list:
    return await oget(f"/position?session_key={sk}")

# ── DRIVER COUNTRY FLAGS ─────────────────────────────────────────────────────
_DRIVER_FLAG = {
    # 2026 grid + recent drivers
    "Verstappen":"🇳🇱","Hamilton":"🇬🇧","Leclerc":"🇲🇨","Norris":"🇬🇧",
    "Piastri":"🇦🇺","Russell":"🇬🇧","Sainz":"🇪🇸","Alonso":"🇪🇸",
    "Perez":"🇲🇽","Stroll":"🇨🇦","Gasly":"🇫🇷","Ocon":"🇫🇷",
    "Bottas":"🇫🇮","Zhou":"🇨🇳","Albon":"🇹🇭","Sargeant":"🇺🇸",
    "Hulkenberg":"🇩🇪","Magnussen":"🇩🇰","Tsunoda":"🇯🇵","De Vries":"🇳🇱",
    "Lawson":"🇳🇿","Bearman":"🇬🇧","Colapinto":"🇦🇷","Doohan":"🇦🇺",
    "Antonelli":"🇮🇹","Hadjar":"🇫🇷","Bortoleto":"🇧🇷","Iwasa":"🇯🇵",
}
def driver_emoji(last_name: str) -> str:
    return _DRIVER_FLAG.get(last_name, "🏁")


# ── FORMATTERS ────────────────────────────────────────────────────────────────
def _msk(dt): return dt.astimezone(MSK)
def dtstr(dt): m=_msk(dt); return f"{m.day} {MG[m.month]}, {m.strftime('%H:%M')} МСК"

def fmt_card(w, show_done=True):
    """
    Карточка уикенда.
    show_done=True  — пройденные сессии помечаются ✓ и зачёркиваются.
    """
    now  = datetime.now(tz=pytz.utc)
    s    = _msk(w["start_utc"])
    e    = _msk(w["end_utc"])
    dates = (f"{s.day}–{e.day} {MG[s.month]}"
             if s.month == e.month
             else f"{s.day} {MG[s.month]} – {e.day} {MG[e.month]}")

    lines = [
        f"{w['flag']}  <b>{w['gp_name'].upper()}</b>",
        f"<i>📍 {w['country']}, {w['city']}   ·   {dates}</i>",
        "",
    ]

    for ss in w["sessions"]:
        dt      = _msk(ss["start_utc"])
        done    = show_done and ss["start_utc"] < now
        # Формат: две строки — заголовок, затем дата·время
        day_str  = f"{dt.day:02d} {MG[dt.month]}"
        time_str = dt.strftime("%H:%M")

        if done:
            # Зачёркнутый через unicode + серый индикатор
            name_s  = "".join(c + "̶" for c in ss["short"])
            lines.append(f"  ✓  <s>{ss['emoji']} {ss['short']}</s>")
            lines.append(f"      <s>📅 {day_str}  ·  🕐 {time_str} МСК</s>")
        else:
            lines.append(f"  {ss['emoji']}  <b>{ss['short']}</b>")
            lines.append(f"      📅 {day_str}  ·  🕐 {time_str} МСК")
        lines.append("")   # пустая строка между сессиями

    # убираем последнюю лишнюю пустую строку
    if lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)

def fmt_month(wks,year,month):
    now   = datetime.now(tz=pytz.utc)
    mwks  = [w for w in wks if _msk(w["start_utc"]).year==year and _msk(w["start_utc"]).month==month]
    hdr   = f"🗓 <b>{MR[month]} {year}</b>  —  {len(mwks)} уикенда\n"
    sep   = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    cards = sep.join(fmt_card(w, show_done=True) for w in mwks)
    return (hdr + cards)[:4090]

async def fmt_cur_weekend(w):
    lines=["🏁 <b>Текущий / ближайший гоночный уикенд</b>\n",fmt_card(w, show_done=True)]
    stds=await driver_standings()
    if stds:
        lines.append("\n\n🏆 <b>Чемпионат гонщиков 2026</b>")
        for s in stds[:10]:
            d=s["Driver"]; name=f"{d['givenName'][0]}. {d['familyName']}"
            team=(s.get("Constructors") or [{}])[0].get("name","—")
            lines.append(f"  {s['position']}. <b>{name}</b> ({team}) · {s['points']} очк.")
    return "\n".join(lines)

TZ_MAP={"мельбурн":"Australia/Melbourne","шанхай":"Asia/Shanghai","сузука":"Asia/Tokyo",
        "сахир":"Asia/Bahrain","джидда":"Asia/Riyadh","майами":"America/New_York",
        "монте-карло":"Europe/Monaco","монако":"Europe/Monaco","монреаль":"America/Toronto",
        "барселона":"Europe/Madrid","шпильберг":"Europe/Vienna","сильверстоун":"Europe/London",
        "будапешт":"Europe/Budapest","спа":"Europe/Brussels","зандворт":"Europe/Amsterdam",
        "монца":"Europe/Rome","баку":"Asia/Baku","сингапур":"Asia/Singapore",
        "остин":"America/Chicago","мехико":"America/Mexico_City","сан-паулу":"America/Sao_Paulo",
        "лас-вегас":"America/Los_Angeles","лусаил":"Asia/Qatar","абу-даби":"Asia/Dubai"}
def local_tz(city):
    low=city.lower()
    for k,tz in TZ_MAP.items():
        if k in low: return pytz.timezone(tz)
    return MSK

async def fmt_next_sess(sess):
    city    = sess["city"]
    msk_dt  = _msk(sess["start_utc"])
    loc     = sess["start_utc"].astimezone(local_tz(city))
    is_race = "гонка" in sess["summary"].lower()

    lines = [
        f"{sess['flag']} <b>Ближайшее событие</b>",
        "",
        f"{sess['emoji']} <b>{sess['summary']}</b>",
        f"📍 {sess['country']}, {city}",
        f"🕐 <b>{msk_dt.strftime('%H:%M')} МСК</b>  ·  {loc.strftime('%H:%M')} местного",
        f"📅 {msk_dt.day} {MG[msk_dt.month]}",
        "",
    ]
    wdata = await get_weather(city, target_dt=sess["start_utc"])
    lines.append(fmt_weather_block(wdata, target_dt=sess["start_utc"]))

    if is_race:
        _, qr = await last_quali()
        if qr:
            lines.append("\n🏁 <b>Стартовая решётка:</b>")
            for q in qr:   # все гонщики, не только топ-10
                d      = q["Driver"]
                last   = d["familyName"]
                demoji = driver_emoji(last)
                team   = q["Constructor"]["name"]
                best   = q.get("Q3") or q.get("Q2") or q.get("Q1") or "—"
                lines.append(
                    f"  <b>P{q['position']}</b>  {demoji} {d['givenName'][0]}. {last}"
                    f"  ({team})  <code>{best}</code>"
                )

    return "\n".join(lines)

async def fmt_standings():
    stds=await driver_standings()
    if not stds: return "❌ Данные чемпионата ещё недоступны — сезон не начался"
    lines=["🏆 <b>Чемпионат гонщиков 2026</b>\n"]
    for s in stds[:20]:
        d=s["Driver"]; team=(s.get("Constructors") or [{}])[0].get("name","—")
        lines.append(f"{int(s['position']):>2}. <b>{d['givenName'][0]}. {d['familyName']}</b> ({team}) — {s['points']} очк.")
    return "\n".join(lines)

async def fmt_quali():
    rname,results=await last_quali()
    if not results: return "❌ Результаты квалификации недоступны — сезон ещё не начался"
    lines=[f"🔥 <b>Квалификация — {rname}</b>\n"]
    q3=[r for r in results if r.get("Q3")]
    q2=[r for r in results if r.get("Q2") and not r.get("Q3")]
    q1=[r for r in results if not r.get("Q2")]
    if q3:
        lines.append("✅ <b>Q3 — проходят в топ-10:</b>")
        for r in q3:
            d=r["Driver"]
            lines.append(f"  {r['position']}. <b>{d['givenName'][0]}. {d['familyName']}</b> ({r['Constructor']['name']})  {r['Q3']}")
    if q2:
        lines.append("\n⚠️ <b>Выбыли в Q2:</b>")
        for r in q2: d=r["Driver"]; lines.append(f"  {r['position']}. {d['givenName'][0]}. {d['familyName']}  {r['Q2']}")
    if q1:
        lines.append("\n❌ <b>Выбыли в Q1:</b>")
        for r in q1: d=r["Driver"]; lines.append(f"  {r['position']}. {d['givenName'][0]}. {d['familyName']}  {r.get('Q1','—')}")
    for r in results:
        if r["Driver"]["familyName"]=="Leclerc":
            lines.append(f"\n\n🔴 <b>Шарль Леклер — Scuderia Ferrari</b>")
            lines.append(f"  Позиция: P{r['position']}")
            lines.append(f"  Q1: {r.get('Q1','—')} · Q2: {r.get('Q2','—')} · Q3: {r.get('Q3','—')}")
    return "\n".join(lines)

async def fmt_race():
    rname,results=await last_race()
    if not results: return "❌ Результаты гонки недоступны — сезон ещё не начался"
    stds=await driver_standings(); pm={s["Driver"]["driverId"]:s["points"] for s in stds}
    lines=[f"🏁 <b>Гонка — {rname}</b>\n"]; medals=["🥇","🥈","🥉"]
    lines.append("<b>Подиум:</b>")
    for r in results[:3]:
        d=r["Driver"]
        lines.append(f"  {medals[int(r['position'])-1]} <b>{d['givenName']} {d['familyName']}</b> ({r['Constructor']['name']}) +{r.get('points','0')} очк.")
    lines.append("\n<b>Итоговая таблица (топ-10):</b>")
    for r in results[:10]:
        d=r["Driver"]; total=pm.get(d["driverId"],"—")
        lines.append(f"  {r['position']}. <b>{d['givenName'][0]}. {d['familyName']}</b> ({r['Constructor']['name']}) +{r.get('points','0')} → {total} очк.")
    wks=build_weekends(); now=datetime.now(tz=pytz.utc)
    nxt=next((w for w in wks if w["end_utc"]>now),None)
    if nxt:
        rs=next((s for s in nxt["sessions"] if "гонка" in s["summary"].lower()),None)
        if rs: lines.append(f"\n📍 Следующая гонка: {nxt['flag']} <b>{nxt['gp_name']}</b>"); lines.append(f"  🕐 {dtstr(rs['start_utc'])}")
    for r in results:
        if r["Driver"]["familyName"]=="Leclerc":
            d=r["Driver"]; total=pm.get(d["driverId"],"—")
            lines.append(f"\n\n🔴 <b>Шарль Леклер — Scuderia Ferrari</b>")
            lines.append(f"  Финиш: P{r['position']}")
            lines.append(f"  Очки гонки: +{r.get('points','0')}")
            lines.append(f"  Итого в сезоне: {total} очк.")
            st=r.get("status","")
            if st and st!="Finished": lines.append(f"  ⚠️ Статус: {st}")
            if r.get("FastestLap"):
                fl=r["FastestLap"]; lines.append(f"  ⚡ Быстрый круг: {fl['Time']['time']} (круг {fl['lap']})")
    return "\n".join(lines)

async def fmt_cur_weekend_with_results(w: dict) -> tuple:
    """Карточка текущего уикенда + клавиатура с результатами."""
    lines = ["🏁 <b>Текущий / ближайший гоночный уикенд</b>\n", fmt_card(w, show_done=True)]
    stds  = await driver_standings()
    if stds:
        lines.append("\n\n🏆 <b>Чемпионат гонщиков 2026</b>")
        for s in stds[:10]:
            d    = s["Driver"]
            name = f"{d['givenName'][0]}. {d['familyName']}"
            team = (s.get("Constructors") or [{}])[0].get("name", "—")
            lines.append(f"  {s['position']}. <b>{name}</b> ({team}) · {s['points']} очк.")
    return "\n".join(lines)

async def fmt_past_weekend(w: dict, rnd: int) -> str:
    """Результаты прошедшего уикенда: гонка + чемпионат."""
    rname, results = await race_by_round(2026, rnd)
    stds           = await driver_standings()
    pts_map        = {s["Driver"]["driverId"]: s["points"] for s in stds}

    lines = [
        f"{w['flag']} <b>{w['gp_name'].upper()}</b>",
        f"<i>📍 {w['country']}, {w['city']}</i>",
        "",
    ]

    if results:
        medals = ["🥇","🥈","🥉"]
        lines.append("<b>Подиум:</b>")
        for r in results[:3]:
            d    = r["Driver"]
            name = f"{d['givenName']} {d['familyName']}"
            demoji = driver_emoji(d["familyName"])
            lines.append(
                f"  {medals[int(r['position'])-1]} {demoji} <b>{name}</b>"
                f" ({r['Constructor']['name']}) +{r.get('points','0')} очк."
            )
        lines.append("")
        lines.append("<b>Топ-10:</b>")
        for r in results[:10]:
            d     = r["Driver"]
            total = pts_map.get(d["driverId"], "—")
            demoji = driver_emoji(d["familyName"])
            lines.append(
                f"  {r['position']}. {demoji} {d['givenName'][0]}. {d['familyName']}"
                f" ({r['Constructor']['name']}) +{r.get('points','0')} → {total} очк."
            )
    else:
        lines.append("📭 Результаты гонки ещё не опубликованы")

    if stds:
        lines.append("\n🏆 <b>Чемпионат после этапа:</b>")
        for s in stds[:5]:
            d    = s["Driver"]
            name = f"{d['givenName'][0]}. {d['familyName']}"
            demoji = driver_emoji(d["familyName"])
            lines.append(f"  {s['position']}. {demoji} <b>{name}</b> — {s['points']} очк.")

    return "\n".join(lines)

async def fmt_past_quali(w: dict, rnd: int) -> str:
    """Результаты квалификации прошедшего уикенда."""
    rname, results = await quali_by_round(2026, rnd)
    if not results:
        return "📭 Результаты квалификации ещё не опубликованы"

    lines = [
        f"{w['flag']} <b>{w['gp_name']} — Квалификация</b>",
        "",
    ]
    q3 = [r for r in results if r.get("Q3")]
    q2 = [r for r in results if r.get("Q2") and not r.get("Q3")]
    q1 = [r for r in results if not r.get("Q2")]

    if q3:
        lines.append("✅ <b>Q3:</b>")
        for r in q3:
            d = r["Driver"]
            demoji = driver_emoji(d["familyName"])
            lines.append(f"  {r['position']}. {demoji} <b>{d['givenName'][0]}. {d['familyName']}</b>"
                         f" ({r['Constructor']['name']})  <code>{r['Q3']}</code>")
    if q2:
        lines.append("\n⚠️ <b>Выбыли в Q2:</b>")
        for r in q2:
            d = r["Driver"]
            demoji = driver_emoji(d["familyName"])
            lines.append(f"  {r['position']}. {demoji} {d['givenName'][0]}. {d['familyName']}  <code>{r['Q2']}</code>")
    if q1:
        lines.append("\n❌ <b>Выбыли в Q1:</b>")
        for r in q1:
            d = r["Driver"]
            demoji = driver_emoji(d["familyName"])
            lines.append(f"  {r['position']}. {demoji} {d['givenName'][0]}. {d['familyName']}  <code>{r.get('Q1','—')}</code>")
    return "\n".join(lines)

# ── LIVE MONITOR ──────────────────────────────────────────────────────────────
class LiveMonitor:
    POLL_INTERVAL = 7   # сек между опросами

    def __init__(self, sk: int, gp: str, city: str, sess_type: str = "race",
                 mv_path: str = None, year: int = 2026):
        self.key       = sk
        self.gp        = gp
        self.city      = city
        self.sess_type = sess_type
        self.mv_path   = mv_path   # путь для Multiviewer, например "1/Q"
        self.year      = year
        self.running   = False
        self.drivers:      dict  = {}
        self.seen_rc:      set   = set()
        self.seen_pit:     set   = set()
        self.best_lap:     Optional[float] = None
        self.form_sent:    bool  = False
        self.start_sent:   bool  = False
        self.finish_sent:  bool  = False
        self.podium_sent:  set   = set()
        self._post_race_task = None

    # ── запуск ───────────────────────────────────────────────────────────────
    async def run(self):
        self.running = True
        log.info("🔴 LiveMonitor start sk=%s  %s  type=%s", self.key, self.gp, self.sess_type)

        # Параллельно грузим гонщиков + историю RC + питы + круги
        (self.drivers,
         history_rc,
         hist_pits,
         hist_laps) = await asyncio.gather(
            f1_drivers(self.key),
            f1_rc(self.key, self.mv_path, self.year),
            f1_pit(self.key),
            f1_laps(self.key),
            return_exceptions=True
        )
        if isinstance(self.drivers, Exception): self.drivers = {}
        if isinstance(history_rc,   Exception): history_rc   = []
        if isinstance(hist_pits,    Exception): hist_pits    = []
        if isinstance(hist_laps,    Exception): hist_laps    = []

        # Глотаем историю — не отправляем
        for m in history_rc:
            self.seen_rc.add(f"{m.get('date','')}{m.get('message','')}")
        for p in hist_pits:
            self.seen_pit.add(f"{p.get('driver_number')}_{p.get('lap_number')}_{p.get('pit_duration')}")
        valid_laps = [l for l in hist_laps if l.get("lap_duration","") and l["lap_duration"] > 0]
        if valid_laps:
            self.best_lap = min(l["lap_duration"] for l in valid_laps)

        log.info("История: %d RC, %d pit, best=%.3fs, %d гонщиков",
                 len(self.seen_rc), len(self.seen_pit),
                 self.best_lap or 0, len(self.drivers))

        while self.running:
            try:
                await self._poll()
            except Exception as e:
                log.error("LiveMonitor poll: %s", e)
            await asyncio.sleep(self.POLL_INTERVAL)

    def stop(self):
        self.running = False
        log.info("LiveMonitor stopped — %s", self.gp)

    def _name(self, n) -> str:
        d = self.drivers.get(n, {})
        return d.get("full_name") or d.get("broadcast_name") or f"#{n}"
    def _team(self, n) -> str:
        return self.drivers.get(n, {}).get("team_name") or ""
    def _flag(self, n) -> str:
        full = self._name(n)
        last = full.split()[-1] if full else ""
        return driver_emoji(last)

    async def _bcast(self, text: str):
        await broadcast(text)

    async def _poll(self):
        """Параллельный опрос всех источников."""
        if self.sess_type == "race":
            await asyncio.gather(
                self._rc(),
                self._pitstop(),
                self._fl(),
                self._positions(),
                return_exceptions=True
            )
        elif self.sess_type == "quali":
            await asyncio.gather(
                self._rc(),
                self._fl(),
                return_exceptions=True
            )
        else:  # practice
            await self._rc()

    # ── Race Control ─────────────────────────────────────────────────────────
    async def _rc(self):
        msgs = await f1_rc(self.key, self.mv_path, self.year)
        for m in msgs:
            uid = f"{m.get('date','')}{m.get('message','')}"
            if uid in self.seen_rc:
                continue
            self.seen_rc.add(uid)
            await self._handle_rc(m)

    async def _handle_rc(self, m: dict):
        text = m.get("message", "")
        flag = (m.get("flag") or "").upper()
        cat  = (m.get("category") or "").upper()
        up   = text.upper()

        if ("FORMATION LAP" in up or "FORMATION" in up) and not self.form_sent:
            self.form_sent = True
            label = "Прогревочный круг" if self.sess_type == "race" else "Сессия начинается"
            await self._bcast(f"🏎️ <b>{label}!</b>\n\n<b>{self.gp}</b>")

        elif (flag == "GREEN" or "GREEN LIGHT" in up or "RACE START" in up)                 and not self.start_sent and self.sess_type == "race":
            self.start_sent = True
            w = await get_weather(self.city)
            await self._bcast(f"🚦 <b>ГОНКА СТАРТОВАЛА!</b>\n\n<b>{self.gp}</b>\n\n{fmt_weather(w)}")

        elif "SAFETY CAR" in up and "VIRTUAL" not in up and "MEDICAL" not in up:
            if "DEPLOYED" in up or flag == "SC":
                await self._bcast(f"🚗 <b>Safety Car!</b>\n{text}")
            elif "WITHDRAWN" in up or "IN THIS LAP" in up:
                await self._bcast("🚗 <b>Safety Car возвращается в боксы</b>")

        elif flag == "VSC" or "VIRTUAL SAFETY CAR" in up:
            if "DEPLOYED" in up or flag == "VSC":
                await self._bcast("🔶 <b>Virtual Safety Car (VSC)!</b>")
            elif "ENDING" in up or "RESUMED" in up:
                await self._bcast("🔶 <b>VSC заканчивается — рестарт!</b>")

        elif flag == "YELLOW":
            scope = m.get("scope", "")
            await self._bcast(f"🟡 <b>Жёлтый флаг</b>{' · Сектор ' + scope if scope else ''}\n{text}")

        elif flag == "RED":
            await self._bcast(f"🔴 <b>КРАСНЫЙ ФЛАГ!</b>\n\n{self.gp}\n{text}")

        elif "PENALTY" in up or "SANCTION" in up or "DRIVE THROUGH" in up or "STOP AND GO" in up:
            await self._bcast(f"⚖️ <b>Штраф</b>\n{text}")

        elif (flag == "CHEQUERED" or "CHEQUERED" in up or "CHECKERED" in up)                 and not self.finish_sent:
            self.finish_sent = True
            label = "Гонка" if self.sess_type == "race" else "Сессия"
            await self._bcast(f"🏁 <b>{label} завершена!</b>\n\n<b>{self.gp}</b>")
            if self.sess_type == "race":
                # Запускаем авто-публикацию результатов
                asyncio.ensure_future(self._post_race_results())
            await asyncio.sleep(180)
            self.stop()

    # ── Пит-стопы ────────────────────────────────────────────────────────────
    async def _pitstop(self):
        pits = await f1_pit(self.key)
        new_pits = []
        for p in pits:
            dur = p.get("pit_duration")
            if not dur:
                continue
            pid = f"{p.get('driver_number')}_{p.get('lap_number')}_{dur}"
            if pid not in self.seen_pit:
                self.seen_pit.add(pid)
                new_pits.append(p)
        # Отправляем батчем (не блокируем цикл)
        for p in new_pits:
            dn = p.get("driver_number")
            name = self._name(dn); flag = self._flag(dn); team = self._team(dn)
            await self._bcast(
                f"🔧 <b>Пит-стоп!</b>\n"
                f"{flag} <b>{name}</b> ({team})\n"
                f"📌 Круг {p.get('lap_number','?')}  ·  "
                f"⏱ {float(dur):.1f} с"
            )

    # ── Быстрый круг ─────────────────────────────────────────────────────────
    async def _fl(self):
        laps = await f1_laps(self.key)
        if not laps:
            return
        fastest = min(laps, key=lambda l: l["lap_duration"])
        dur = fastest["lap_duration"]
        if self.best_lap is not None and dur >= self.best_lap:
            return
        self.best_lap = dur
        dn   = fastest["driver_number"]
        mins = int(dur // 60); secs = dur % 60
        name = self._name(dn); flag = self._flag(dn); team = self._team(dn)
        await self._bcast(
            f"⚡ <b>Новый быстрый круг!</b>\n"
            f"{flag} <b>{name}</b> ({team})\n"
            f"⏱ <b>{mins}:{secs:06.3f}</b>  ·  Круг {fastest.get('lap_number','?')}"
        )

    # ── Финиш P1/P2/P3 ───────────────────────────────────────────────────────
    async def _positions(self):
        if len(self.podium_sent) >= 3:
            return
        pos_data = await f1_positions(self.key)
        if not pos_data:
            return
        latest: dict = {}
        for p in pos_data:
            latest[p.get("driver_number")] = p
        for dn, p in latest.items():
            position = p.get("position")
            if position in (1, 2, 3) and f"P{position}" not in self.podium_sent:
                lap = p.get("lap_number", 0) or 0
                if lap >= 45:
                    medals = {1:"🥇", 2:"🥈", 3:"🥉"}
                    self.podium_sent.add(f"P{position}")
                    name = self._name(dn); flag = self._flag(dn); team = self._team(dn)
                    await self._bcast(
                        f"{medals[position]} <b>P{position} — {name}!</b>\n"
                        f"{flag} ({team})  ·  Круг {lap}"
                    )

    # ── Авто-результаты после финиша ─────────────────────────────────────────
    async def _post_race_results(self):
        """
        Сразу после гонки: итоговые позиции из OpenF1.
        Через 10–40 мин (когда Jolpica обновится): официальные результаты.
        """
        await asyncio.sleep(90)   # ждём финальные позиции OpenF1

        # Шаг 1: быстрые результаты из OpenF1 positions
        pos_data = await f1_positions(self.key)
        if pos_data:
            latest: dict = {}
            for p in pos_data:
                dn = p.get("driver_number")
                if dn not in latest or p.get("lap_number",0) > latest[dn].get("lap_number",0):
                    latest[dn] = p
            sorted_pos = sorted(latest.values(), key=lambda x: x.get("position", 99))
            medals = {1:"🥇",2:"🥈",3:"🥉"}
            lines  = [f"🏁 <b>Итоги гонки — {self.gp}</b>\n", "<b>Топ-10:</b>"]
            for p in sorted_pos[:10]:
                dn   = p.get("driver_number")
                pos  = p.get("position", "?")
                name = self._name(dn); flag = self._flag(dn); team = self._team(dn)
                medal = medals.get(pos, f"{pos}.")
                lines.append(f"  {medal} {flag} {name} ({team})")
            lines.append("\n<i>Официальные результаты появятся позже в разделе «Итоги»</i>")
            await self._bcast("\n".join(lines))

        # Шаг 2: ждём Jolpica (проверяем каждые 10 мин, до 1 часа)
        for attempt in range(6):
            await asyncio.sleep(600)
            rname, results = await last_race()
            if results:
                stds = await driver_standings()
                pts  = {s["Driver"]["driverId"]: s["points"] for s in stds}
                lines = [f"📊 <b>Официальные результаты — {rname}</b>\n"]
                for r in results[:10]:
                    d    = r["Driver"]
                    last = d["familyName"]
                    flag = driver_emoji(last)
                    pos  = int(r["position"])
                    medal = medals.get(pos, f"{pos}.")
                    total = pts.get(d["driverId"], "—")
                    lines.append(
                        f"  {medal} {flag} {d['givenName'][0]}. {last}"
                        f" ({r['Constructor']['name']}) +{r.get('points','0')} → {total} очк."
                    )
                await self._bcast("\n".join(lines))
                log.info("Официальные результаты отправлены после %d мин", (attempt+1)*10)
                return
        log.warning("Jolpica так и не вернул результаты через час после гонки")


_monitor: Optional[LiveMonitor] = None

async def find_openf1_session(gp_name: str, sess_type: str) -> Optional[dict]:
    """
    Ищет session_key в OpenF1 по названию гонки и типу сессии.
    Пробует latest, затем ищет по времени ±2 часа.
    """
    # Попытка 1: latest
    for attempt in range(5):
        sess = await f1_latest_sess()
        if sess:
            sname = (sess.get("session_name") or "").lower()
            # Проверяем что это нужная сессия
            type_ok = (
                (sess_type == "race"     and ("race" in sname or "гонка" in sname)) or
                (sess_type == "quali"    and "qualif" in sname) or
                (sess_type == "practice" and ("practice" in sname or "fp" in sname)) or
                True  # если не можем определить — берём latest
            )
            if type_ok:
                log.info("OpenF1 сессия найдена (latest): sk=%s %s", sess["session_key"], sname)
                return sess
        log.info("start_live: ожидаем OpenF1 (попытка %d/5)...", attempt+1)
        await asyncio.sleep(30)

    # Попытка 2: поиск по текущему году и ближайшей сессии
    now = datetime.now(tz=pytz.utc)
    year = now.year
    sessions = await oget(f"/sessions?year={year}")
    if sessions:
        # Найдём сессию, которая началась в последние 3 часа
        for s in reversed(sessions):
            try:
                start_str = s.get("date_start", "")
                if not start_str:
                    continue
                # Парсим ISO дату
                from datetime import datetime as _dt
                if start_str.endswith("Z"):
                    start_str = start_str[:-1] + "+00:00"
                s_start = _dt.fromisoformat(start_str).replace(tzinfo=pytz.utc)
                delta   = (now - s_start).total_seconds()
                if 0 <= delta <= 7200:   # началась в последние 2 часа
                    log.info("OpenF1 сессия найдена по времени: sk=%s", s["session_key"])
                    return s
            except Exception as e:
                continue

    log.warning("start_live: сессия не найдена для %s", gp_name)
    return None

async def start_live(gp: str, city: str, sess_type: str = "race"):
    global _monitor
    if _monitor and _monitor.running:
        _monitor.stop()
    sess = await find_openf1_session(gp, sess_type)
    if not sess:
        log.warning("start_live: не смогли найти OpenF1 сессию для %s", gp)
        return
    # Формируем mv_path для Multiviewer: "{round}/{session_abbr}"
    year  = datetime.now(tz=pytz.utc).year
    rnd   = sess.get("meeting_key", 0)
    stype_abbr = {"race": "R", "quali": "Q", "practice": "P1", "sprint": "S"}.get(sess_type, "R")
    mv_path = f"{rnd}/{stype_abbr}"
    _monitor = LiveMonitor(sess["session_key"], gp, city, sess_type, mv_path=mv_path, year=year)
    asyncio.create_task(_monitor.run())

# ── SCHEDULER ─────────────────────────────────────────────────────────────────
async def send_reminder(ev,mins):
    city=ev.get("city",ev["location"].split(",")[0].strip())
    msk_dt=_msk(ev["start_utc"]); loc=ev["start_utc"].astimezone(local_tz(city))
    if   mins==0:  head=f"{ev['emoji']} <b>СТАРТ ПРЯМО СЕЙЧАС!</b>"
    elif mins==5:  head=f"{ev['emoji']} <b>До старта 5 минут!</b>"
    else:          head=f"{ev['emoji']} <b>До старта 30 минут!</b>"
    lines=[head,"",f"{ev.get('flag','🏁')} <b>{ev['summary']}</b>",
           f"📍 {ev.get('country','')}, {city}",
           f"🕐 <b>{msk_dt.strftime('%H:%M')} МСК</b>  ·  {loc.strftime('%H:%M')} местного",""]
    wdata=await get_weather(city, target_dt=ev["start_utc"])
    lines.append(fmt_weather_block(wdata, target_dt=ev["start_utc"]))
    if "гонка" in ev["summary"].lower() and mins >= 0:
        _,qr=await last_quali()
        if qr:
            lines.append("\n🏁 <b>Стартовая решётка:</b>")
            for q in qr:
                d    = q["Driver"]
                last = d["familyName"]
                flag = driver_emoji(last)
                team = q["Constructor"]["name"]
                lines.append(f"  <b>P{q['position']}</b>  {flag} {d['givenName'][0]}. {last}  ({team})")
    text="\n".join(lines)
    await broadcast(text)

def schedule_all():
    wks=build_weekends(); now=datetime.now(tz=pytz.utc); count=0
    for w in wks:
        for s in w["sessions"]:
            start=s["start_utc"]; is_race="гонка" in s["summary"].lower()
            ev={**s,"gp_name":w["gp_name"],"city":w["city"],"country":w["country"],"flag":w["flag"],"location":s["location"]}
            for m in [30,5,0]:
                t=start-timedelta(minutes=m)
                if t>now:
                    scheduler.add_job(send_reminder,"date",run_date=t,args=[ev,m],
                                      id=f"r{m}_{s['summary']}_{start.isoformat()}",replace_existing=True)
                    count+=1
            # Запускаем live-монитор для гонки, квалификации и практики
            is_quali   = "квалификация" in s["summary"].lower() and "спринту" not in s["summary"].lower()
            is_sprint  = "спринт" in s["summary"].lower()
            is_practice= "свободных" in s["summary"].lower()
            if (is_race or is_quali or is_sprint or is_practice) and start>now:
                t_live = start + timedelta(minutes=2)
                if is_race or is_sprint:
                    stype = "race"
                elif is_quali:
                    stype = "quali"
                else:
                    stype = "practice"
                scheduler.add_job(start_live,"date",run_date=t_live,
                                  args=[w["gp_name"],w["city"],stype],
                                  id=f"live_{s['summary']}_{start.isoformat()}",replace_existing=True)
    log.info("Запланировано %d напоминаний, %d уикендов",count,len(wks))

# ── KEYBOARDS ─────────────────────────────────────────────────────────────────
REPLY_KB=ReplyKeyboardMarkup([["🏠 Меню"]],resize_keyboard=True,is_persistent=True)
def _home(): return [InlineKeyboardButton("🏠 Меню",callback_data="home")]

def main_kb(cid, priv):
    rows = []
    if priv:
        subbed  = is_subscribed(cid)
        enabled = subbed and not is_muted(cid)
        if not subbed:   label = "🔔 Уведомления — выкл"
        elif enabled:    label = "✅ Уведомления — вкл"
        else:            label = "☑️ Уведомления — выкл"
        rows.append([InlineKeyboardButton(label, callback_data="notif_toggle")])
    rows += [
        [InlineKeyboardButton("⚡ Ближайшее событие",       callback_data="cal:next_sess")],
        [InlineKeyboardButton("🏁 Этот уикенд",             callback_data="cal:current")],
        [InlineKeyboardButton("⏭ Следующий уикенд",         callback_data="cal:next")],
        [InlineKeyboardButton("🗓 Календарь всех событий",  callback_data="cal:months")],
        [InlineKeyboardButton("🕰 Прошедшие уикенды",       callback_data="cal:past")],
        [InlineKeyboardButton("🏆 Баллы за сезон",          callback_data="res:standings")],
        [InlineKeyboardButton("✖️ Закрыть",                  callback_data="close")],
    ]
    return InlineKeyboardMarkup(rows)

async def cur_weekend_kb(w: dict = None):
    """Кнопки текущего уикенда с индикатором наличия результатов.
    w — уикенд для которого показываем кнопки; галочки проверяются по нему."""
    qname, qr = await last_quali()
    rname, rr = await last_race()

    # Проверяем что результаты относятся к этому уикенду (по названию ГП)
    gp = (w.get("gp_name") or "").lower() if w else ""
    def _matches(name: str) -> bool:
        if not gp or not name: return False
        # Jolpica возвращает "Australian Grand Prix" — сравниваем ключевое слово
        nl = name.lower()
        # Берём первое слово из gp_name и ищем его в rname
        keyword = gp.split()[0]
        return keyword in nl

    q_dot = "🟢" if (qr and _matches(qname)) else "⚫️"
    r_dot = "🟢" if (rr and _matches(rname)) else "⚫️"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔥 Квалификация — результаты {q_dot}", callback_data="res:quali")],
        [InlineKeyboardButton(f"🏁 Гонка — результаты {r_dot}",        callback_data="res:race")],
        _home(),
    ])

def months_kb(wks):
    seen = OrderedDict()
    for w in wks:
        m = _msk(w["start_utc"]); seen[(m.year, m.month)] = MR[m.month]
    rows = []; row = []
    for (y, mo), name in seen.items():
        row.append(InlineKeyboardButton(name, callback_data=f"cal:month:{y}-{mo:02d}"))
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    rows.append(_home()); return InlineKeyboardMarkup(rows)

def month_nav_kb(wks, year, month):
    all_m = sorted({(_msk(w["start_utc"]).year, _msk(w["start_utc"]).month) for w in wks})
    idx   = all_m.index((year, month)) if (year, month) in all_m else 0
    nav   = []
    if idx > 0:
        py,pm = all_m[idx-1]; nav.append(InlineKeyboardButton(f"◀️ {MR[pm][:3]}", callback_data=f"cal:month:{py}-{pm:02d}"))
    if idx < len(all_m)-1:
        ny,nm = all_m[idx+1]; nav.append(InlineKeyboardButton(f"{MR[nm][:3]} ▶️", callback_data=f"cal:month:{ny}-{nm:02d}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton("📅 Все месяцы", callback_data="cal:months")])
    rows.append(_home()); return InlineKeyboardMarkup(rows)

def past_weekends_kb(wks):
    """Кнопки прошедших уикендов."""
    now  = datetime.now(tz=pytz.utc)
    past = [w for w in wks if w["end_utc"] < now]
    rows = []
    for i, w in enumerate(reversed(past)):  # свежие сверху
        s = _msk(w["start_utc"]); e = _msk(w["end_utc"])
        if s.month == e.month:
            dates = f"{s.day}–{e.day} {MG[s.month]}"
        else:
            dates = f"{s.day} {MG[s.month]}–{e.day} {MG[e.month]}"
        label = f"{w['flag']} {w['country']}, {w['city']}  ·  {dates}"
        rnd   = len(past) - i   # номер раунда
        rows.append([InlineKeyboardButton(label, callback_data=f"cal:past:{rnd}")])
    rows.append(_home())
    return InlineKeyboardMarkup(rows)

async def past_weekend_detail_kb(rnd: int):
    _, qr = await quali_by_round(2026, rnd)
    q_dot = "🟢" if qr else "⚫️"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔥 Квалификация — результаты {q_dot}", callback_data=f"cal:past_quali:{rnd}")],
        _home(),
    ])

def back_kb(): return InlineKeyboardMarkup([_home()])
def results_kb(): return InlineKeyboardMarkup([
    [InlineKeyboardButton("🔥 Квалификация", callback_data="res:quali"),
     InlineKeyboardButton("🏁 Гонка",         callback_data="res:race")],
    _home(),
])

# ── HELPERS ───────────────────────────────────────────────────────────────────
def _past_weekends():
    now = datetime.now(tz=pytz.utc)
    return [w for w in build_weekends() if w["end_utc"] < now]

def _round_for_past(wks_past: list, rnd: int) -> Optional[dict]:
    """rnd — 1-based, свежие сначала (reversed порядок)."""
    ordered = list(reversed(wks_past))
    if 1 <= rnd <= len(ordered):
        return ordered[rnd - 1]
    return None

# ── HANDLERS ──────────────────────────────────────────────────────────────────
async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = upd.effective_chat; priv = chat.type == "private"
    subscribe(chat.id, priv)
    desc = (
        "⏰ Напоминания <b>за 30 мин</b> и <b>за 5 мин</b> до каждой сессии\n"
        "🌡 Погода + местное время в напоминаниях\n"
        "🔴 Live-события во время гонки\n"
        "🏆 Результаты и чемпионат\n\n"
        "Нажми <b>🔔 Уведомления — выкл</b> чтобы включить!"
        if priv else
        "Этот чат добавлен — уведомления будут приходить сюда!\n\nИспользуй /start для меню."
    )
    await upd.message.reply_text("🏠", reply_markup=REPLY_KB)
    await upd.message.reply_text(
        f"🏎️ <b>F1 2026 — бот уведомлений</b>\n\n{desc}",
        parse_mode="HTML", reply_markup=main_kb(chat.id, priv)
    )

async def on_menu(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = upd.effective_chat; priv = chat.type == "private"
    # Удаляем сообщение пользователя «🏠 Меню» (тихо — в группах может не быть прав)
    try:
        await upd.message.delete()
    except Exception:
        pass
    # Отправляем через chat, а не через message (message уже удалён)
    await ctx.bot.send_message(
        chat_id=chat.id,
        text="🏎️ <b>F1 2026 — главное меню</b>",
        parse_mode="HTML",
        reply_markup=main_kb(chat.id, priv)
    )

async def cmd_status(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d   = _load(); now = datetime.now(tz=MSK)
    live = "✅ активен" if (_monitor and _monitor.running) else "💤 не активен"
    await upd.message.reply_text(
        f"📊 <b>Статус бота</b>\n\n"
        f"👤 Личных: {len(d['users'])}\n"
        f"💬 Чатов: {len(d['chats'])}\n"
        f"🔕 Выключили: {len(d.get('muted',[]))}\n"
        f"⏰ Напоминаний: {len(scheduler.get_jobs())}\n"
        f"🔴 Live: {live}\n"
        f"🕐 {now.strftime('%d.%m.%Y %H:%M')} МСК",
        parse_mode="HTML"
    )

async def on_cb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q: CallbackQuery = upd.callback_query
    await q.answer()
    data = q.data
    chat = upd.effective_chat
    priv = chat.type == "private"

    # ── Закрыть ───────────────────────────────────────────────────────────────
    if data == "close":
        try: await q.message.delete()
        except: await q.edit_message_reply_markup(reply_markup=None)
        return

    # ── Главное меню ──────────────────────────────────────────────────────────
    if data == "home":
        await q.edit_message_text(
            "🏎️ <b>F1 2026 — главное меню</b>",
            parse_mode="HTML", reply_markup=main_kb(chat.id, priv)
        ); return

    # ── Уведомления ───────────────────────────────────────────────────────────
    if data == "notif_toggle" and priv:
        enabled = toggle_notif(chat.id, True)
        await q.answer("✅ Уведомления включены!" if enabled else "☑️ Уведомления выключены", show_alert=True)
        await q.edit_message_reply_markup(reply_markup=main_kb(chat.id, True)); return

    # ── Ближайшее событие ─────────────────────────────────────────────────────
    if data == "cal:next_sess":
        s = nxt_session(build_weekends())
        if not s:
            await q.edit_message_text("😔 Нет предстоящих событий.", reply_markup=back_kb()); return
        await q.edit_message_text("⏳ Загружаю погоду...", parse_mode="HTML")
        text = await fmt_next_sess(s)
        await q.edit_message_text(text[:4090], parse_mode="HTML", reply_markup=back_kb()); return

    # ── Этот уикенд (с кнопками результатов) ─────────────────────────────────
    if data == "cal:current":
        wks = build_weekends(); w = cur_weekend(wks) or nxt_weekend(wks)
        if not w:
            await q.edit_message_text("😔 Нет данных.", reply_markup=back_kb()); return
        await q.edit_message_text("⏳ Загружаю...", parse_mode="HTML")
        text = await fmt_cur_weekend_with_results(w)
        await q.edit_message_text(text[:4090], parse_mode="HTML", reply_markup=await cur_weekend_kb(w)); return

    # ── Следующий уикенд ──────────────────────────────────────────────────────
    if data == "cal:next":
        w = nxt_weekend(build_weekends())
        if not w:
            await q.edit_message_text("😔 Нет предстоящих уикендов.", reply_markup=back_kb()); return
        await q.edit_message_text(fmt_card(w, show_done=False), parse_mode="HTML", reply_markup=back_kb()); return

    # ── Календарь: выбор месяца ────────────────────────────────────────────────
    if data == "cal:months":
        wks = build_weekends()
        await q.edit_message_text(
            "📅 <b>Выбери месяц:</b>", parse_mode="HTML", reply_markup=months_kb(wks)
        ); return

    if data.startswith("cal:month:"):
        ym    = data.split(":", 2)[2]
        year  = int(ym.split("-")[0]); month = int(ym.split("-")[1])
        wks   = build_weekends()
        text  = fmt_month(wks, year, month)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=month_nav_kb(wks, year, month)); return

    # ── Прошедшие уикенды ─────────────────────────────────────────────────────
    if data == "cal:past":
        past = _past_weekends()
        if not past:
            await q.edit_message_text("😔 Ещё не прошло ни одного уикенда.", reply_markup=back_kb()); return
        await q.edit_message_text(
            "🕰 <b>Прошедшие уикенды:</b>", parse_mode="HTML",
            reply_markup=past_weekends_kb(past)
        ); return

    if data.startswith("cal:past:") and not data.startswith("cal:past_quali:"):
        rnd  = int(data.split(":")[-1])
        past = _past_weekends()
        w    = _round_for_past(past, rnd)
        if not w:
            await q.edit_message_text("😔 Уикенд не найден.", reply_markup=back_kb()); return
        await q.edit_message_text("⏳ Загружаю результаты...", parse_mode="HTML")
        text = await fmt_past_weekend(w, rnd)
        await q.edit_message_text(
            text[:4090], parse_mode="HTML",
            reply_markup=await past_weekend_detail_kb(rnd)
        ); return

    if data.startswith("cal:past_quali:"):
        rnd  = int(data.split(":")[-1])
        past = _past_weekends()
        w    = _round_for_past(past, rnd)
        if not w:
            await q.edit_message_text("😔 Уикенд не найден.", reply_markup=back_kb()); return
        await q.edit_message_text("⏳ Загружаю квалификацию...", parse_mode="HTML")
        text = await fmt_past_quali(w, rnd)
        await q.edit_message_text(
            text[:4090], parse_mode="HTML",
            reply_markup=await past_weekend_detail_kb(rnd)
        ); return

    # ── Чемпионат ─────────────────────────────────────────────────────────────
    if data == "res:standings":
        await q.edit_message_text("⏳ Загружаю...", parse_mode="HTML")
        await q.edit_message_text(
            (await fmt_standings())[:4090], parse_mode="HTML", reply_markup=back_kb()
        ); return

    # ── Результаты (квали/гонка) — из текущего уикенда ───────────────────────
    if data == "res:quali":
        await q.edit_message_text("⏳ Загружаю...", parse_mode="HTML")
        wks = build_weekends(); cw = cur_weekend(wks) or nxt_weekend(wks)
        await q.edit_message_text(
            (await fmt_quali())[:4090], parse_mode="HTML", reply_markup=await cur_weekend_kb(cw)
        ); return

    if data == "res:race":
        await q.edit_message_text("⏳ Загружаю...", parse_mode="HTML")
        wks = build_weekends(); cw = cur_weekend(wks) or nxt_weekend(wks)
        await q.edit_message_text(
            (await fmt_race())[:4090], parse_mode="HTML", reply_markup=await cur_weekend_kb(cw)
        ); return

# ── MAIN ──────────────────────────────────────────────────────────────────────
async def post_init(app:Application):
    global http,_app
    http=aiohttp.ClientSession(); _app=app
    schedule_all(); scheduler.start()
    log.info("🏎️  F1 2026 Bot запущен!")

async def post_shutdown(app:Application):
    if _monitor and _monitor.running: _monitor.stop()
    if http and not http.closed: await http.close()
    if scheduler.running: scheduler.shutdown(wait=False)

def main():
    app=(Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("status",cmd_status))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.Text(["🏠 Меню"]),on_menu))
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
