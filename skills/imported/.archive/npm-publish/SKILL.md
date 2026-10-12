# npm-publish

# npm-publish

> Sibling skill **`npm-publishing`** covers the same territory in more depth
> (passkey-only 2FA, headless-agent handoff, KDE browser quirks, full error decoder).
> Prefer `npm-publishing` when the publish is stuck; both agree the #1 cause of a
> scoped-publish 404 is a **scope/slug mismatch**, not tokens or 2FA.

## When to use
- `npm publish` of a scoped package (`@org/name`) fails with E404.
- Need to publish using a passkey (FaceID on phone, fingerprint on laptop) instead of tokens.
- Setting up an npm org scope, or publishing the first package into an org.

## Core workflow
1. **Match the package `name` scope to the org's registry slug — this is the #1 cause of scoped-publish 404s.**
   - The org's npm URL is `npmjs.com/settings/<slug>/members`. The `<slug>` is the scope you must publish to: `@<slug>/name`.
   - A package named `@cortexcloud/mcp` targets scope `cortexcloud`. If the org slug is `cortexcloud.org`, the package MUST be `@cortexcloud.org/mcp` or the PUT 404s with "could not be found or you do not have permission".
2. **Confirm the scope exists at the registry BEFORE touching tokens:**
   `curl -s https://registry.npmjs.org/-/org/<slug>` → `{"code":"ResourceNotFound",...}` means the scope is not provisioned or the slug is wrong.
   Also: `curl -s https://registry.npmjs.org/-/org/<slug>/user` → `403` means the authenticated user is not a member (but verify the slug is correct first — a wrong slug also 403s).
3. **Publish from the device that holds the passkey** (FaceID on phone, fingerprint on laptop):
   `npm login --web` prints a URL → open it in the browser with the enrolled passkey → complete → then `npm publish <tarball> --access public`.

## Critical pitfalls (read before debugging)
- **Do NOT cycle granular access tokens to fix a scoped-publish 404.** A 404 "could not be found or you do not have permission" on `@scope/name` is almost always a scope/slug mismatch or an unprovisioned org — NOT a token-permission issue. Cycling tokens (even "bypass 2FA" ones) just reproduces the same 404, because no token can grant a scope that doesn't exist or doesn't match the package name. (Real session: 16 tokens tested, identical 403/404 every time — all wasted.)
- **`npm org ls <org>` is NOT a membership-list command.** It errors with `First argument 'orgname' is required` / a JSON error. There is no `npm org ls` that lists your orgs. Use the registry REST endpoint or the website UI to confirm membership.
- **npm redacts the publish-time 2FA URL** (`https://www.npmjs.com/auth/cli/***`) in ALL terminal/CLI output. The full URL IS visible on the user's own screen — copy it from there and open in the passkey browser. A headless agent session cannot relay it.
- **Bypass-2FA granular tokens are being deprecated** by npm (it shows a "tokens that bypass 2fa are being stopped" notice). Don't rely on them; publish via the passkey session instead.
- A scoped publish triggers a **SECOND** 2FA challenge, separate from login. Complete it in the same passkey browser (copy the `auth/cli/<uuid>` URL into Chrome/fingerprint).
- **First publish of a scope may provision it.** If it still 404s after the slug is correct, create the first package via the npm website org Packages tab ("Add package"), then CLI works for subsequent versions.

## Verification
- `npm view @<slug>/<name> version` → returns the version once live.
- `npm whoami` → should show the org-owning account.

## References
- `references/npm-scoped-publish-debug.md` — exact diagnostic commands and real error signatures.
