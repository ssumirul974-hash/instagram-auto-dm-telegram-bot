# Instagram Auto DM Bot (Telegram) — Button based

👨‍💻 Developed by Rohan 04

## Setup
1. @BotFather se token lo. @userinfobot se apna Telegram ID lo.
2. Env vars: `TELEGRAM_TOKEN`, `ADMIN_IDS` (kai admin: `111,222`), `ENCRYPTION_KEY` (Fernet key).
3. `pip install -r requirements.txt` → `python bot.py`  (Render: Start Command `python bot.py`)
4. Telegram me /start dabao — sab kuch buttons se hota hai.

## Use
Add Account (sessionid) → DM Message → Target List → Auto DM → START.
Extras: Single DM, Auto Reply (message requests bhi), 🎁 Delivery (DM/follow karne walon ko link/photo/video auto), 💭 Comment Reply (keyword + comment->DM), 📖 Story Reply, Welcome DM (naye followers), Schedule, Admin Panel (users, ban/approve, limit, broadcast, stats, logs, CSV).
Message tips: `{username}` = naam, `{hi|hello|hey}` = random word (ban risk kam).

Optional env: `COMMENT_LIMIT`(60) `DEVELOPER_NAME` (footer me naam, default Rohan 04 ) `DAILY_LIMIT`(40) `MAX_ACCOUNTS`(3) `REQUIRE_APPROVAL`(0/1) `MIN_DELAY`(45) `MAX_DELAY`(120) `TZ_NAME` `DATA_FILE`.

⚠️ Render free pe disk ephemeral: restart pe data ud jata hai.
⚠️ Zyada DM = Instagram ban. Limits mat badhao.
