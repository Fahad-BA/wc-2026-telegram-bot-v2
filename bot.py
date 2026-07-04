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

# --- FLAG MAPPING ---
FLAG_MAP = {
    # API-Football (English) — primary
    'Algeria': '🇩🇿', 'Argentina': '🇦🇷', 'Australia': '🇦🇺', 'Austria': '🇦🇹',
    'Belgium': '🇧🇪', 'Bosnia & Herzegovina': '🇧🇦', 'Brazil': '🇧🇷',
    'Canada': '🇨🇦', 'Cape Verde Islands': '🇨🇻', 'Colombia': '🇨🇴',
    'Congo DR': '🇨🇩', 'Croatia': '🇭🇷', 'Curaçao': '🇨🇼', 'Czechia': '🇨🇿',
    'Ecuador': '🇪🇨', 'Egypt': '🇪🇬', 'England': '🏴󠁧󠁢󠁥󠁮󠁧󠁿', 'France': '🇫🇷',
    'Germany': '🇩🇪', 'Ghana': '🇬🇭', 'Haiti': '🇭🇹', 'Iran': '🇮🇷',
    'Iraq': '🇮🇶', 'Ivory Coast': '🇨🇮', 'Japan': '🇯🇵', 'Jordan': '🇯🇴',
    'Mexico': '🇲🇽', 'Morocco': '🇲🇦', 'Netherlands': '🇳🇱',
    'New Zealand': '🇳🇿', 'Norway': '🇳🇴', 'Panama': '🇵🇦', 'Paraguay': '🇵🇾',
    'Portugal': '🇵🇹', 'Qatar': '🇶🇦', 'Saudi Arabia': '🇸🇦',
    'Scotland': '🏴󠁧󠁢󠁳󠁣󠁴󠁿', 'Senegal': '🇸🇳', 'South Africa': '🇿🇦',
    'South Korea': '🇰🇷', 'Spain': '🇪🇸', 'Sweden': '🇸🇪', 'Switzerland': '🇨🇭',
    'Tunisia': '🇹🇳', 'Türkiye': '🇹🇷', 'USA': '🇺🇸', 'Uruguay': '🇺🇾',
    'Uzbekistan': '🇺🇿',
    # OpenLigaDB (German) — legacy fallback
    'Algerien': '🇩🇿', 'Argentinien': '🇦🇷', 'Australien': '🇦🇪', 'Belgien': '🇧🇪',
    'Bosnien-Herzegowina': '🇧🇦', 'Brasilien': '🇧🇷', 'DR Kongo': '🇨🇩',
    'Deutschland': '🇩🇪', 'Elfenbeinküste': '🇨🇮', 'Frankreich': '🇫🇷',
    'Irak': '🇮🇶', 'Jordanien': '🇯🇴', 'Kanada': '🇨🇦', 'Kap Verde': '🇨🇻',
    'Katar': '🇶🇦', 'Kolumbien': '🇨🇴', 'Kroatien': '🇭🇷', 'Marokko': '🇲🇦',
    'Mexiko': '🇲🇽', 'Neuseeland': '🇳🇿', 'Niederlande': '🇳🇱', 'Norwegen': '🇳🇴',
    'Österreich': '🇦🇹', 'Saudi-Arabien': '🇸🇦', 'Schweden': '🇸🇪',
    'Schweiz': '🇨🇭', 'Spanien': '🇪🇸', 'Südafrika': '🇿🇦', 'Südkorea': '🇰🇷',
    'Tschechien': '🇨🇿', 'Tunesien': '🇹🇳', 'Türkei': '🇹🇷',
    'Usbekistan': '🇺🇿', 'Ägypten': '🇪🇬',
}


def flag(name):
    return FLAG_MAP.get(name, name)

# --- CONFIGURATION ---
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_KEY = os.environ["API_FOOTBALL_KEY"]
USER_ID = int(os.environ["USER_ID"])
WC_2026_LEAGUE_ID = 1  # League ID for World Cup 2026
API_BASE_URL = "https://v3.football.api-sports.io"
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
            if not parsed or not parsed.get('response') or len(parsed['response']) == 0:
                 return None
        except:
            return None

        updated_at = datetime.fromisoformat(updated_at_str)
        if ttl_minutes == -1 or (datetime.now() - updated_at < timedelta(minutes=ttl_minutes)):
            return parsed
    return None

def save_cache(endpoint, data):
    if not data or not data.get('response') or len(data['response']) == 0:
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
async def fetch_api(session, endpoint, params=None, use_cache=False, ttl_minutes=60):
    param_str = json.dumps(params, sort_keys=True) if params else ""
    cache_key = f"{endpoint}_{param_str}"
    
    if use_cache:
        cached = get_cached_data(cache_key, ttl_minutes)
        if cached:
            return cached

    headers = {
        'x-rapidapi-key': API_KEY,
        'x-rapidapi-host': 'v3.football.api-sports.io'
    }
    async with session.get(f"{API_BASE_URL}/{endpoint}", headers=headers, params=params) as response:
        if response.status == 200:
            data = await response.json()
            if use_cache and data and data.get('response') and len(data['response']) > 0:
                save_cache(cache_key, data)
            return data
        else:
            logging.error(f"API Error {response.status}: {await response.text()}")
            return None

# --- NOTIFICATION LOGIC ---
async def safe_send(bot, text):
    try:
        await bot.send_message(USER_ID, text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Failed to send message to {USER_ID}: {e}")

async def broadcast_lineups(bot, session, fixture):
    fixture_id = fixture['fixture']['id']
    if is_processed('processed_lineups', fixture_id):
        return

    data = await fetch_api(session, "fixtures/lineups", {"fixture": fixture_id})
    if not data or not data.get('response'):
        return

    home_name = flag(fixture['teams']['home']['name'])
    away_name = flag(fixture['teams']['away']['name'])
    text = f"🏟 *التشكيلة: {home_name} ضد {away_name}*\n\n"
    for team_data in data['response']:
        text += f"*{flag(team_data['team']['name'])} ({team_data['formation']})*\n"
        players = ", ".join([p['player']['name'] for p in team_data['startXI']])
        text += f"XI: {players}\n\n"

    await safe_send(bot, text)
    mark_as_processed('processed_lineups', fixture_id)

async def check_live_events(bot, session, fixture_id):
    data = await fetch_api(session, "fixtures/events", {"fixture": fixture_id})
    if not data or not data.get('response'):
        return

    for event in data['response']:
        p_id = event['player'].get('id', '0')
        unique_id = f"{fixture_id}_{event['time']['elapsed']}_{event['type']}_{event['detail']}_{p_id}"
        if is_processed('processed_events', unique_id):
            continue

        emoji = "⚽" if event['type'] == 'Goal' else "🟨" if event['detail'] == 'Yellow Card' else "🟥" if event['detail'] == 'Red Card' else "🔄"
        msg = f"{emoji} *{event['type']} ({event['time']['elapsed']}')*\n"
        msg += f"{flag(event['team']['name'])}: {event['player']['name']}"
        if event['assist'].get('name'):
            msg += f" (Assist: {event['assist']['name']})"
        
        await safe_send(bot, msg)
        mark_as_processed('processed_events', unique_id)

async def broadcast_summary(bot, session, fixture):
    fixture_id = fixture['fixture']['id']
    if is_processed('processed_summaries', fixture_id):
        return

    stats_data = await fetch_api(session, "fixtures/statistics", {"fixture": fixture_id})
    if not stats_data or not stats_data.get('response'):
        return

    home_name = flag(fixture['teams']['home']['name'])
    away_name = flag(fixture['teams']['away']['name'])
    text = f"🏁 *نهاية المباراة: {home_name} {fixture['goals']['home']} - {fixture['goals']['away']} {away_name}*\n\n"
    text += "*إحصائيات المباراة:*\n"
    for team_stat in stats_data['response']:
        t_name = flag(team_stat['team']['name'])
        stats = {s['type']: s['value'] for s in team_stat['statistics']}
        text += f"_{t_name}_: التسديدات: {stats.get('Total Shots')}, الاستحواذ: {stats.get('Ball Possession')}, الركنيات: {stats.get('Corner Kicks')}\n"

    await safe_send(bot, text)
    mark_as_processed('processed_summaries', fixture_id)

# --- MAIN LOOPS ---
async def monitor_world_cup(bot):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                sleep_interval = 1800
                today = datetime.now().strftime('%Y-%m-%d')
                data = await fetch_api(session, "fixtures", {"league": WC_2026_LEAGUE_ID, "season": 2026, "date": today})
                
                if data and data.get('response'):
                    for fixture in data['response']:
                        status = fixture['fixture']['status']['short']
                        date_str = fixture['fixture']['date'].replace('Z', '+00:00')
                        kickoff = datetime.fromisoformat(date_str)
                        now = datetime.now(kickoff.tzinfo)

                        is_live = status in ['1H', 'HT', '2H', 'ET', 'P']
                        is_soon = status == 'NS' and (kickoff - now) < timedelta(minutes=45)

                        if is_soon:
                            await broadcast_lineups(bot, session, fixture)
                            sleep_interval = 60

                        if is_live:
                            await check_live_events(bot, session, fixture['fixture']['id'])
                            sleep_interval = 60

                        if status == 'FT':
                            await broadcast_summary(bot, session, fixture)

                logging.info(f"Polling check complete. Sleeping for {sleep_interval // 60} minutes.")
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
        await message.answer("مرحباً! بوت كأس العالم 2026 نشط حالياً. استخدم /help للمساعدة.")

    @dp.message(Command("help"))
    async def help_cmd(message: types.Message):
        builder = InlineKeyboardBuilder()
        current_date = TOURNAMENT_START_DATE
        today = datetime.now()
        
        while current_date <= today:
            date_str = current_date.strftime('%Y-%m-%d')
            btn_text = current_date.strftime('%b %d')
            builder.add(InlineKeyboardButton(text=btn_text, callback_data=f"date:{date_str}"))
            current_date += timedelta(days=1)
        
        builder.adjust(4)
        text = "🏆 *بوت كأس العالم 2026*\n\n/fixtures - مباريات اليوم\n/results - نتائج اليوم\n/goals <id> - أهداف المباراة\n/lineups <id> - التشكيلة\n/cards <id> - البطاقات\n\nاختر تاريخاً لعرض النتائج السابقة:"
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.callback_query(F.data.startswith("date:"))
    async def handle_date_selection(callback: CallbackQuery):
        selected_date_str = callback.data.split(":")[1]
        params = {"league": WC_2026_LEAGUE_ID, "season": 2026, "date": selected_date_str}
        
        async with aiohttp.ClientSession() as session:
            data = get_cached_data(f"fixtures_{json.dumps(params, sort_keys=True)}", ttl_minutes=-1)
            if not data:
                data = await fetch_api(session, "fixtures", params, use_cache=True, ttl_minutes=10)
            
            if data and data.get('response') and len(data['response']) > 0:
                all_finished = True
                for fixture in data['response']:
                    if fixture['fixture']['status']['short'] not in ['FT', 'AET', 'PEN']:
                        all_finished = False
                        break
                
                if all_finished:
                    save_cache(f"fixtures_{json.dumps(params, sort_keys=True)}", data)

                text = f"📅 *مباريات يوم {selected_date_str}:*\n\n"
                for f in data['response']:
                    home = flag(f['teams']['home']['name'])
                    away = flag(f['teams']['away']['name'])
                    status = f['fixture']['status']['short']
                    f_id = f['fixture']['id']
                    kickoff = riyadh_time(f['fixture']['date'])
                    
                    if status in ['FT', 'AET', 'PEN']:
                        score = f"{f['goals']['home']} - {f['goals']['away']}"
                        text += f"• `{f_id}`: {home} {score} {away} ({status})\n"
                    else:
                        text += f"• `{f_id}`: {home} vs {away} ({kickoff})\n"
                
                try:
                    await callback.message.edit_text(text, parse_mode="Markdown")
                except Exception:
                    await callback.message.answer(text, parse_mode="Markdown")
            else:
                await callback.answer(f"لا توجد مباريات لهذا اليوم.", show_alert=True)

    @dp.message(Command("fixtures"))
    async def cmd_fixtures(message: types.Message):
        today = datetime.now().strftime('%Y-%m-%d')
        async with aiohttp.ClientSession() as session:
            data = await fetch_api(session, "fixtures", {"league": WC_2026_LEAGUE_ID, "season": 2026, "date": today}, use_cache=True, ttl_minutes=60)
            
            if data and data.get('response'):
                text = "📅 *مباريات اليوم:*\n\n"
                for f in data['response']:
                    kickoff = riyadh_time(f['fixture']['date'])
                    text += f"ID: `{f['fixture']['id']}` | {kickoff} | {flag(f['teams']['home']['name'])} ضد {flag(f['teams']['away']['name'])} ({f['fixture']['status']['short']})\n"
                await message.answer(text, parse_mode="Markdown")
            else:
                await message.answer("لا توجد مباريات اليوم.")

    @dp.message(Command("results"))
    async def cmd_results(message: types.Message):
        today = datetime.now().strftime('%Y-%m-%d')
        async with aiohttp.ClientSession() as session:
            data = await fetch_api(session, "fixtures", {"league": WC_2026_LEAGUE_ID, "season": 2026, "date": today}, use_cache=True, ttl_minutes=720)
            
            if data and data.get('response'):
                results = [f for f in data['response'] if f['fixture']['status']['short'] in ['FT', 'AET', 'PEN']]
                if not results:
                    await message.answer("لم تنتهِ أي مباريات اليوم بعد.")
                    return
                
                text = "🏁 *نتائج اليوم:*\n\n"
                for f in results:
                    text += f"{flag(f['teams']['home']['name'])} {f['goals']['home']} - {f['goals']['away']} {flag(f['teams']['away']['name'])}\n"
                await message.answer(text, parse_mode="Markdown")
            else:
                await message.answer("لا توجد نتائج اليوم.")

    @dp.message(Command("goals"))
    async def cmd_goals(message: types.Message, command: CommandObject):
        if not command.args:
            await message.answer("Usage: `/goals <fixture_id>`", parse_mode="Markdown")
            return
        
        fixture_id = command.args
        async with aiohttp.ClientSession() as session:
            data = await fetch_api(session, "fixtures/events", {"fixture": fixture_id, "type": "Goal"}, use_cache=True, ttl_minutes=5)
            if data and data.get('response'):
                text = "⚽ *أهداف المباراة*\n\n"
                for event in data['response']:
                    text += f"• {event['time']['elapsed']}' - {flag(event['team']['name'])}: {event['player']['name']}\n"
                await message.answer(text, parse_mode="Markdown")
            else:
                await message.answer("لا توجد أهداف مسجلة لهذه المباراة.")

    @dp.message(Command("lineups"))
    async def cmd_lineups(message: types.Message, command: CommandObject):
        if not command.args:
            await message.answer("Usage: `/lineups <fixture_id>`", parse_mode="Markdown")
            return
        
        fixture_id = command.args
        async with aiohttp.ClientSession() as session:
            data = await fetch_api(session, "fixtures/lineups", {"fixture": fixture_id}, use_cache=True, ttl_minutes=1440)
            
            if data and data.get('response'):
                text = "🏟 *التشكيلة*\n\n"
                for team_data in data['response']:
                    text += f"*{flag(team_data['team']['name'])} ({team_data['formation']})*\n"
                    players = ", ".join([p['player']['name'] for p in team_data['startXI']])
                    text += f"XI: {players}\n\n"
                await message.answer(text, parse_mode="Markdown")
            else:
                await message.answer("التشكيلة غير متوفرة لهذا الرقم.")

    @dp.message(Command("cards"))
    async def cmd_cards(message: types.Message, command: CommandObject):
        if not command.args:
            await message.answer("Usage: `/cards <fixture_id>`", parse_mode="Markdown")
            return
        
        fixture_id = command.args
        async with aiohttp.ClientSession() as session:
            data = await fetch_api(session, "fixtures/events", {"fixture": fixture_id}, use_cache=True, ttl_minutes=5)
            if data and data.get('response'):
                cards = [e for e in data['response'] if e['type'] == 'Card']
                if not cards:
                    await message.answer("لا توجد بطاقات مسجلة لهذه المباراة.")
                    return
                
                text = "🟨 *البطاقات*\n\n"
                for event in cards:
                    emoji = "🟨" if event['detail'] == 'Yellow Card' else "🟥"
                    text += f"{emoji} {event['time']['elapsed']}' - {flag(event['team']['name'])}: {event['player']['name']}\n"
                await message.answer(text, parse_mode="Markdown")
            else:
                await message.answer("لا توجد بيانات متاحة لهذه المباراة.")

    await asyncio.gather(dp.start_polling(bot), monitor_world_cup(bot))

if __name__ == "__main__":
    asyncio.run(main())
