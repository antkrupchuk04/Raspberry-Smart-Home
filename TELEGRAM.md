# Telegram motion notifications

New, successfully stored motion detections can send a private Telegram message containing the location, sensor label, local event time and status. A subsequent clear transition sends one matching **Motion cleared** message after a successfully sent detection. Repeated occupancy reports and retained MQTT snapshots do not send notifications. Motion history continues to work when Telegram is disabled or unavailable.

Messages use this layout, with a bold heading:

```text
🚨 Motion detected

📍 Location: Living Room
📡 Sensor: motion-room1
🕒 Time: 29.09.2026, 20:26
⚡ Status: Motion detected
```

Clear messages use `✅ Motion cleared` and `⚡ Status: Motion cleared`, with the real clear event's local timestamp. Time follows the Raspberry Pi's configured timezone. Battery readings remain available in the dashboard.

## Bot setup

Create a bot through Telegram's `@BotFather` using `/newbot`. Open the bot and send `/start`. Obtain the intended private chat's numeric `message.chat.id` from the bot's `getUpdates` response. Do not select an unknown chat automatically or remove an existing webhook to obtain updates. If another application already consumes updates, obtain the chat ID from that application or use a dedicated bot.

Keep credentials in the existing private `~/smarthome/.env` (mode 0600, excluded from Git):

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=replace_with_your_bot_token
TELEGRAM_CHAT_ID=replace_with_your_numeric_private_chat_id
TELEGRAM_COOLDOWN_SECONDS=60
TELEGRAM_SENSOR_LABEL=motion-room1
TELEGRAM_LOCATION_LABEL="Living Room"
```

The optional labels only affect Telegram. Empty labels use `MOTION_SENSOR_NAME` and `MOTION_SENSOR_ROOM`; the MQTT topic, device ID and stored history are unchanged.

Restart only the collector after changing its settings:

```sh
sudo systemctl restart smarthome-collector
journalctl -u smarthome-collector -f
```

Set `TELEGRAM_ENABLED=false` and restart the collector to stop alerts. Telegram requires outbound HTTPS Internet access; no inbound port, webhook, Telegram SDK or additional runtime dependency is needed. The application does not poll Telegram updates or process incoming commands.

## Delivery behavior

- An optional callback queues a notification only after the PostgreSQL motion write commits.
- A bounded queue and a dedicated worker thread keep HTTP requests outside the MQTT callback.
- The default cooldown permits at most one detection attempt per sensor every 60 seconds. It applies to failed attempts too, to prevent bursts.
- A matching clear bypasses the detection cooldown without resetting it. Detection/clear cycles suppressed by cooldown produce no extra clear alerts. A clear queued while detection is still being sent is delivered only if that detection succeeds.
- Cooldown attempts are saved atomically before sending, in `~/.local/state/smarthome/telegram-notifications.json`, mode 0600. It contains timestamps, pending-clear flags and a credential-scope hash, never the token or chat ID. `TELEGRAM_STATE_FILE` can override its location.
- Cooldown and successfully sent detections awaiting a clear survive collector restarts. A changed token or chat ID uses a new scope. Older state files without pending-clear flags still load normally.
- Events waiting more than 120 seconds are discarded. There is no historical replay when enabling notifications or restarting the collector.
- Telegram HTTP 429 `retry_after` is respected. Network and HTTP errors log only generic information/status codes, never full URLs or credentials.
- Requests use a five-second socket timeout. Ambiguous failures/timeouts are not retried: Telegram may already have accepted the message. Queue overflow, shutdown, network outages or process crashes can therefore lose an alert; the PostgreSQL event history remains authoritative. This is a best-effort notification channel, not an alarm-delivery guarantee.
- Telegram HTML mode renders the bold heading. All dynamic sensor/location labels are escaped so they cannot inject formatting.

## Validation

```sh
cd ~/smarthome
venv/bin/python -m unittest discover -s tests -v
```

Telegram tests mock all network requests. They cover exact message formatting, safe labels, post-commit detection/clear notification, duplicate/retained exclusion, matched clears inside cooldown, suppressed cycles, failed detection/clear delivery, disabled configuration, per-sensor cooldown, persistence and migration, rate limiting, private state permissions, safe failure logging, notification isolation and worker behavior. Test credentials and fixture alerts are never sent to Telegram.

Sources: https://core.telegram.org/bots/tutorial and https://core.telegram.org/bots/api#sendmessage
