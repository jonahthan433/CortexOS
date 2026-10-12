# aws-cli-setup

# AWS CLI Setup & Credential Flows (headless hosts)

Use when installing the AWS CLI or authenticating AWS on a headless Linux
host (no browser): `aws login` flows, the Agent Toolkit setup, PATH wiring.

## Install (user-local, no sudo)

```bash
curl -fsSL 'https://awscli.amazonaws.com/v2/install.sh' | bash   # GPG-verified
export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
aws --version   # e.g. 2.36.x
```

## `aws login` flows (headless)

- **Same-device flow** (`aws login --region <r>`): callback goes to
  `http://127.0.0.1:<port>/oauth/callback` on the HOST. Useless when the
  user's browser is elsewhere — "site can't be reached". Use `--remote`.
- **`aws login --region <r> --remote`**: prints a `us-east-1.signin.aws.amazon.com/v1/authorize?...` URL; user authorizes on any device; AWS shows a code. Run in a **background PTY** — it blocks on `Enter the authorization code displayed in your browser:` stdin. Exact invocation: `terminal(background=true, pty=true, notify=true)` then drive stdin with `process(action="submit", data=<code>)` once the user pastes the code. Foreground `pty=true` will hang past the foreground timeout cap.
- **The code arrives base64-wrapped**: the user often pastes
  `base64("code=<JWT>&state=<uuid>")`. Verified: it decodes cleanly and the
  `state` matches the session's authorize URL.
- **RESOLVED 2026-08-10 — accepted input format: submit the RAW base64 blob
  verbatim.** The CLI base64-decodes the pasted input itself; feeding the
  decoded `code=...&state=...` (mid-string `=` → invalid base64) or the bare
  JWT-ish code (base64url `.`/`-` → StdEncoding rejects) both fail with
  `aws: [ERROR]: Failed to decode the verification code.` Paste the blob
  unchanged into the stdin prompt (process submit). Verified success: exit 0
  + `Updated profile default to use arn:aws:iam::<acct>:root credentials.`
- Each failed attempt kills the session → the user must re-authorize a fresh
  URL (PKCE challenge is per session; old codes never validate). Restart
  `aws login --remote` cleanly and get a NEW code rather than reusing one.
- Credentials are valid 12h, renewable 90 days without re-auth.
- Verify with `aws sts get-caller-identity`.

## Staging CT (CT105, 192.168.1.15) SSH

- CT105 sshd is **pubkey-only** — password auth is rejected with
  `paramiko.ssh_exception.BadAuthenticationType: Bad authentication type; allowed types: ['publickey']`.
  The local `~/.ssh/hermes_ninyi` ed25519 key (already configured for the
  Proxmox host `pve` / 192.168.1.11) **is** authorized on CT105 and works
  with `ssh -i ~/.ssh/hermes_ninyi root@192.168.1.15`.
- If `sshpass` is missing on the operator host, use **paramiko** in a throwaway
  venv: `uv venv /tmp/sshv --python 3.11 && uv pip install --python /tmp/sshv/bin/python paramiko`.
  `paramiko.Ed25519Key.from_private_key_file("~/.ssh/hermes_ninyi")` + `pkey=key`
  on `connect()` is the correct auth shape for this CT.

## Agent Toolkit (`aws configure agent-toolkit`)

Setup doc: `https://raw.githubusercontent.com/aws/agent-toolkit-for-aws/refs/heads/main/setup-instructions/setup.md`
- Toolkit service lives ONLY in **us-east-1** regardless of the account's
  default region.
- Interactive wizard; exit 253 / "requires interactive terminal" means the
  user must run it in their own terminal.
- Requires live credentials (`sts` OK) first.
- **Aborts with "No supported AI coding agents found"** when none of the
  supported agents (Claude Code, Codex, Cursor, Kiro, Gemini CLI, OpenCode,
  Windsurf, ...) are installed — Hermes is NOT on the list. Either install
  one of them, or skip the skill bundle: the CLI + the rules file
  (`rules/aws-agent-rules.md` → AGENTS.md) still give full capability.
- Verify the toolkit: `aws agent-toolkit list-available-skills --region us-east-1`.
