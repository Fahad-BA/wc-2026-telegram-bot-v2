import asyncio
import logging
import sqlite3
import aiohttp
import json
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime, timedelta, timezone

# --- FLAG MAPPING (German names → emoji) ---
FLAG_MAP = {
    'Algerien': '🇩🇿', 'Argentinien': '🇦🇷', 'Australien': '🇦🇺', 'Belgien': '🇧🇪',
    'Bosnien-Herzegowina': '🇧🇦', 'Brasilien': '🇧🇷', 'Curaçao': '🇨🇼', 'DR Kongo': '🇨🇩',
    'Deutschland': '🇩🇪', 'Ecuador': '🇪🇨', 'Elfenbeinküste': '🇨🇮', 'England': '🏴󠁧󠁢󠁥󠁮󠁧󠁿',
    'Frankreich': '🇫🇷', 'Ghana': '🇬🇭', 'Haiti': '🇭🇹', 'Irak': '🇮🇶', 'Iran': '🇮🇷',
    'Japan': '🇯🇵', 'Jordanien': '🇯🇴', 'Kanada': '🇨🇦', 'Kap Verde': '🇨🇻',
    'Katar': '🇶🇦', 'Kolumbien': '🇨🇴', 'Kroatien': '🇭🇷', 'Marokko': '🇲🇦',
    'Mexiko': '🇲🇽', 'Neuseeland': '🇳🇿', 'Niederlande': '🇳🇱', 'Norwegen': '🇳🇴',
    'Panama': '🇵🇦', 'Paraguay': '🇵🇾', 'Portugal': '🇵🇹', 'Saudi-Arabien': '🇸🇦',
    'Schottland': '🏴󠁧󠁢󠁳󠁣󠁴󠁿', 'Schweden': '🇸🇪', 'Schweiz': '🇨🇭', 'Senegal': '🇸🇳',
    'Spanien': '🇪🇸', 'Südafrika': '🇿🇦', 'Südkorea': '🇰🇷', 'Tschechien': '🇨🇿',
    'Tunesien': '🇹🇳', 'Türkei': '🇹🇷', 'USA': '🇺🇸', 'Uruguay': '🇺🇾',
    'Usbekistan': '🇺🇿', 'Ägypten': '🇪🇬', 'Österreich': '🇦🇹',
}

def flag(name):
    return FLAG_MAP.get(name, name)

# --- CONFIGURATION ---
BOT_TOKEN = "BOT_TOKEN_REMOVED"
USER_ID = 697241718  # Direct messages to this User ID
OLDB_SHORTCUT = "wm2026"
OLDB_SEASON = "2026"
API_BASE_URL = "https://api.openligadb.de"
TOURNAMENT_START_DATE = datetime(2026, 6, 11)
RIYADH_TZ = timezone(timedelta(hours=3))

def riyadh_time(utc_str):
    """Convert UTC datetime string to Riyadh time string."""
    if not utc_str:
        return '—'
    try:
        dt = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
        return dt.astimezone(RIYADH_TZ).strftime('%H:%M')
    except:
        return '—'

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY)')
    cursor.execute('CREATE TABLE IF NOT EXISTS processed_lineups (fixture_id INTEGER PRIMARY KEY)')
    cursor.execute('CREATE TABLE IF NOT EXISTS processed_summaries (fixture_id INTEGER PRIMARY KEY)')
    cursor.execute('''CREATE TABLE IF NOT EXISTS api_cache (
        endpoint TEXT PRIMARY KEY,
        data TEXT,
        updated_at TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def is_processed(table, identifier):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute(f"SELECT 1 FROM {table} WHERE { 'event_id' if table == 'processed_events' else 'fixture_id' } = ?", (identifier,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_as_processed(table, identifier):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    try:
        cursor.execute(f"INSERT INTO {table} VALUES (?)", (identifier,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

# --- CACHE HELPERS ---
def get_cached_data(endpoint, ttl_minutes=60):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT data, updated_at FROM api_cache WHERE endpoint = ?", (endpoint,))
    row = cursor.fetchone()
    conn.close()

    if row:
        data_str, updated_at_str = row
        try:
            parsed = json.loads(data_str)
            if not parsed:
                 return None
        except:
            return None

        updated_at = datetime.fromisoformat(updated_at_str)
        if ttl_minutes == -1 or (datetime.now() - updated_at < timedelta(minutes=ttl_minutes)):
            return parsed
    return None

def save_cache(endpoint, data):
    if not data:
        return
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO api_cache (endpoint, data, updated_at) VALUES (?, ?, ?)",
        (endpoint, json.dumps(data), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

# --- API HELPERS ---
async def fetch_oldb(session, endpoint, params=None, use_cache=False, ttl_minutes=60):
    param_str = json.dumps(params, sort_keys=True) if params else ""
    cache_key = f"oldb_{endpoint}_{param_str}"
    
    if use_cache:
        cached = get_cached_data(cache_key, ttl_minutes)
        if cached:
            return cached

    url = f"{API_BASE_URL}/{endpoint}"
    
    async with session.get(url, params=params) as response:
        if response.status == 200:
            data = await response.json()
            if use_cache and data:
                save_cache(cache_key, data)
            return data
        else:
            logging.error(f"OpenLigaDB API Error {response.status} for {endpoint}")
            return None

def get_smart_ttl(data, requested_date_str=None):
    """
    Calculate TTL for tournament data.
    If requested_date_str is today, or contains unfinished matches, return 5 minutes.
    Otherwise return 1440 minutes (24h).
    """
    if not data or not isinstance(data, list):
        return 5
    
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    # If this fetch includes today's matches, always use short TTL
    has_today = any((m.get('matchDateTimeUTC') or m.get('matchDateTime') or '').startswith(today_str) for m in data)
    if has_today:
        return 5
        
    # If filtering for a specific date, and that date has unfinished matches, use short TTL
    if requested_date_str:
        matches = [m for m in data if (m.get('matchDateTimeUTC') or m.get('matchDateTime') or '').startswith(requested_date_str)]
        if matches and not all(m.get('matchIsFinished') for m in matches):
            return 5
            
    return 1440

# --- NOTIFICATION LOGIC ---
async def safe_send(bot, text):
    try:
        await bot.send_message(USER_ID, text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Failed to send message to {USER_ID}: {e}")

async def broadcast_summary(bot, session, match):
    match_id = match['matchID']
    if is_processed('processed_summaries', match_id):
        return

    home_name = flag(match['team1']['teamName'])
    away_name = flag(match['team2']['teamName'])
    
    final_result = next((r for r in match.get('matchResults', []) if r.get('resultTypeID') == 2), None)
    if not final_result:
        final_result = match.get('matchResults', [{}])[0]
        
    score = f"{final_result.get('pointsTeam1', 0)} - {final_result.get('pointsTeam2', 0)}"

    text = f"🏁 *نهاية المباراة: {home_name} {score} {away_name}*\n\n"
    text += "ملاحظة: تفاصيل الإحصائيات غير متوفرة حالياً عبر هذا المصدر."

    await safe_send(bot, text)
    mark_as_processed('processed_summaries', match_id)

# --- MAIN LOOPS ---
async def monitor_world_cup(bot):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                sleep_interval = 1800
                # Use a very short TTL for live monitoring
                data = await fetch_oldb(session, f"getmatchdata/{OLDB_SHORTCUT}/{OLDB_SEASON}", use_cache=True, ttl_minutes=1)
                
                if data and isinstance(data, list):
                    for m in data:
                        is_finished = m.get('matchIsFinished', False)
                        if is_finished:
                            await broadcast_summary(bot, session, m)

                logging.info(f"Polling check complete. Sleeping for {sleep_interval}s.")
                await asyncio.sleep(sleep_interval)
            except Exception as e:
                logging.error(f"Monitor loop error: {e}")
                await asyncio.sleep(60)

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def start_cmd(message: types.Message):
        await message.answer("مرحباً! بوت كأس العالم 2026 (محرك OpenLigaDB) نشط حالياً. استخدم /help للمساعدة.")

    @dp.message(Command("help"))
    async def help_cmd(message: types.Message):
        builder = InlineKeyboardBuilder()
        curr = TOURNAMENT_START_DATE
        today = datetime.now()
        while curr <= today:
            d_str = curr.strftime('%Y-%m-%d')
            btn_text = curr.strftime('%b %d')
            builder.add(InlineKeyboardButton(text=btn_text, callback_data=f"oldbdate:{d_str}"))
            curr += timedelta(days=1)
        builder.adjust(4)
        text = "🏆 *بوت كأس العالم 2026*\n\n/fixtures - مباريات اليوم\n/results - نتائج اليوم\n/goals <id> - أهداف مباراة\n/lineups <id> - التشكيلة\n/cards <id> - البطاقات\n\nاختر تاريخاً:"
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.callback_query(F.data.startswith("oldbdate:"))
    async def handle_date_selection(callback: CallbackQuery):
        date_str = callback.data.split(":")[1]
        async with aiohttp.ClientSession() as session:
            # First, check for any cache (even a 24h one)
            data = await fetch_oldb(session, f"getmatchdata/{OLDB_SHORTCUT}/{OLDB_SEASON}", use_cache=True, ttl_minutes=1440)
            
            # Recalculate smart TTL based on current state
            ttl = get_smart_ttl(data, date_str)
            if ttl == 5:
                # If we need fresh data, re-fetch with short TTL
                data = await fetch_oldb(session, f"getmatchdata/{OLDB_SHORTCUT}/{OLDB_SEASON}", use_cache=True, ttl_minutes=5)

            if data and isinstance(data, list):
                matches = [m for m in data if (m.get('matchDateTimeUTC') or m.get('matchDateTime') or '').startswith(date_str)]
                if matches:
                    text = f"📅 *مباريات يوم {date_str}:*\n\n"
                    for m in matches:
                        results = m.get('matchResults', [])
                        final_res = next((r for r in results if r.get('resultTypeID') == 2), results[0] if results else {})
                        res = f"{final_res.get('pointsTeam1', '?')} - {final_res.get('pointsTeam2', '?')}"
                        kickoff = riyadh_time(m.get('matchDateTimeUTC') or m.get('matchDateTime'))
                        status = "FT" if m.get('matchIsFinished') else kickoff
                        text += f"• `{m['matchID']}`: 🏠 {flag(m['team1']['teamName'])} {res} {flag(m['team2']['teamName'])} 🏃 ({status})\n"
                    
                    try:
                        await callback.message.edit_text(text, parse_mode="Markdown")
                    except Exception:
                        await callback.message.answer(text, parse_mode="Markdown")
                    return
            await callback.answer("لا توجد مباريات لهذا اليوم.")

    @dp.message(Command("fixtures"))
    async def cmd_fixtures(message: types.Message):
        today = datetime.now().strftime('%Y-%m-%d')
        async with aiohttp.ClientSession() as session:
            # Fixtures are by definition upcoming/live, use short TTL
            data = await fetch_oldb(session, f"getmatchdata/{OLDB_SHORTCUT}/{OLDB_SEASON}", use_cache=True, ttl_minutes=5)
            if data and isinstance(data, list):
                matches = [m for m in data if (m.get('matchDateTimeUTC') or m.get('matchDateTime') or '').startswith(today)]
                if matches:
                    text = "📅 *مباريات اليوم:*\n\n"
                    for m in matches:
                        kickoff = riyadh_time(m.get('matchDateTimeUTC') or m.get('matchDateTime'))
                        text += f"ID: `{m['matchID']}` | {kickoff} | 🏠 {flag(m['team1']['teamName'])} ضد {flag(m['team2']['teamName'])} 🏃\n"
                    await message.answer(text, parse_mode="Markdown")
                    return
            await message.answer("لا توجد مباريات اليوم.")

    @dp.message(Command("results"))
    async def cmd_results(message: types.Message):
        today = datetime.now().strftime('%Y-%m-%d')
        async with aiohttp.ClientSession() as session:
            # Results change as matches finish, use short TTL
            data = await fetch_oldb(session, f"getmatchdata/{OLDB_SHORTCUT}/{OLDB_SEASON}", use_cache=True, ttl_minutes=5)
            if data and isinstance(data, list):
                matches = [m for m in data if (m.get('matchDateTimeUTC') or m.get('matchDateTime') or '').startswith(today) and m.get('matchIsFinished')]
                if matches:
                    text = "🏁 *نتائج اليوم:*\n\n"
                    for m in matches:
                        results = m.get('matchResults', [])
                        final_res = next((r for r in results if r.get('resultTypeID') == 2), results[0] if results else {})
                        text += f"🏠 {flag(m['team1']['teamName'])} {final_res.get('pointsTeam1', '?')} - {final_res.get('pointsTeam2', '?')} {flag(m['team2']['teamName'])} 🏃\n"
                    await message.answer(text, parse_mode="Markdown")
                    return
            await message.answer("لم تنتهِ أي مباريات اليوم بعد.")

    @dp.message(Command("goals"))
    async def cmd_goals(message: types.Message, command: CommandObject):
        if not command.args:
            await message.answer("استخدام: `/goals <match_id>`\n\nاكتب /fixtures لعرض مباريات اليوم وأرقامها.", parse_mode="Markdown")
            return
        try:
            match_id = int(command.args.strip())
        except ValueError:
            await message.answer("⚠️ الـ ID يجب أن يكون رقماً. مثال: `/goals 66123`", parse_mode="Markdown")
            return
        async with aiohttp.ClientSession() as session:
            # Use short TTL for goals as they update during live games
            data = await fetch_oldb(session, f"getmatchdata/{OLDB_SHORTCUT}/{OLDB_SEASON}", use_cache=True, ttl_minutes=5)
            if data and isinstance(data, list):
                match = next((m for m in data if int(m.get('matchID', 0)) == match_id), None)
                if match:
                    home = flag(match['team1']['teamName'])
                    away = flag(match['team2']['teamName'])
                    goals = match.get('goals', [])
                    if not goals:
                        results = match.get('matchResults', [])
                        final_res = next((r for r in results if r.get('resultTypeID') == 2), results[0] if results else {})
                        score = f"{final_res.get('pointsTeam1', 0)} - {final_res.get('pointsTeam2', 0)}"
                        await message.answer(f"⚽ *{home} {score} {away}*\n\nلا توجد تفاصيل أهداف متاحة.", parse_mode="Markdown")
                        return
                    kickoff = riyadh_time(match.get('matchDateTimeUTC') or match.get('matchDateTime'))
                    text = f"⚽ *{home} ضد {away}* ({kickoff} توقيت الرياض)\n\n"
                    has_details = any(g.get('goalGetterName') for g in goals)
                    if has_details:
                        prev_s1, prev_s2 = 0, 0
                        for g in goals:
                            scorer = g.get('goalGetterName', 'غير معروف')
                            minute = g.get('matchMinute') or '??'
                            s1 = g.get('scoreTeam1', 0)
                            s2 = g.get('scoreTeam2', 0)
                            penalty = " (ركلة جزاء)" if g.get('isPenalty', False) else ""
                            own = " (هدف عكسي)" if g.get('isOwnGoal', False) else ""
                            if g.get('isOwnGoal'):
                                side = f"🏃 {away}" if s1 > prev_s1 else f"🏠 {home}"
                            elif s1 > prev_s1:
                                side = f"🏠 {home}"
                            else:
                                side = f"🏃 {away}"
                            prev_s1, prev_s2 = s1, s2
                            text += f"{side} {scorer} ({minute}'){penalty}{own}\n"
                    else:
                        prev_s1, prev_s2 = 0, 0
                        for g in goals:
                            s1 = g.get('scoreTeam1', 0)
                            s2 = g.get('scoreTeam2', 0)
                            if s1 > prev_s1:
                                side = f"🏠 {home}"
                            else:
                                side = f"🏃 {away}"
                            prev_s1, prev_s2 = s1, s2
                            minute = g.get('matchMinute') or '??'
                            penalty = " (ركلة جزاء)" if g.get('isPenalty', False) else ""
                            own = " (هدف عكسي)" if g.get('isOwnGoal', False) else ""
                            text += f"{side} ({minute}'){penalty}{own} → {s1}-{s2}\n"
                        text += "\n_⚠️ بيانات الهدافين غير متوفرة في OpenLigaDB لهذه المباراة_"
                    await message.answer(text, parse_mode="Markdown")
                    return
            await message.answer("⚠️ لم يتم العثور على مباراة بهذا الرقم.\nاستخدم /fixtures لعرض المباريات المتاحة.", parse_mode="Markdown")

    @dp.message(Command("lineups"))
    async def cmd_lineups(message: types.Message, command: CommandObject):
        await message.answer("🏟 *التشكيلة*\n\nعذراً، بيانات التشكيلة غير متوفرة حالياً عبر محرك OpenLigaDB المجاني.", parse_mode="Markdown")

    @dp.message(Command("cards"))
    async def cmd_cards(message: types.Message, command: CommandObject):
        await message.answer("🟨 *البطاقات*\n\nعذراً، تفاصيل البطاقات والأحداث غير متوفرة حالياً عبر محرك OpenLigaDB المجاني.", parse_mode="Markdown")

    await asyncio.gather(dp.start_polling(bot), monitor_world_cup(bot))

if __name__ == "__main__":
    asyncio.run(main())
