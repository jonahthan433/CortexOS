# Connectors (Google: Gmail, Calendar, Drive, YouTube)

Connectors are the only path in CortexOS that performs external operations
(sending mail, creating calendar events, uploading files). They are deliberately
narrow: **reads run directly and are audited; every write/external action needs
a fresh human approval for every run** and is protected by idempotency,
rate limits, exact-scope checks, and a journal that blocks silent retries after
an unknown outcome.

| Connector | Reads (run directly) | Approval-gated actions | Default scopes |
|---|---|---|---|
| Gmail | `messages.search`, `messages.get` | `messages.send` | `gmail.readonly`, `gmail.send` |
| Calendar | `events.list` | `events.create` | `calendar.readonly`, `calendar.events` |
| Drive | `files.list` *(needs the optional `drive.readonly` scope — see caveat)* | `files.upload` (app-created files) | `drive.file` |
| YouTube | `videos.search` | — (upload/publish not implemented) | `youtube.readonly` |

Nothing runs until a connector has an OAuth client file **and** an
authorization. The HUD (Connectors panel) and the CLI both report
`not_configured` / `not authorized` exactly as they are.

## One-time setup

### 1. Google Cloud (per connector you want; one client covers all four)

1. Open <https://console.cloud.google.com/> and create (or pick) a project.
2. **APIs & Services → Library**: enable the APIs you need — *Gmail API*,
   *Google Calendar API*, *Google Drive API*, *YouTube Data API v3*.
3. **APIs & Services → OAuth consent screen**: User type *External* is fine for
   a personal account. While the app is in *Testing*, add your own Google
   account under **Test users** (otherwise Google blocks the consent). You do
   not need to publish the app for personal use.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   application type **Desktop app**. Copy the client ID and client secret.

### 2. Client files on the CortexOS host

Create one JSON file per connector under `config/connectors/`
(mode `0600`); the same client ID/secret may be reused for all of them:

```sh
install -m 600 /dev/stdin /opt/cortexos/app/config/connectors/gmail.json <<'JSON'
{
  "client_id": "…apps.googleusercontent.com",
  "client_secret": "…"
}
JSON
```

Optional keys in the same file:

- `"redirect_uri"`: overrides the default
  `http://localhost:8765/connectors/callback`. Any loopback URI works because
  the flow is manual copy/paste (nothing needs to listen on the server).

### 3. Authorize (either path)

**HUD:** Connectors → **Authorize** → open the shown link deliberately in a
browser → approve access. Google redirects your browser to the loopback URL;
the page will fail to load (**expected** — nothing listens there). Copy the
`code` parameter from the address bar (the whole URL works too) and paste it
into the panel, then press **Complete authorization**.

**CLI:**

```sh
cd /opt/cortexos/app && .venv/bin/cortexos connectors authorize gmail
# open the printed URL, approve, copy the code parameter from the redirect URL
.venv/bin/cortexos connectors complete gmail <state> <code>
```

Both paths exchange the code server-side with **PKCE (S256)** and store tokens
at `state/connectors/<id>/token.json` (0600). Tokens are never returned by any
API, never shown in the HUD, and never written to receipts.

### 4. Verify

HUD → Connectors → **Check health** (or
`POST /api/connectors/<id>/health`). `ok` means the stored token was accepted
by Google. Missing scopes are reported per operation as `missing_scope`.

## Using a connector

- **Drafts (the normal path).** Create a draft in the HUD drafts panel:
  - kind **Connector action**: target
    `{"connector": "gmail", "operation": "messages.search", "args": {"query": "…"}}`
    — reads execute on save (audited); sends/uploads create an approval.
  - kind **Email**: target `{"to": ["a@b.c"], "subject": "…"}`; the draft body
    is the message. Saving maps it to `gmail.messages.send`, which always waits
    for approval.
  - Approve the pending action with `/approve <id>` on Telegram or through the
    approvals view; rejecting never executes anything.
- **Direct API:** `POST /api/connectors/<id>/operations/<operation>` with
  `{"args": {…}}`; the same approval gate applies.

## Safety semantics (enforced in code, not documentation)

- Credentials: `not_configured` / `blocked_missing_credentials` block execution
  **before** any approval is created, so approvals can never be orphaned.
- Scopes: each operation declares its exact scopes; a missing granted scope is
  reported precisely and the call is refused before any HTTP request.
- Idempotency: external calls are deduplicated by content hash (or a caller
  key); the same request is never sent twice. A timeout/5xx after a call marks
  the outcome `unknown` and blocks silent retries until inspected
  (`state/connectors/journal.json`).
- Rate limits per operation, bounded timeouts, and no automatic retries for
  non-idempotent calls.
- Revoke: `.venv/bin/cortexos connectors revoke <id>` removes local tokens
  (approvals are unaffected). Revoke in your Google account to remove
  server-side access as well.

## Caveats

- **Drive `files.list`** requires the broader `drive.readonly` scope, which is
  intentionally **not** requested by default (least privilege). It stays
  declared-but-unavailable (`missing_scope`) until the scope set is extended in
  `cortexos/connectors/google.py`; re-run authorize afterwards.
- The consent screen shows *unverified app* while in Testing — expected for a
  personal OAuth client. No Google verification is needed for personal use.
- One OAuth client per Google Cloud project is enough for all four connectors;
  each connector keeps its own token and scope set.
