# ⚽ World Cup 2026 Telegram Bot v2

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-3.x-2A2A2A?logo=telegram&logoColor=white)
![API-Football](https://img.shields.io/badge/API--Football-v3-00D9A3)
![SQLite](https://img.shields.io/badge/SQLite-3-003B57?logo=sqlite&logoColor=white)
![systemd](https://img.shields.io/badge/systemd-service-009639?logo=linux&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

A private **Telegram bot** that delivers real-time **FIFA World Cup 2026** match notifications — live events (goals, cards, substitutions), lineups, and full-time summaries with statistics. Built with `aiogram` 3.x and powered by the API-Football v3 API.

## ✨ Features

- **Live match monitoring** — Automatically polls for World Cup fixtures and tracks live status
- **Real-time event notifications:**
  - ⚽ Goals (with assist)
  - 🟨 Yellow / 🟥 Red cards
  - 🔄 Substitutions
- **Pre-match lineups** — Sends starting XI + formation 45 min before kickoff
- **Full-time summaries** — Final score + match statistics (shots, possession, corners)
- **Interactive commands:**
  - `/fixtures` — Today's matches with kickoff times (Riyadh TZ)
  - `/results` — Completed matches today
  - `/goals <id>` — Goal scorers for a specific match
  - `/lineups <id>` — Starting lineups for a match
  - `/cards <id>` — Booking records for a match
  - `/help` — Date picker keyboard for browsing past results
- **Smart caching** — SQLite-backed API response cache with per-endpoint TTLs
- **Dedup tracking** — Prevents duplicate notifications via processed-event IDs
- **Country flag mapping** — 50+ nations mapped to emoji flags (English + German API names)
- **systemd service** — Runs as a daemon with auto-restart

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Bot Framework | aiogram 3.x |
| HTTP Client | aiohttp (async) |
| Data Source | API-Football v3 |
| Storage | SQLite 3 |
| Deployment | systemd service on Linux |
| Time Zone | Asia/Riyadh (UTC+3) |

## 📦 Installation

```bash
# Clone the repository
git clone https://github.com/Fahad-BA/wc-2026-telegram-bot-v2.git
cd wc-2026-telegram-bot-v2

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install aiogram aiohttp

# Set your API key (or hardcode in bot.py)
export API_FOOTBALL_KEY="your_api_football_key"

# Run the bot
python bot.py
```

### systemd Deployment

```bash
# Copy the service file
sudo cp wc_bot.service /etc/systemd/system/

# Edit paths if needed
sudo nano /etc/systemd/system/wc_bot.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable wc_bot
sudo systemctl start wc_bot

# Check status
sudo systemctl status wc_bot
```

## ⚙️ Configuration

Edit the following constants in `bot.py`:

```python
BOT_TOKEN = "your_telegram_bot_token"
API_KEY = os.getenv("API_FOOTBALL_KEY", "your_api_football_key")
USER_ID = 123456789           # Your Telegram user ID
WC_2026_LEAGUE_ID = 1          # World Cup league ID on API-Football
```

## 📁 Structure

```
wc-2026-telegram-bot-v2/
├── bot.py               # Main bot logic (async polling + monitor loop)
├── wc_bot.service       # systemd unit file
├── bot.db               # SQLite DB (auto-created, gitignored)
└── .gitignore
```

## 📄 License

MIT — Free to use and modify.

> **Note:** This bot is configured for private use (single-user DM). Modify `USER_ID` or extend with chat registration for multi-user support.
