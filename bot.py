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
USER_ID = 697241718  # Direct messages to this User ID
FOTMOB_WC_LEAGUE_ID = 77  # FotMob League ID for World Cup
TOURNAMENT_START_DATE = datetime(2026, 6, 11)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"

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
async def fetch_fotmob(session, endpoint, params=None, use_cache=False, ttl_minutes=60):
    param_str = json.dumps(params, sort_keys=True) if params else ""
    cache_key = f"fotmob_{endpoint}_{param_str}"
    
    if use_cache:
        cached = get_cached_data(cache_key, ttl_minutes)
        if cached:
            return cached

    url = f"https://www.fotmob.com/api/{endpoint}"
    headers = { 'User-Agent': USER_AGENT }
    
    async with session.get(url, headers=headers, params=params) as response:
        if response.status == 200:
            data = await response.json()
            if use_cache and data:
                save_cache(cache_key, data)
            return data
        else:
            logging.error(f"FotMob API Error {response.status} for {endpoint}")
            return None

# --- NOTIFICATION LOGIC ---
async def safe_send(bot, text):
    try:
        await bot.send_message(USER_ID, text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Failed to send message to {USER_ID}: {e}")

async def broadcast_lineups(bot, session, match_id, home_name, away_name):
    if is_processed('processed_lineups', match_id):
        return

    data = await fetch_fotmob(session, "matchDetails", {"matchId": match_id})
    if not data or 'content' not in data or 'lineup' not in data['content']:
        return

    lineup_data = data['content']['lineup']
    text = f"🏟 *Lineups: {home_name} vs {away_name}*\n\n"
    
    for team_key in ['home', 'away']:
        team = lineup_data.get(team_key, {})
        if not team: continue
        t_name = team.get('teamName', 'Unknown')
        formation = team.get('formation', '')
        text += f"*{t_name} ({formation})*\n"
        
        # FotMob nested players
        players_list = []
        for line in team.get('lineup', []):
            for player in line:
                players_list.append(player.get('name', {}).get('full', 'Unknown'))
        
        text += f"XI: {', '.join(players_list[:11])}\n\n"

    await safe_send(bot, text)
    mark_as_processed('processed_lineups', match_id)

async def check_live_events(bot, session, match_id):
    data = await fetch_fotmob(session, "matchDetails", {"matchId": match_id})
    if not data or 'content' not in data or 'matchFacts' not in data['content']:
        return

    events = data['content']['matchFacts'].get('events', {}).get('events', [])
    for event in events:
        if event.get('type') not in ['Goal', 'Card']:
            continue
            
        e_type = event['type']
        e_id = f"{match_id}_{event.get('eventId') or event.get('time')}"
        if is_processed('processed_events', e_id):
            continue

        time = event.get('time', '??')
        team = event.get('teamName', '')
        player = event.get('playerName', '')
        detail = event.get('card', event.get('type'))
        
        emoji = "⚽" if e_type == 'Goal' else "🟨" if detail == 'Yellow' else "🟥" if detail == 'Red' else "🔄"
        msg = f"{emoji} *{e_type} ({time}')*\n{team}: {player}"
        
        await safe_send(bot, msg)
        mark_as_processed('processed_events', e_id)

async def broadcast_summary(bot, session, match_id, home_name, away_name, score):
    if is_processed('processed_summaries', match_id):
        return

    data = await fetch_fotmob(session, "matchDetails", {"matchId": match_id})
    if not data or 'content' not in data or 'stats' not in data['content']:
        return

    text = f"🏁 *Full Time: {home_name} {score} {away_name}*\n\n"
    text += "*Match Statistics:*\n"
    
    stats_list = data['content']['stats'].get('stats', [])
    if stats_list:
        for group in stats_list:
            for stat in group.get('stats', []):
                if stat.get('title') in ['Total shots', 'Ball possession', 'Corner kicks']:
                    title = stat['title']
                    h_val, a_val = stat.get('stats', [0, 0])
                    text += f"• {title}: {h_val} - {a_val}\n"

    await safe_send(bot, text)
    mark_as_processed('processed_summaries', match_id)

# --- MAIN LOOPS ---
async def monitor_world_cup(bot):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                sleep_interval = 1800
                today_str = datetime.now().strftime('%Y%m%d')
                data = await fetch_fotmob(session, "matches", {"date": today_str})
                
                if data and 'leagues' in data:
                    wc_league = next((l for l in data['leagues'] if l['id'] == FOTMOB_WC_LEAGUE_ID), None)
                    if wc_league:
                        for m in wc_league.get('matches', []):
                            match_id = m['id']
                            status = m['status']
                            is_live = status.get('live', False)
                            is_finished = status.get('finished', False)
                            is_soon = not is_live and not is_finished and status.get('started', False) == False
                            
                            home = m['home']['name']
                            away = m['away']['name']
                            score = f"{m['home'].get('score', 0)} - {m['away'].get('score', 0)}"

                            if is_soon:
                                # Start checking for lineups 45m before
                                await broadcast_lineups(bot, session, match_id, home, away)
                                sleep_interval = 300 # Speed up

                            if is_live:
                                await check_live_events(bot, session, match_id)
                                sleep_interval = 60

                            if is_finished:
                                await broadcast_summary(bot, session, match_id, home, away, score)

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
        await message.answer("World Cup 2026 Bot (FotMob Engine) active! Use /help for commands.")

    @dp.message(Command("help"))
    async def help_cmd(message: types.Message):
        builder = InlineKeyboardBuilder()
        curr = TOURNAMENT_START_DATE
        today = datetime.now()
        while curr <= today:
            d_str = curr.strftime('%Y%m%d')
            btn_text = curr.strftime('%b %d')
            builder.add(InlineKeyboardButton(text=btn_text, callback_data=f"fdate:{d_str}"))
            curr += timedelta(days=1)
        builder.adjust(4)
        text = "🏆 *World Cup 2026 Bot Help*\n\n/fixtures - Today\n/results - Today's scores\n/lineups <id>\n/cards <id>\n\nSelect a date:"
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.callback_query(F.data.startswith("fdate:"))
    async def handle_date_selection(callback: CallbackQuery):
        date_str = callback.data.split(":")[1]
        async with aiohttp.ClientSession() as session:
            data = await fetch_fotmob(session, "matches", {"date": date_str}, use_cache=True, ttl_minutes=10)
            if data and 'leagues' in data:
                wc = next((l for l in data['leagues'] if l['id'] == FOTMOB_WC_LEAGUE_ID), None)
                if wc and wc.get('matches'):
                    text = f"📅 *Matches on {date_str}:*\n\n"
                    all_done = True
                    for m in wc['matches']:
                        res = f"{m['home'].get('score','?')} - {m['away'].get('score','?')}"
                        status = "FT" if m['status'].get('finished') else "Live" if m['status'].get('live') else "NS"
                        text += f"• `{m['id']}`: {m['home']['name']} {res} {m['away']['name']} ({status})\n"
                        if not m['status'].get('finished'): all_done = False
                    
                    if all_done: # Infinite cache upgrade
                        save_cache(f"fotmob_matches_{json.dumps({'date': date_str}, sort_keys=True)}", data)
                    await callback.message.edit_text(text, parse_mode="Markdown")
                    return
            await callback.answer("No matches found.")

    @dp.message(Command("fixtures"))
    async def cmd_fixtures(message: types.Message):
        today = datetime.now().strftime('%Y%m%d')
        async with aiohttp.ClientSession() as session:
            data = await fetch_fotmob(session, "matches", {"date": today}, use_cache=True, ttl_minutes=30)
            if data and 'leagues' in data:
                wc = next((l for l in data['leagues'] if l['id'] == FOTMOB_WC_LEAGUE_ID), None)
                if wc and wc.get('matches'):
                    text = "📅 *Today's Fixtures:*\n\n"
                    for m in wc['matches']:
                        text += f"ID: `{m['id']}` | {m['home']['name']} vs {m['away']['name']}\n"
                    await message.answer(text, parse_mode="Markdown")
                    return
            await message.answer("No fixtures today.")

    @dp.message(Command("results"))
    async def cmd_results(message: types.Message):
        today = datetime.now().strftime('%Y%m%d')
        async with aiohttp.ClientSession() as session:
            data = await fetch_fotmob(session, "matches", {"date": today}, use_cache=True, ttl_minutes=30)
            if data and 'leagues' in data:
                wc = next((l for l in data['leagues'] if l['id'] == FOTMOB_WC_LEAGUE_ID), None)
                if wc and wc.get('matches'):
                    text = "🏁 *Today's Results:*\n\n"
                    found = False
                    for m in wc['matches']:
                        if m['status'].get('finished'):
                            found = True
                            text += f"{m['home']['name']} {m['home']['score']} - {m['away']['score']} {m['away']['name']}\n"
                    if found:
                        await message.answer(text, parse_mode="Markdown")
                        return
            await message.answer("No completed matches yet.")

    @dp.message(Command("lineups"))
    async def cmd_lineups(message: types.Message, command: CommandObject):
        if not command.args:
            await message.answer("Usage: `/lineups <id>`", parse_mode="Markdown")
            return
        async with aiohttp.ClientSession() as session:
            data = await fetch_fotmob(session, "matchDetails", {"matchId": command.args}, use_cache=True, ttl_minutes=1440)
            if data and 'content' in data and 'lineup' in data['content']:
                text = "🏟 *Lineups*\n\n"
                for tk in ['home', 'away']:
                    t = data['content']['lineup'].get(tk, {})
                    text += f"*{t.get('teamName')}*\nXI: "
                    pl = []
                    for line in t.get('lineup', []):
                        for p in line: pl.append(p['name']['full'])
                    text += f"{', '.join(pl[:11])}\n\n"
                await message.answer(text, parse_mode="Markdown")
                return
            await message.answer("Lineups not found.")

    @dp.message(Command("cards"))
    async def cmd_cards(message: types.Message, command: CommandObject):
        if not command.args:
            await message.answer("Usage: `/cards <id>`", parse_mode="Markdown")
            return
        async with aiohttp.ClientSession() as session:
            data = await fetch_fotmob(session, "matchDetails", {"matchId": command.args}, use_cache=True, ttl_minutes=5)
            if data and 'content' in data and 'matchFacts' in data['content']:
                events = data['content']['matchFacts'].get('events', {}).get('events', [])
                cards = [e for e in events if e.get('type') == 'Card']
                if cards:
                    text = "🟨 *Match Cards*\n\n"
                    for e in cards:
                        text += f"• {e['time']}' - {e['teamName']}: {e['playerName']} ({e.get('card','')})\n"
                    await message.answer(text, parse_mode="Markdown")
                    return
            await message.answer("No cards found.")

    await asyncio.gather(dp.start_polling(bot), monitor_world_cup(bot))

if __name__ == "__main__":
    asyncio.run(main())
