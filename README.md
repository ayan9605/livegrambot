# Livegram Clone Bot

A Railway-ready Telegram Livegram-style bot. The main bot can receive `/clone YOUR_BOT_TOKEN` in private chat and start a clone bot for that user.

## Features

- Runs with `python-telegram-bot`
- Uses Railway environment variables instead of hard-coded secrets
- `/clone YOUR_BOT_TOKEN` starts a user-owned clone
- `/myclones` lists the user's clones
- `/removeclone USERNAME_OR_ID` stops and removes a clone
- Admin reply flow with inline Reply buttons

## Railway Deploy

1. Create the factory bot with [@BotFather](https://t.me/BotFather).
2. Deploy this public GitHub repo on Railway with "New Project" then "Deploy from GitHub repo".
3. Add these Railway variables before the first deploy:

```env
BOT_TOKEN=your_factory_bot_token
ADMIN_IDS=your_telegram_user_id
FORWARD_CHAT_ID=your_telegram_user_id
ENABLE_CLONING=true
CLONES_FILE=clones.json
```

Railway will use `railway.json` and start the app with:

```bash
python main.py
```

## How Users Clone

1. The user creates a new bot token with [@BotFather](https://t.me/BotFather).
2. The user opens your factory bot in private chat.
3. The user sends:

```text
/clone 123456789:AA...
```

4. The factory bot starts that token as a clone.
5. The user opens their new clone bot and presses `/start` so the clone bot can message their admin inbox.

## Important Security Note

Anyone who sends a bot token to your factory bot is trusting your Railway deployment with control of that bot. Do not log or publish tokens. This repo ignores `.env` and `clones.json`, but the running service still stores clone tokens so clones can restart.

For production persistence on Railway, mount a Railway volume and set `CLONES_FILE` to a path inside that volume, for example `/data/clones.json`. Without persistent storage, clones may disappear after redeploys or restarts.

## Local Run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python main.py
```

Set real values in `.env` or export the same variables in your shell before running.
