# hermes-ops-selfrepair

# Hermes Ops — Self-Repair

When Hermes itself is broken (not the user's projects), fix it at the root. Two recurring failures below; add more as they emerge.

## 1. Vision returns "no image was attached" / blank

**Symptom:** `vision_analyze` on a valid JPEG/PNG returns "no image was attached" or a canned "please upload an image" text. The image file IS valid (check `file` / `ls -la` first — non-empty, proper dimensions).

**Root cause:** `auxiliary.vision` is pinned to a provider whose API key is dead/forbidden. The framework silently drops to a text-only fallback that claims no image. The dead key does NOT throw loud — it degrades to "no image attached", which looks like a vision bug but is an auth bug.

**Verify the provider is dead (not a fluke):**
```bash
cd ~/.hermes && grep -nE 'vision:' -A2 config.yaml
# find the key for that provider in .env, then:
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $KEY" https://openrouter.ai/api/v1/keys
# 401 = dead key = root cause
```

**Fix — repoint vision to a working provider (config is guard-railed, use the CLI):**
```bash
hermes config set auxiliary.vision.provider gemini        # uses GOOGLE_API_KEY in .env
hermes config set auxiliary.vision.model gemini-3.6-flash
hermes config show | grep -A1 -i vision   # confirm
```
- `opencode` / `opencode-zen` may also work but `opencode-zen` can hit a `401 CreditsError` (no payment method) even for free models — prefer `gemini` (GOOGLE_API_KEY, free tier) when available.
- Don't patch `config.yaml` directly — agent edits to it are refused; use `hermes config set`.

**Confirm fix with a real image** before declaring done — re-run `vision_analyze` on a known screenshot.

## 2. Desktop app won't launch — chrome-sandbox sudo error

**Symptom (on the user's laptop, e.g. Fedora 'Admin' as user `jonathan`):**
```
sudo: a terminal is required to read the password
✗ Failed to configure Electron's Linux sandbox helper: .../linux-unpacked/chrome-sandbox
```
`hermes desktop` tries `sudo` internally to set the setuid bit, but the env can't prompt for a password.

**Fix — set the sandbox binary to root-owned setuid 4755 as root:**
```bash
sudo chown root:root /home/hermes/.hermes/hermes-agent/apps/desktop/release/linux-unpacked/chrome-sandbox
sudo chmod 4755 /home/hermes/.hermes/hermes-agent/apps/desktop/release/linux-unpacked/chrome-sandbox
ls -la .../chrome-sandbox   # expect -rwsr-xr-x root root
```
Then `hermes desktop` again. The `-rwsr-xr-x` (the `s`) is the setuid bit — that's what Electron needs.

**Pitfalls:**
- The error text "No such file or directory" on manual chown means the binary isn't extracted yet — run `hermes desktop` once (it unpacks), THEN chown. Don't chown a path that doesn't exist.
- `sudo hermes desktop` fails "command not found" — `hermes` is in user PATH, not root's. Fix the binary perms directly instead.
- `hermes desktop -s` → "unrecognized arguments": `-s` isn't a flag. Ignore it.
- The desktop install is PER-MACHINE: fixing it on CT103 does NOT fix the user's laptop (Admin). Ask which instance, or fix both.

## General rules for self-repair
- Always verify the fix end-to-end (real image read, real login 200, real launch) — never trust a config write alone.
- Root-cause, not symptom: a "vision broken" report is usually a dead provider key; a "desktop won't launch" is a missing setuid bit. Fix the cause.
- Config edits go through `hermes config set`, not direct file writes.
- See `references/provider-credits.md` for which providers are known-working vs paywalled.
