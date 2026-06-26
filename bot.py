import asyncio
import logging
import sqlite3
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from datetime import datetime, timedelta

# --- CONFIGURATION ---
BOT_TOKEN = "8876705370:AAEGmWOMaTjOflAy7myAWMouVoBn8_kHais"
API_KEY = "d74ed4898dd30d9f491f2e33e6a6abbe"
USER_ID = 697241718  # Direct messages to this User ID
WC_2026_LEAGUE_ID = 1  # League ID for World Cup 2026
API_BASE_URL = "https://v3.football.api-sports.io"

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY)')
    cursor.execute('CREATE TABLE IF NOT EXISTS processed_lineups (fixture_id INTEGER PRIMARY KEY)')
    cursor.execute('CREATE TABLE IF NOT EXISTS processed_summaries (fixture_id INTEGER PRIMARY KEY)')
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

# --- API HELPERS ---
async def fetch_api(session, endpoint, params=None):
    headers = {
        'x-rapidapi-key': API_KEY,
        'x-rapidapi-host': 'v3.football.api-sports.io'
    }
    async with session.get(f"{API_BASE_URL}/{endpoint}", headers=headers, params=params) as response:
        if response.status == 200:
            return await response.json()
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
        await message.answer("World Cup 2026 Bot active! You will now receive match updates.")

    # Run monitor and bot polling together
    await asyncio.gather(dp.start_polling(bot), monitor_world_cup(bot))

if __name__ == "__main__":
    asyncio.run(main())
