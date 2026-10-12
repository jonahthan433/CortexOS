# npm-publishing

# npm-publishing (scoped packages, passkey-only 2FA, headless agent)

## When to use
User asks to publish an npm package, especially a scoped `@org/...` package, and the
agent environment is headless (no browser, no FaceID/fingerprint attached to the
agent session).

## ROOT CAUSE FIRST — the scope/slug mismatch (the #1 cause of scoped-publish 404s)
Before touching tokens, 2FA, or the passkey device, verify the package name's scope
matches the org's **registry slug**. This single check resolves most "could not be
found or you do not have permission" 404s and would have saved ~16 wasted token
attempts in one session.

- The org's npm URL is `npmjs.com/settings/<slug>/members`. The `<slug>` is the scope
  you must publish to: `@<slug>/name`.
- A package named `@cortexcloud/mcp` targets scope `cortexcloud`. If the org slug is
  `cortexcloud.org` (URL `npmjs.com/settings/cortexcloud.org/members`), the package
  MUST be `@cortexcloud.org/mcp` — otherwise the PUT 404s with
  "could not be found or you do not have permission", even with a perfectly valid
  owner login and token.
- **Confirm the slug at the registry BEFORE anything else:**
  ```bash
  curl -s https://registry.npmjs.org/-/org/<slug>
  # → {"code":"ResourceNotFound","message":"/-/org/<slug> does not exist"}  = slug wrong OR org unprovisioned
  curl -s https://registry.npmjs.org/-/org/<slug>/user
  # → 403 = authenticated user not a member (but verify slug is correct first; a wrong slug also 403s)
  ```
- Fix: rename `package.json` `"name"` to `@<slug>/<name>` (the code is scope-agnostic;
  only the name string matters), rebuild, repack. The rename + publish on the
  passkey device is the actual fix — NOT token cycling.
- **First publish of a scope may provision it.** If it still 404s after the slug is
  correct and the owner is logged in, create the first package via the npm website
  org Packages tab ("Add package"), then CLI works for subsequent versions.

## Hard blocker (learned the hard way)
If the user's npm 2FA is **passkey-only** (FaceID / fingerprint, no TOTP authenticator
app), publishing from a headless agent session is effectively impossible:
- `npm publish` with a granular token that has org scope returns `EOTP`.
- The EOTP error prints a browser URL — but npm **redacts it as `***`** in BOTH stdout
  and log files. You cannot see or relay it. (`grep auth/cli/...` returns nothing.)
- The `--otp <code>` flag exists, but needs a 6-digit TOTP code the user doesn't have.
- `npm login --web` also prints a redacted URL — same dead end.
- Granular tokens WITHOUT org scope → `404 Not Found` on publish (no permission). A token
  only reaches the org if it was explicitly scoped to `@org` at creation (or hits EOTP,
  which proves it has org access).

Result: the publish MUST complete on a device that holds the passkey (the user's
Lenovo with fingerprint, or cross-device browser flow on their iPhone). The agent
cannot finish it.

## Workflow that works
1. **Build + verify on the agent** (headless is fine for this):
   - `npm install`, build (`tsc` etc.), `npm pack` → produces `pkg-0.1.0.tgz`.
   - Smoke-test the server/bin locally (e.g. MCP stdio handshake) BEFORE handing off.
   - Fix `bin` field: use `"bin": {"name": "dist/index.js"}` (NO `./` prefix) or npm
     auto-removes it with a warning and the CLI command won't be installed.
   - Record the tarball sha256 for verification.
2. **Hand the tarball to the user's device** (pick one):
   - **scp**: `scp hermes@<host>:/path/pkg-0.1.0.tgz ~/Downloads/` (fastest if reachable).
   - **base64**: `base64 -w0 pkg-0.1.0.tgz` → paste; user decodes:
     `echo "<b64>" | base64 -d > pkg-0.1.0.tgz` on their machine.
3. **User publishes locally** (native passkey prompt appears):
   ```bash
   npm login            # FaceID/fingerprint prompt — works natively on their device
   npm publish pkg-0.1.0.tgz --access public
   npm view @org/pkg version   # → 0.1.0
   ```
4. **Agent verifies**: `npm view @cortexcloud/mcp version` (from anywhere) confirms live.

## Publish triggers a SECOND, separate 2FA challenge
`npm login --web` completing does NOT satisfy publish 2FA. When the user runs
`npm publish`, npm prints a *second* `Authenticate your account at:
https://www.npmjs.com/auth/cli/<uuid>` prompt. If that second challenge is not
completed (e.g. it auto-opened in a browser where the passkey fails), `npm publish`
aborts with `404 Not Found ... could not be found or you do not have permission` —
which looks like a scope error but is actually an aborted 2FA.
- Symptom: `npm whoami` shows the org owner (e.g. `jonathan44`), login succeeded, but
  `npm publish` still 404s AND a fresh `auth/cli/<uuid>` URL was printed → it's the
  publish-time 2FA that didn't complete, not a scope problem.
- Fix: complete that second `auth/cli/<uuid>` URL in the browser where the passkey
  works (Chrome, see below). The URL is one-shot — re-run `npm publish` to get a
  fresh one if it expired.

## Default browser / WebAuthn on KDE (fingerprint only works in Chrome)
If the user's fingerprint/passkey works in Chrome but not Firefox, and `npm login` /
publish auto-opens Firefox, the 2FA never completes:
- `xdg-settings set default-web-browser google-chrome.desktop` often does NOT stick on
  KDE Plasma — it emits `/usr/bin/xdg-mime: line 885: qtpaths: command not found` and
  `xdg-settings get` still reports `org.mozilla.firefox.desktop`. Harmless noise, but
  the setting didn't take.
- Force it via KDE config instead:
  ```bash
  kwriteconfig5 --file kdeglobals --group General --key BrowserApplication google-chrome.desktop
  kbuildsycoca5 2>/dev/null
  ```
- **Most reliable:** don't rely on the default at all. When npm prints the
  `auth/cli/<uuid>` URL, COPY it from the terminal and PASTE it into Chrome manually,
  then touch the fingerprint. npm detects the callback regardless of which browser
  launched it. Works for both `npm login --web` and the publish-time challenge.

## Org-scoped bypass-2FA token (DEPRECATED — npm is retiring these)
npm now shows a "tokens that bypass 2fa are being stopped" notice. Treat bypass-2FA
granular tokens as unavailable. If you must try, a granular token created from the
**personal account** token page 404s (no org scope) even with bypass-2FA ticked.
Create it FROM INSIDE the org instead:
- npmjs.com → your org (`@cortexcloud`) → **Settings → Access Tokens** (not your
  personal account's tokens page). Generate a granular token there with
  **Bypass 2FA for publishes** enabled. This token is org-scoped AND bypasses the
  publish 2FA → `npm publish` from the agent succeeds with no prompt.
- If that token still 404s, the org page wasn't used — regenerate from the org scope.
- **Reality check:** even when these tokens existed, the 404 was almost always a
  scope/slug mismatch (see ROOT CAUSE FIRST), not a 2FA gate. Fix the slug first.

## Working handoff path used in a real session (build on agent, publish on user device)
- Agent builds + packs tarball at `/home/hermes/cortexcloud-mcp/`.
- Push to the Proxmox CT (ninyi) so it's reachable from the user's laptop:
  `scp -i ~/.ssh/hermes_ninyi /home/hermes/cortexcloud-mcp/<pkg>.tgz root@192.168.1.11:/root/<pkg>.tgz`
- User on their laptop: `scp root@192.168.1.11:/root/<pkg>.tgz ~/Downloads/` then
  `npm publish <pkg>.tgz --access public` (native fingerprint prompt).
- If the agent→ninyi scp gets blocked by a consent prompt, fall back to base64:
  `base64 -w0 <pkg>.tgz` on agent, user decodes `echo "<b64>" | base64 -d > <pkg>.tgz`.

### THE granular-token "Organizations" dropdown is the step users miss
When creating a granular token, the form has a dedicated **"Organizations" section**.
The token only gets `@org` scope if you **select `@cortexcloud` in that dropdown**
(or generate the token from the org's own token page). If you skip it, the token is
scoped to the personal account only → `403 Forbidden - GET /-/org/<org>/user` and
`404` on publish, **regardless of the "Bypass 2FA" tickbox**. A "bypass-ticked" token
without the org selected is useless for scoped publish. This was the root cause of
~10 failed publish attempts in one session: every token was personal-scope.

### The "Do not challenge npm publish" checkbox is NOT a scope fix
The npm 2FA page has a checkbox: *"Do not challenge npm publish, npm trust
operations from IP <x> for the next 5 minutes."* This only **bypasses the 2FA
challenge** — it grants NO publish permission. If you tick it and `npm publish`
**still** 404s with the same "could not be found or you do not have permission",
that conclusively proves the failure is **scope/permission, not 2FA**. Stop chasing
2FA at that point and fix the token scope (see above). Burning more 2FA cycles after
this checkbox fails is wasted effort.

### Decisive diagnostic: verify the SLUG at the registry FIRST (before any token)
`npm org ls` is NOT a membership-list command — it errors with
`First argument 'orgname' is required` / a JSON error, and there is no `npm org ls`
variant that lists your orgs. Do NOT rely on it. Instead, confirm the slug directly:

```bash
# On the agent (no auth needed for existence check):
curl -s https://registry.npmjs.org/-/org/<slug>
# → {"code":"ResourceNotFound",...}  = slug wrong OR org unprovisioned (the REAL 404 cause)
# → org JSON  = slug correct; now check membership with an authed call on the user device
```

- If `curl /-/org/<slug>` returns `ResourceNotFound`, the slug in `package.json` does
  not match the org's registry slug. **This was the actual root cause of a 16-token
  failure loop** (package was `@cortexcloud/mcp` but the org slug was `cortexcloud.org`).
  Rename `package.json` `"name"` to `@<slug>/<name>` and republish — do NOT cycle tokens.
- Confirmed in this session: `cortexcloud` AND `cortexcloud.org` both returned
  `ResourceNotFound` at the registry, yet the website showed the org with the user as
  owner. The slug in the package name was the only thing that mattered. Renaming to
  `@cortexcloud.org/mcp` + publishing from the passkey device fixed it immediately.
- Corroboration: pnpm issue #11098 shows the identical signature (`whoami` = owner,
  yet publish 404s) — root cause was scope/slug, not the registry rejecting a legit owner.

## Error-shape decoder (read before burning more tokens)
- `404 Not Found ... could not be found or you do not have permission` on PUBLISH,
  with `npm whoami` = the org owner and a fresh `auth/cli/<uuid>` URL printed → the
  publish-time 2FA did not complete (see above), NOT a scope error.
- `402 Payment Required - ... You must sign up for private packages` → org-scoped
  publish targeted a scope/slug that doesn't exist on the registry (the org is
  provisioned under a DIFFERENT slug, see ROOT CAUSE FIRST). Happens on a scoped
  package BEFORE the slug is fixed — fix the scope mismatch, NOT npm access. Even
  with the org's package already public under the correct slug, publishing a version
  to a WRONG/new scope still yields this 402; `npm publish --access public` alone
  does not rescue a wrong slug — rename `package.json` `"name"` to the real slug first.
- `404 Not Found ... could not be found or you do not have permission` on PUBLISH with
  a granular TOKEN (no `npm login`) and NO `auth/cli` URL → token has NO org scope.
  Regenerate the granular token WITH the `@org` scope selected in the Organizations
  dropdown (or from the org's own token page).
- `403 Forbidden - GET /-/org/<org>/user` → account not seen as org member by registry.
  Confirm with `npm org ls` on the user device (see diagnostic above).
- `EOTP ... Open this URL ... ***` → token HAS org scope; needs 2FA. With passkey-only
  2FA this is a dead end headlessly. Hand off to user device.
- Same `404` even after ticking "Do not challenge npm publish" → **scope problem, not
  2FA** (the checkbox only suppresses the 2FA prompt). Fix token scope.

## When the user says "do research online / let's fix this"
Before burning another token, web-search the exact error signature. The npm 404-on-
scoped-publish pattern is a known, documented issue (pnpm #11098; npm/npm #17569):
`whoami` = owner + `owner ls` lists them, yet publish 404s. The accepted fixes are
(1) re-run login to refresh the session token, or (2) use a properly org-scoped
token. Research first → saves the token shuffle loop.

## Secrets hygiene
- Set `NPM_TOKEN` via session-only:
  `printf '//registry.npmjs.org/:_authToken=%s\n' "$NPM_TOKEN" > ~/.npmrc` (chmod 600).
  Never echo the token back. Session ends → token gone.

## References
- `references/scoped-publish-404-decision-tree.md` — **START HERE** for any scoped-publish 404: a 5-step ordered triage (slug check → rename → membership → passkey-device publish → token scope) plus a quick error decoder table.
- `references/npm-passkey-2fa-headless.md` — full diagnostic excerpts and the exact
  error shapes (404 vs 403 vs EOTP) with what each means.
- `references/npm-scope-diagnostic-2026-07.md` — condensed session evidence: the
  granular-token "Organizations" dropdown miss, the "Do not challenge" non-fix, the
  `npm org ls` decisive test, and pnpm #11098 corroboration.
