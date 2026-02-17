# 🤖 Telegram Autoposter & Auto-Approve Bot

A powerful and optimized Telegram bot built with Python and Pyrogram to automate message forwarding between channels and automatically approve join requests.

## 🚀 Features

### 📬 Autoposter
- **Automated Forwarding**: Forward posts from any source channel to a target channel.
- **Batch Processing**: Forward messages in configurable batches to avoid rate limits.
- **Filtering**: Choose to forward only media, only text, or all posts.
- **Custom Captions**: Add your own captions to forwarded posts with HTML formatting support.
- **Inline Buttons**: Add custom inline buttons with URLs to every forwarded message.
- **Auto-Delete**: Automatically delete forwarded posts after a specified duration.
- **Job Management**: Create, edit, stop, start, and reset multiple forwarding jobs.
- **Persistence**: Active jobs automatically resume after bot restart.

### 🤝 Auto-Approve
- **Instant Approval**: Automatically approves all incoming chat join requests for channels where the bot is an administrator.

## 🛠 Setup & Installation

### 1. Requirements
- Python 3.8+
- MongoDB Database

### 2. Configuration
Create a `config.py` file in the root directory or set the following environment variables:

- `API_ID`: Your Telegram API ID from [my.telegram.org](https://my.telegram.org)
- `API_HASH`: Your Telegram API Hash
- `BOT_TOKEN`: Your Bot Token from [@BotFather](https://t.me/BotFather)
- `MONGODB_URI`: Your MongoDB connection string
- `ADMIN_IDS`: List of admin user IDs (comma-separated in env vars)
- `FORCE_SUB_CHANNEL_ID`: (Optional) Channel ID or username for force subscription check

### 3. Installation
```bash
pip install -r requirements.txt
```

### 4. Running the Bot
```bash
python bot.py
```

## ⚙️ Optimization Details
- **Batch retrieval**: Uses `app.get_messages` to fetch multiple messages in a single request.
- **Native Methods**: Uses Pyrogram's `msg.copy()` for efficient message duplication.
- **Async Logic**: Fully asynchronous implementation using `motor` for database and `asyncio` for task management.
- **Graceful Shutdown**: Properly cancels background tasks on exit.
