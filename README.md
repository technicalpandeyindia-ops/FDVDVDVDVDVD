# Course Extractor Bot

Telegram bot that extracts and downloads full courses from StudyBee/ClassX and sends them to any group or channel.

## Commands
- `/start` — intro
- `/download` — start extraction flow (asks course ID, then target chat)
- `/cancel` — cancel current flow

## Deploy on Render

### 1. Create bot
- Go to [@BotFather](https://t.me/BotFather) → `/newbot` → copy token

### 2. Push to GitHub
```bash
git init
git add .
git commit -m "init coursebot"
git remote add origin https://github.com/YOURNAME/coursebot.git
git push -u origin main
```

### 3. Deploy on Render
1. Go to https://render.com → New → Web Service
2. Connect your GitHub repo
3. Set these env vars in Render dashboard:
   - `BOT_TOKEN` = your telegram bot token
   - `QUALITY` = `1080p` / `720p` / `480p` (default: 720p)
   - `STUDYBEE_COOKIE` = your login cookie (optional, if site needs auth)
4. Build command (auto from render.yaml):
   ```
   pip install -r requirements.txt && apt-get install -y ffmpeg
   ```
5. Start command: `python bot.py`

### 4. Add bot as admin
In your target group/channel → Add admin → your bot → allow "Post messages"

### 5. Use
1. DM your bot `/download`
2. Enter course ID (e.g. `41`)
3. Enter target channel ID (e.g. `-1001234567890` or `@yourchannel`)
4. Bot downloads everything and sends to the channel, keeps you updated in DM

## Notes
- Videos are sent as Telegram videos (streaming supported)
- Files >2GB are sent as documents
- PDFs sent as documents
- Progress + ETA reported in your DM
- Bot auto-resumes on Render restart (just re-run `/download`)
