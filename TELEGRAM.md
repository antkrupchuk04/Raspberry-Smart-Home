# Telegram motion notifications

New, successfully stored motion detections can send a private Telegram message containing the room, sensor name, local event time and reported battery percentage. Clear reports, repeated occupancy reports and retained MQTT snapshots do not send notifications. Motion history continues to work when Telegram is disabled or unavailable.

## Bot setup

Create a bot through Telegram's `@BotFather` using `/newbot`. Open the bot and send `/start`. Obtain the intended private chat's numeric `message.chat.id` from the bot's `getUpdates` response. Do not select an unknown chat automatically or remove an existing webhook to obtain updates. If another application already consumes updates, obtain the chat ID from that application or use a dedicated bot.

Keep credentials in the existing private `~/smarthome/.env` (mode 0600, excluded from Git):

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=replace_with_your_bot_token
TELEGRAM_CHAT_ID=replace_with_your_numeric_private_chat_id
TELEGRAM_COOLDOWN_SECONDS=60
```

Restart only the collector after changing its settings:

```sh
sudo systemctl restart smarthome-collector
journalctl -u smarthome-collector -f
```

Set `TELEGRAM_ENABLED=false` and restart the collector to stop alerts. Telegram requires outbound HTTPS Internet access; no inbound port, webhook, Telegram SDK or additional runtime dependency is needed. The application does not poll Telegram updates or process incoming commands.

## Delivery behavior

- An optional callback queues a notification only after the PostgreSQL motion write commits.
- A bounded queue and a dedicated worker thread keep HTTP requests outside the MQTT callback.
- The default cooldown permits at most one attempt per sensor every 60 seconds. It applies to failed attempts too, to prevent bursts.
- Cooldown attempts are saved atomically before sending, in `~/.local/state/smarthome/telegram-notifications.json`, mode 0600. It contains timestamps and a credential-scope hash, never the token or chat ID. `TELEGRAM_STATE_FILE` can override its location.
- Cooldown survives collector restarts. A changed token or chat ID uses a new cooldown scope.
- Events waiting more than 120 seconds are discarded. There is no historical replay when enabling notifications or restarting the collector.
- Telegram HTTP 429 `retry_after` is respected. Network and HTTP errors log only generic information/status codes, never full URLs or credentials.
- Requests use a five-second socket timeout. Ambiguous failures/timeouts are not retried: Telegram may already have accepted the message. Queue overflow, shutdown, network outages or process crashes can therefore lose an alert; the PostgreSQL event history remains authoritative. This is a best-effort notification channel, not an alarm-delivery guarantee.
- No HTML/Markdown parse mode is used for messages. Sensor/room names are plain text.

## Validation

```sh
cd ~/smarthome
venv/bin/python -m unittest discover -s tests -v
```

Telegram tests mock all network requests. They cover post-commit-only notification, duplicate/clear/retained exclusion, disabled configuration, per-sensor cooldown, persistence, rate limiting, private state permissions, safe failure logging, notification isolation and worker behavior. Test credentials and fixture alerts are never sent to Telegram.

Sources: https://core.telegram.org/bots/tutorial and https://core.telegram.org/bots/api#sendmessage
