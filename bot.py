import asyncio
import logging
import sqlite3
import aiohttp
import json
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime, timedelta

# --- CONFIGURATION ---
BOT_TOKEN = "8876705370:AAEGmWOMaTjOflAy7myAWMouVoBn8_kHais"
API_KEY = "d74ed4898dd30d9f491f2e33e6a6abbe"
USER_ID = 697241718  # Direct messages to this User ID
WC_2026_LEAGUE_ID = 1  # League ID for World Cup 2026
API_BASE_URL = "https://v3.football.api-sports.io"
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
        updated_at = datetime.fromisoformat(updated_at_str)
        # Use a very large TTL for "infinite" caching
        if ttl_minutes == -1 or (datetime.now() - updated_at < timedelta(minutes=ttl_minutes)):
            return json.loads(data_str)
    return None

def save_cache(endpoint, data):
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
    cache_key = f"{endpoint}_{json.dumps(params, sort_keys=True)}" if params else endpoint
    
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
            if use_cache and data:
                # Check for "No data" or empty responses from API-Football
                # API-Football often returns an empty list in 'response' when no data exists
                has_data = data.get('response') and len(data['response']) > 0
                
                # NEVER cache "empty" data forever
                if not has_data and ttl_minutes == -1:
                    save_cache(cache_key, data) # Still save it, but don't treat it as infinite in the logic below
                else:
                    save_cache(cache_key, data)
            return data
        return None

# --- NOTIFICATION LOGIC ---
async def safe_send(bot, text):
    try:
        await bot.send_message(USER_ID, text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Failed to send message to {USER_ID}: {e}. Make sure the user has /start-ed the bot.")

async def broadcast_lineups(bot, session, fixture):
    fixture_id = fixture['fixture']['id']
    if is_processed('processed_lineups', fixture_id):
        return

    data = await fetch_api(session, "fixtures/lineups", {"fixture": fixture_id})
    if not data or not data.get('response'):
        return

    text = f"🏟 *Lineups: {fixture['teams']['home']['name']} vs {fixture['teams']['away']['name']}*\n\n"
    for team_data in data['response']:
        text += f"*{team_data['team']['name']} ({team_data['formation']})*\n"
        players = ", ".join([p['player']['name'] for p in team_data['startXI']])
        text += f"XI: {players}\n\n"

    await safe_send(bot, text)
    mark_as_processed('processed_lineups', fixture_id)

async def check_live_events(bot, session, fixture_id):
    data = await fetch_api(session, "fixtures/events", {"fixture": fixture_id})
    if not data or not data.get('response'):
        return

    for event in data['response']:
        unique_id = f"{fixture_id}_{event['time']['elapsed']}_{event['type']}_{event['detail']}_{event['player']['id']}"
        if is_processed('processed_events', unique_id):
            continue

        emoji = "⚽" if event['type'] == 'Goal' else "🟨" if event['detail'] == 'Yellow Card' else "🟥" if event['detail'] == 'Red Card' else "🔄"
        msg = f"{emoji} *{event['type']} ({event['time']['elapsed']}')*\n"
        msg += f"{event['team']['name']}: {event['player']['name']}"
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

    text = f"🏁 *Full Time: {fixture['teams']['home']['name']} {fixture['goals']['home']} - {fixture['goals']['away']} {fixture['teams']['away']['name']}*\n\n"
    text += "*Match Statistics:*\n"
    for team_stat in stats_data['response']:
        t_name = team_stat['team']['name']
        stats = {s['type']: s['value'] for s in team_stat['statistics']}
        text += f"_{t_name}_: Shots: {stats.get('Total Shots')}, Possession: {stats.get('Ball Possession')}, Corners: {stats.get('Corner Kicks')}\n"

    await safe_send(bot, text)
    mark_as_processed('processed_summaries', fixture_id)

# --- MAIN LOOPS ---
async def monitor_world_cup(bot):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Default sleep interval is 30 minutes (1800 seconds)
                sleep_interval = 1800
                today = datetime.now().strftime('%Y-%m-%d')
                data = await fetch_api(session, "fixtures", {"league": WC_2026_LEAGUE_ID, "season": 2026, "date": today})
                
                if data and data.get('response'):
                    for fixture in data['response']:
                        status = fixture['fixture']['status']['short']
                        kickoff = datetime.fromisoformat(fixture['fixture']['date'].replace('Z', '+00:00'))
                        now = datetime.now(kickoff.tzinfo)

                        is_live = status in ['1H', 'HT', '2H', 'ET', 'P']
                        is_soon = status == 'NS' and (kickoff - now) < timedelta(minutes=45)

                        if is_soon:
                            await broadcast_lineups(bot, session, fixture)
                            sleep_interval = 60  # Switch to 1 minute polling

                        if is_live:
                            await check_live_events(bot, session, fixture['fixture']['id'])
                            sleep_interval = 60  # Switch to 1 minute polling

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
        await message.answer("World Cup 2026 Bot active! Use /help to see all commands and match history.")

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
        text = (
            "🏆 *World Cup 2026 Bot Help*\n\n"
            "Commands:\n"
            "/fixtures - Today's matches\n"
            "/results - Today's final scores\n"
            "/lineups <fixture_id> - Get XI\n"
            "/cards <fixture_id> - Get cards\n\n"
            "Select a date below to view match history:"
        )
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.callback_query(F.data.startswith("date:"))
    async def handle_date_selection(callback: CallbackQuery):
        selected_date_str = callback.data.split(":")[1]
        
        async with aiohttp.ClientSession() as session:
            # Fix: Ensure query params are exactly as API-Sports expects (league, season, date)
            params = {"league": WC_2026_LEAGUE_ID, "season": 2026, "date": selected_date_str}
            
            # 1. First fetch with a short TTL to verify data existence and status
            # This avoids "infinite" caching of errors or empty results
            temp_data = await fetch_api(session, "fixtures", params, use_cache=True, ttl_minutes=5)
            
            all_finished = True
            has_data = False
            if temp_data and temp_data.get('response') and len(temp_data['response']) > 0:
                has_data = True
                for fixture in temp_data['response']:
                    if fixture['fixture']['status']['short'] not in ['FT', 'AET', 'PEN']:
                        all_finished = False
                        break
            else:
                all_finished = False
            
            # Fix: NEVER use infinite TTL (-1) if matches were empty or missing
            # Only use it if we have verified matches and they are ALL finished
            final_ttl = -1 if (has_data and all_finished) else 5
            
            data = await fetch_api(session, "fixtures", params, use_cache=True, ttl_minutes=final_ttl)
            
            if data and data.get('response') and len(data['response']) > 0:
                text = f"📅 *Matches on {selected_date_str}:*\n\n"
                for f in data['response']:
                    home = f['teams']['home']['name']
                    away = f['teams']['away']['name']
                    status = f['fixture']['status']['short']
                    f_id = f['fixture']['id']
                    
                    if status in ['FT', 'AET', 'PEN']:
                        score = f"{f['goals']['home']} - {f['goals']['away']}"
                        text += f"• `{f_id}`: {home} {score} {away} ({status})\n"
                    else:
                        text += f"• `{f_id}`: {home} vs {away} ({status})\n"
                
                await callback.message.edit_text(text, parse_mode="Markdown")
            else:
                await callback.answer(f"No match data found for {selected_date_str}.", show_alert=True)

    @dp.message(Command("fixtures"))
    async def cmd_fixtures(message: types.Message):
        today = datetime.now().strftime('%Y-%m-%d')
        async with aiohttp.ClientSession() as session:
            data = await fetch_api(session, "fixtures", {"league": WC_2026_LEAGUE_ID, "season": 2026, "date": today}, use_cache=True, ttl_minutes=60)
            
            if data and data.get('response'):
                text = "📅 *Today's Fixtures:*\n\n"
                for f in data['response']:
                    text += f"ID: `{f['fixture']['id']}` | {f['teams']['home']['name']} vs {f['teams']['away']['name']} ({f['fixture']['status']['short']})\n"
                await message.answer(text, parse_mode="Markdown")
            else:
                await message.answer("No fixtures found for today.")

    @dp.message(Command("results"))
    async def cmd_results(message: types.Message):
        today = datetime.now().strftime('%Y-%m-%d')
        async with aiohttp.ClientSession() as session:
            data = await fetch_api(session, "fixtures", {"league": WC_2026_LEAGUE_ID, "season": 2026, "date": today}, use_cache=True, ttl_minutes=720)
            
            if data and data.get('response'):
                results = [f for f in data['response'] if f['fixture']['status']['short'] in ['FT', 'AET', 'PEN']]
                if not results:
                    await message.answer("No completed matches yet today.")
                    return
                
                text = "🏁 *Today's Results:*\n\n"
                for f in results:
                    text += f"{f['teams']['home']['name']} {f['goals']['home']} - {f['goals']['away']} {f['teams']['away']['name']}\n"
                await message.answer(text, parse_mode="Markdown")
            else:
                await message.answer("No results found.")

    @dp.message(Command("lineups"))
    async def cmd_lineups(message: types.Message, command: CommandObject):
        if not command.args:
            await message.answer("Usage: `/lineups <fixture_id>`", parse_mode="Markdown")
            return
        
        fixture_id = command.args
        async with aiohttp.ClientSession() as session:
            data = await fetch_api(session, "fixtures/lineups", {"fixture": fixture_id}, use_cache=True, ttl_minutes=1440)
            
            if data and data.get('response'):
                text = "🏟 *Lineups*\n\n"
                for team_data in data['response']:
                    text += f"*{team_data['team']['name']} ({team_data['formation']})*\n"
                    players = ", ".join([p['player']['name'] for p in team_data['startXI']])
                    text += f"XI: {players}\n\n"
                await message.answer(text, parse_mode="Markdown")
            else:
                await message.answer("Lineups not available for this fixture ID.")

    @dp.message(Command("cards"))
    async def cmd_cards(message: types.Message, command: CommandObject):
        if not command.args:
            await message.answer("Usage: `/cards <fixture_id>`", parse_mode="Markdown")
            return
        
        fixture_id = command.args
        async with aiohttp.ClientSession() as session:
            fix_data = await fetch_api(session, "fixtures", {"id": fixture_id}, use_cache=True, ttl_minutes=5)
            ttl = 1
            if fix_data and fix_data.get('response'):
                status = fix_data['response'][0]['fixture']['status']['short']
                if status in ['FT', 'AET', 'PEN']:
                    ttl = 720
            
            data = await fetch_api(session, "fixtures/events", {"fixture": fixture_id}, use_cache=True, ttl_minutes=ttl)
            
            if data and data.get('response'):
                cards = [e for e in data['response'] if e['type'] == 'Card']
                if not cards:
                    await message.answer("No cards reported for this match.")
                    return
                
                text = "🟨 *Match Cards*\n\n"
                for event in cards:
                    emoji = "🟨" if event['detail'] == 'Yellow Card' else "🟥"
                    text += f"{emoji} {event['time']['elapsed']}' - {event['team']['name']}: {event['player']['name']}\n"
                await message.answer(text, parse_mode="Markdown")
            else:
                await message.answer("No event data available.")

    await asyncio.gather(dp.start_polling(bot), monitor_world_cup(bot))

if __name__ == "__main__":
    asyncio.run(main())
