# telegram-channel-ingestion

# Telegram Channel Ingestion via Telethon MTProto

Class of work: read an external Telegram channel (where your account is a **member**, not an admin) — backfill history + listen live — and persist raw messages plus parsed signals.

## Golden rule: MTProto user-client, NOT Bot API

A bot can read channel history only if made an admin. If the account is just a member (common for signal/scanner channels), use **Telethon MTProto user-client**.

> **Session file = credential.** Store OUTSIDE the repo (e.g. `<project>/.tgsessions/<name>.session`), chmod 700, add to `.gitignore`. Never log or commit.

## Procedure

### 1. Auth (interactive, one-time)

```python
import os, asyncio
from telethon import TelegramClient

with open("/opt/<project>/.env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

async def main():
    c = TelegramClient("/opt/<project>/.tgsessions/<name>",
                      int(os.environ["TELEGRAM_API_ID"]),
                      os.environ["TELEGRAM_API_HASH"])
    await c.connect()
    if await c.is_user_authorized():
        print("ALREADY_AUTHORIZED")
        return
    await c.send_code_request(os.environ["TELETHON_PHONE"])
    code = input("Code: ").strip()
    pwd = input("2FA password: ").strip()
    try:
        await c.sign_in(os.environ["TELETHON_PHONE"], code)
    except Exception as e:
        if "password" in str(e).lower():
            await c.sign_in(password=pwd)
    await c.disconnect()

asyncio.run(main())
```

**Pitfall — auth MUST be interactive.** Codes expire in ~30s. Background polls lose the race — run the script directly on the server and type code+password when prompted.

**Pitfall — `os.environ` vs `pydantic-settings`.** `settings.TELEGRAM_API_ID` works but `os.environ.get("TELEGRAM_API_ID")` returns None. Load `.env` manually (shown above).

**Pitfall — session lock.** Kill the running server BEFORE running auth, or get `sqlite3.OperationalError: database is locked`.

### 2. Parser — inspect real messages FIRST

Don't guess the format. In this session the channel published "Scout Opportunity" lists (TOP 30 LONG/SHORT with RSI15/TSI15/RSI4H/TSI4H/divergence/funding), NOT trade signals. The first parser assumed entry/SL/target and matched nothing.

**Pitfall — single-pattern side detection fails on real channel variety.** Section headers arrive with emoji prefixes (🔴 LONG OPPORTUNITIES, 🟢 SHORT OPPORTUNITIES), bare keywords, inline cues (LONG:, → SHORT), and ranked list items. Use a prioritized cascade instead of one regex:

1. Explicit section headers with context words (OPPORTUNITIES/TOP/SCOUT/SETUP/CONFIRM) + LONG/SHORT
2. Emoji/prefix markers (🔴🟢🟣▶▷⬆⬇) followed by LONG/SHORT
3. Inline direction cues (LONG:, SHORT:, → LONG, LONG →)
4. Carry forward the last-detected side for lines that don't re-declare it

**Pitfall — ambiguous bare headers.** A line like `LONG:` with no context word and no list items following may be data, not a header. Require a context word or emoji/prefix marker before switching side. If the next non-empty line contains a recognizable ticker pattern, treat the header as valid.

**Pitfall — first-pass parser is always wrong for a new channel.** Parse a batch of real messages, print candidates, eyeball misses, iterate the regex BEFORE wiring to execution. Don't assume correctness.

**Confidence scoring**: assign by rank (top-5 → high), section type (SCOUT/TOP → +0.1), cap at 1.0. Low-confidence signals can route to a review queue.

**Non-coin word filter**: maintain a deny-list of English words that collide with ticker patterns (TOP, LONG, SHORT, RSI, FUNDING, VOLUME, PRICE, ADMIN, UPDATE, SCAN, SCOUT, etc.). Extend when new false positives appear.

**Deduplicate by (symbol, side)** keeping highest confidence per pair.

**Extra model fields go to `unknown_fields` JSON.** If parser extracts fields not on the model (e.g. `rsi15`, `tsi15` when model has `rsi`), don't add DB columns. Pass known fields to constructor, stash extras in `unknown_fields = Column(JSON)`. Read back as `signal.unknown_fields.get("rsi15")`.

**Pitfall — stale `__pycache__` after patching parser.** Always clear `__pycache__` dirs after updating `.py` files on the target:
```bash
find /opt/<project> -name __pycache__ -exec rm -rf {} + 2>/dev/null
```
Otherwise the old compiled parser keeps running.

```python
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from sqlalchemy import select  # required in this module for runtime

class TgChannelIngester:
    def __init__(self, channel_username, backfill_limit=10000):
        self.channel_username = channel_username
        self.backfill_limit = backfill_limit
        self._api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
        self._api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        self._session_path = "/opt/<project>/.tgsessions/<name>.session"
        self._client = None
        self._running = False

    async def _connect(self):
        self._client = TelegramClient(self._session_path, self._api_id, self._api_hash)
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError("Telegram not authorized")

    async def backfill(self, entity):
        offset_id = 0
        total = 0
        while total < self.backfill_limit:
            batch = await self._client.get_messages(entity, limit=100,
                                                    offset_id=offset_id, reverse=False)
            if not batch:
                break
            for msg in batch:
                if msg.message:
                    await self._process_message(msg, entity)
                offset_id = msg.id
                total += 1
            await asyncio.sleep(0.5)
        return total

    async def _on_new_message(self, event):
        await self._process_message(event.message, event.chat)

    async def start(self):
        await self._connect()
        entity = await self._client.get_entity(self.channel_username)
        await self.backfill(entity)
        self._running = True
        self._client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=[entity]),
        )
        while self._running:
            await asyncio.sleep(30)
```

### 4. Restart (Proxmox CT)

```bash
pct exec 106 -- bash -c "fuser -k 8080/tcp 2>/dev/null; sleep 2"
pct exec 106 -- bash -c "find /opt/<project> -name __pycache__ -exec rm -rf {} + 2>/dev/null"
pct exec 106 -- bash -c "cd /opt/<project> && source venv/bin/activate && PYTHONPATH=/opt/<project> nohup python main.py > /tmp/cq.log 2>&1 &"
```

**Pitfall — stale `__pycache__`.** Always clear after patching `.py` files.

**Pitfall — port already in use.** `fuser -k <port>/tcp` before restart.
