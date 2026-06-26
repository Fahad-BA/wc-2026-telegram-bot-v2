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
from datetime import datetime, timedelta

# --- CONFIGURATION ---
BOT_TOKEN = "BOT_TOKEN_REMOVED"
USER_ID = 697241718  # Direct messages to this User ID
FD_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "YOUR_FALLBACK_API_KEY_HERE")
FD_WC_LEAGUE_CODE = "WC" # World Cup league code for football-data.org
API_BASE_URL = "https://api.football-data.org/v4"
TOURNAMENT_START_DATE = datetime(2026, 6, 11)

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
async def fetch_fd(session, endpoint, params=None, use_cache=False, ttl_minutes=60):
    param_str = json.dumps(params, sort_keys=True) if params else ""
    cache_key = f"fd_{endpoint}_{param_str}"
    
    if use_cache:
        cached = get_cached_data(cache_key, ttl_minutes)
        if cached:
            return cached

    url = f"{API_BASE_URL}/{endpoint}"
    headers = { 'X-Auth-Token': FD_API_KEY }
    
    async with session.get(url, headers=headers, params=params) as response:
        if response.status == 200:
            data = await response.json()
            if use_cache and data:
                save_cache(cache_key, data)
            return data
        else:
            logging.error(f"Football-Data API Error {response.status} for {endpoint}")
            return None

# --- NOTIFICATION LOGIC ---
async def safe_send(bot, text):
    try:
        await bot.send_message(USER_ID, text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Failed to send message to {USER_ID}: {e}")

async def broadcast_summary(bot, session, match):
    match_id = match['id']
    if is_processed('processed_summaries', match_id):
        return

    home_name = match['homeTeam']['name']
    away_name = match['awayTeam']['name']
    score_data = match['score']['fullTime']
    score = f"{score_data['home']} - {score_data['away']}"

    text = f"🏁 *Full Time: {home_name} {score} {away_name}*\n\n"
    text += "Note: Detailed match statistics are restricted on the current API tier."

    await safe_send(bot, text)
    mark_as_processed('processed_summaries', match_id)

# --- MAIN LOOPS ---
async def monitor_world_cup(bot):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                sleep_interval = 1800
                today_str = datetime.now().strftime('%Y-%m-%d')
                # Football-Data uses competitions/WC/matches?dateFrom=...&dateTo=...
                data = await fetch_fd(session, f"competitions/{FD_WC_LEAGUE_CODE}/matches", {"dateFrom": today_str, "dateTo": today_str})
                
                if data and 'matches' in data:
                    for m in data['matches']:
                        status = m['status']
                        is_live = status in ['IN_PLAY', 'PAUSED']
                        is_finished = status == 'FINISHED'
                        
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
        await message.answer("World Cup 2026 Bot (Football-Data Engine) active! Use /help for commands.")

    @dp.message(Command("help"))
    async def help_cmd(message: types.Message):
        builder = InlineKeyboardBuilder()
        curr = TOURNAMENT_START_DATE
        today = datetime.now()
        while curr <= today:
            d_str = curr.strftime('%Y-%m-%d')
            btn_text = curr.strftime('%b %d')
            builder.add(InlineKeyboardButton(text=btn_text, callback_data=f"fddate:{d_str}"))
            curr += timedelta(days=1)
        builder.adjust(4)
        text = "🏆 *World Cup 2026 Bot Help*\n\n/fixtures - Today\n/results - Today's scores\n/lineups <id>\n/cards <id>\n\nSelect a date:"
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.callback_query(F.data.startswith("fddate:"))
    async def handle_date_selection(callback: CallbackQuery):
        date_str = callback.data.split(":")[1]
        async with aiohttp.ClientSession() as session:
            data = await fetch_fd(session, f"competitions/{FD_WC_LEAGUE_CODE}/matches", {"dateFrom": date_str, "dateTo": date_str}, use_cache=True, ttl_minutes=10)
            if data and 'matches' in data:
                text = f"📅 *Matches on {date_str}:*\n\n"
                all_done = True
                for m in data['matches']:
                    res_h = m['score']['fullTime'].get('home', '?')
                    res_a = m['score']['fullTime'].get('away', '?')
                    res = f"{res_h} - {res_a}"
                    status = m['status']
                    text += f"• `{m['id']}`: {m['homeTeam']['name']} {res} {m['awayTeam']['name']} ({status})\n"
                    if status != 'FINISHED': all_done = False
                
                if all_done: # Infinite cache upgrade
                    save_cache(f"fd_competitions/{FD_WC_LEAGUE_CODE}/matches_{json.dumps({'dateFrom': date_str, 'dateTo': date_str}, sort_keys=True)}", data)
                await callback.message.edit_text(text, parse_mode="Markdown")
                return
            await callback.answer("No matches found.")

    @dp.message(Command("fixtures"))
    async def cmd_fixtures(message: types.Message):
        today = datetime.now().strftime('%Y-%m-%d')
        async with aiohttp.ClientSession() as session:
            data = await fetch_fd(session, f"competitions/{FD_WC_LEAGUE_CODE}/matches", {"dateFrom": today, "dateTo": today}, use_cache=True, ttl_minutes=30)
            if data and 'matches' in data:
                text = "📅 *Today's Fixtures:*\n\n"
                for m in data['matches']:
                    text += f"ID: `{m['id']}` | {m['homeTeam']['name']} vs {m['awayTeam']['name']} ({m['status']})\n"
                await message.answer(text, parse_mode="Markdown")
                return
            await message.answer("No fixtures today.")

    @dp.message(Command("results"))
    async def cmd_results(message: types.Message):
        today = datetime.now().strftime('%Y-%m-%d')
        async with aiohttp.ClientSession() as session:
            data = await fetch_fd(session, f"competitions/{FD_WC_LEAGUE_CODE}/matches", {"dateFrom": today, "dateTo": today}, use_cache=True, ttl_minutes=30)
            if data and 'matches' in data:
                text = "🏁 *Today's Results:*\n\n"
                found = False
                for m in data['matches']:
                    if m['status'] == 'FINISHED':
                        found = True
                        score = m['score']['fullTime']
                        text += f"{m['homeTeam']['name']} {score['home']} - {score['away']} {m['awayTeam']['name']}\n"
                if found:
                    await message.answer(text, parse_mode="Markdown")
                    return
            await message.answer("No completed matches yet.")

    @dp.message(Command("lineups"))
    async def cmd_lineups(message: types.Message, command: CommandObject):
        await message.answer("🏟 *Lineups*\n\nLineup data is currently limited on the Free tier of the Football-Data API.", parse_mode="Markdown")

    @dp.message(Command("cards"))
    async def cmd_cards(message: types.Message, command: CommandObject):
        await message.answer("🟨 *Match Cards*\n\nDetailed card and event data is currently limited on the Free tier of the Football-Data API.", parse_mode="Markdown")

    await asyncio.gather(dp.start_polling(bot), monitor_world_cup(bot))

if __name__ == "__main__":
    asyncio.run(main())
