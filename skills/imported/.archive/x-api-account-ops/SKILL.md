# x-api-account-ops

# X API Account Operations

Operating an X account programmatically with `xurl` (the official CLI, see the
bundled `xurl` skill for command reference). This skill carries the hard-won
pitfalls that the bundled skill does not: fresh-account filters, auth
bootstrap when the browser flow is blocked, and the home-feed trap.

## Auth bootstrap (when the browser OAuth flow is blocked)

1. User creates an X developer app (developer.x.com → console.x.com) for the
   account; app type **"Web App, Automated App or Bot"** (confidential client)
   — Native App breaks PKCE.
2. **Callback URI must EXACTLY match the authorize URL's redirect_uri**:
   `http://localhost:8080/callback` (http, WITH `/callback`). The console
   rejects `http://` sometimes — keep retrying the exact form; an https
   variant saves but then every authorize attempt fails with "Something went
   wrong" on the x.com page.
3. **App must be in Production, not Sandbox** — sandbox apps refuse OAuth
   authorize with the same generic "Something went wrong" page. Check the app
   page banner; click "Move to Production".
4. User buys API credits (pay-per-use; min $5) — posting costs money.
5. Register + auth:
   ```bash
   xurl auth apps add <name> --client-id <ID> --client-secret <SECRET>
   xurl auth oauth2 --app <name> --headless <handle>
   xurl auth default <name>
   ```
   Headless mode prints an authorize URL and waits on stdin for the pasted
   redirect code → run it as a **background PTY process**, poll for the URL,
   send it to the user, then `process(action=submit)` the pasted
   `localhost:8080/callback?...code=...` URL. Non-interactive runs exit with
   EOF immediately.
6. **Manual token import** (user already has access+refresh tokens, e.g. from
   another tool, and the browser flow is broken): write `~/.xurl/auth.yml`
   (chmod 600) — schema from xurl source `store/tokens.go`:
   ```yaml
   apps:
     <name>:
       client_id: <id>
       client_secret: <secret>
       redirect_uri: http://localhost:8080/callback
       default_user: <handle>
       oauth2_tokens:
         <handle>:
           type: oauth2
           oauth2:
             access_token: <at>
             refresh_token: <rt>
             expiration_time: 0   # 0 -> refresh via client creds on first use
   default_app: <name>
   ```
   Verify: `xurl auth status` shows `▸ oauth2: <handle>`, then `xurl whoami`.
   The first write may 403 once and succeed after the auto-refresh re-mints a
   full-scope token.
7. X console quirks: Client ID ends in `MTpjaQ`; regenerating the secret
   invalidates the old one (re-register the app: `xurl auth apps remove
   <name>` + add again).

## New-account posting constraints (anti-spam)

Fresh profiles (days old) fail writes with a generic
`403 "You are not permitted to perform this action"` — same error as a scope
problem, so diagnose before assuming scope:
- **Non-ASCII (emoji 🚀, arrows →, en-dashes –) → 403.** Pure-ASCII versions
  of the same text post fine.
- **Text near 280 chars → 403** while a ~180-char version succeeds.
- `$` signs and bare domains (`api.example.com`, no scheme) are fine.
- **Rapid post+delete cycles trigger temporary write blocks** (same 403) —
  space out cleanup deletes.
- Repeated attempts of identical blocked text stay blocked — change the text.

Methodical diagnosis: `xurl post "test"` (tiny ASCII) proves writes work; then
bisect the failing text (length → symbols → links).

## Listing an account's own posts

`xurl timeline` = HOME feed (accounts the profile follows) — NOT the profile.
Deleting from it can target other accounts' posts (deletes fail harmlessly
with 403, but don't rely on that). Use the raw endpoint instead:
```
xurl "/2/users/<user_id>/tweets?max_results=20&tweet.fields=created_at"
```
`<user_id>` comes from `xurl whoami` → `data.id`.

## Profile editing (OAuth1.0a ONLY)

v2 `PATCH /2/users/:id` returns 403 **"You are not permitted to use OAuth2 on
this endpoint"** — profile writes (name, bio, location, url, avatar, banner)
reject OAuth2 tokens entirely, even with `users.write`. The v1.1 endpoints
(`/1.1/account/update_profile.json`, `update_profile_image.json`,
`update_profile_banner.json`) require **OAuth 1.0a**:

- Console → Keys and tokens → **OAuth 1.0 Keys**: API Key (consumer key), API
  Key Secret (consumer secret), Access Token, Access Token Secret.
- Store alongside OAuth2 in `~/.xurl/auth.yml`:
  ```yaml
  apps:
    <name>:
      ...
      oauth1_token:
        type: oauth1
        oauth1:
          access_token: <at>
          token_secret: <ats>
          consumer_key: <ck>
          consumer_secret: <cs>
  ```
- **xurl raw mode cannot sign v1.1 form-encoded bodies** (401 `code 32`
  "Could not authenticate you"); JSON bodies pass auth but v1.1 ignores them.
  Use `scripts/xprofile.py` (requests-oauthlib, creds read from auth.yml —
  never print them):
  ```
  scripts/xprofile.py show
  scripts/xprofile.py profile '{"name":"...","description":"...","location":"...","url":"..."}'
  scripts/xprofile.py avatar /path/img.png     # <= 2MB
  scripts/xprofile.py banner /path/img.png
  ```
- Verify writes via v2 read (`/2/users/me?user.fields=location,url,...`) —
  v1.1 read endpoints (`users/show.json`) 403 for standard tokens.

## Costs (2026 pay-per-use)

$0.015/plain post, $0.20/post with URL, $0.005/read, $0.001/owned read.
$5 ≈ 25 link-posts ≈ 3–4 months of a light channel (2 link posts/week).

## Verification

Every write must be confirmed by the xurl JSON response (`data.id` /
`data.deleted`), then by listing the account's own posts — never by the tool
exit code alone.
