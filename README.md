# Coinradar 📈

A Telegram bot for cryptocurrency price tracking and alerts, built in Python. It monitors coin prices, sends updates and chart visualizations to Telegram, and keeps local history in SQLite.

## Features

- 📈 Cryptocurrency price tracking with configurable alerts
- 📊 Chart generation (matplotlib) sent directly to your chat
- 💾 Local price history storage (SQLite)

## Setup

```bash
# Python 3.10+ required
pip install -r requirements.txt

# Configure your bot token (create a bot with @BotFather)
export TELEGRAM_BOT_TOKEN="your-bot-token-here"

# Run
python coinradar_bot.py
```

## Run as a systemd service (Linux)

Create `/etc/systemd/system/coinradar_bot.service`:

```ini
[Unit]
Description=Coinradar Telegram Bot
After=network.target

[Service]
Type=simple
User=youruser
Environment=TELEGRAM_BOT_TOKEN=your-bot-token-here
ExecStart=/usr/bin/python3 /path/to/coinradar_bot.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then reload and start:

```sh
sudo systemctl daemon-reload
sudo systemctl enable coinradar_bot.service
sudo systemctl start coinradar_bot.service
sudo systemctl status coinradar_bot.service
```

## Logs

```sh
sudo journalctl -u coinradar_bot.service -f
```

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file.
