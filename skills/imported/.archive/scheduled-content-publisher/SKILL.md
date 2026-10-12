# scheduled-content-publisher

# Scheduled Content Publisher

Class of work: the user wants an agent to **publish content on a recurring
schedule** to a channel or feed they own — e.g. "post 3 times a day to my
Telegram channel", "daily tips to my group", "weekly roundup to the feed".
Distinct from `scheduled-briefing-agent` (which delivers a private digest TO
the user); here the output is PUBLIC content authored for an audience.

The deliverable is one or more `cronjob(action=create)` jobs whose prompt
generates a finished post and delivers it straight to the channel.

## Workflow

1. **Get the destination identifier FIRST — it's the hard blocker.** Being
   admin isn't enough; the cron needs an explicit target. For Telegram:
   - Public channel → `@username`.
   - Private channel → numeric ID starting `-100...` (user can forward a
     channel message or read it from channel info).
   Set `deliver="telegram:<id>"` on the cronjob.

2. **Verify publish permission with a real test post BEFORE building the
   engine.** Schedule a one-shot job (`schedule="1m"`, NOT "now" — "now" is
   rejected; use a short duration) that posts a short confirmation line to the
   channel. Have the user confirm it appeared. If it fails, the bot likely
   needs "Post Messages" admin permission enabled explicitly. Only build the
   full engine after the test lands.

3. **Nail down cadence + rotation with the user:** how many posts/day, at what
   times, and any special recurring slot (e.g. a weekly roundup on a fixed
   weekday). Default spread for 3/day: 09:00 / 14:00 / 19:00.

4. **One cron job PER daily time slot** (not one job posting 3x). Keeps timing
   and per-slot category logic clean. Give each a distinct category pool so
   slots don't collide.

5. **Anti-repeat via a shared log file.** Create e.g.
   `~/.hermes/<project>/posts_log.md`. Every job prompt MUST:
   - Read the log first and avoid repeating tools/topics/categories used in the
     last several days (and the other slots THAT day).
   - Append a one-line record after composing:
     `YYYY-MM-DD | SLOT | CATEGORY | tools/topic | summary` (date via
     `date +%Y-%m-%d`), BEFORE emitting the final post.

6. **Fact-check gate.** Bake into each prompt: verify every claim, tool name,
   URL, and repo with `web_search`; confirm free/affordable resources are
   actually live and free; no clickbait, hype, or unverified rumors.

7. **Conditional/project-update posts must be gated on GENUINE signal.** e.g.
   only post a project update when a GitHub commit poll shows real new
   progress — never invent progress. Otherwise fall back to a rotation
   category.

8. **Restrict `enabled_toolsets`** to what the job needs (typically
   `["web","file","terminal"]`) to cut token overhead.

## Critical output rule (the cron IS the post)

A cron job auto-delivers the agent's FINAL response verbatim to the target.
So the prompt must instruct: "Your final response IS the post — output ONLY
the finished post text, no preamble, no explanation, no meta." Put log-writing
and web_search as intermediate steps that finish BEFORE the final message.

## Clean delivery — stripping the cron wrapper

Cron's built-in delivery prepends a metadata wrapper to what subscribers/you
see, e.g.:

```
Cronjob Response: <job name>
(job_id: <id>)
-------------
<the post>
...
To stop or manage this job, send me a new message ...
```

This wrapper is injected by the cron delivery layer AFTER the agent produces
its output. It is NOT in the agent's response and NOT removable by any prompt
instruction ("output only the post text" can't strip text appended later),
nor by a config flag or `hermes cron` option. Do not promise the user a prompt
tweak will remove it — it won't.

**Fix: bypass cron delivery and send via `hermes send`.** Keep the job on its
cron schedule but set `deliver="local"` (save only, no auto-delivery), and make
the job's LAST step write the finished post to a file and call:

```bash
hermes send --to telegram:<channel_id> --file /path/to/post.md
```

`hermes send` is a plain send path with no cron wrapper — the post lands clean.
`hermes send --list` shows configured targets; `--to` accepts
`telegram:<chat_id>[:thread_id]` or `platform:#channel`. This also applies to
private briefings (`scheduled-briefing-agent`) if the user wants the wrapper
gone there too.

## Voice / formatting preferences (embed these in the prompt)

- Write as a knowledgeable HUMAN. NEVER reference cron, automation, bots,
  scheduling, "this post", or any test/meta artifact. (User explicitly
  required this — clean, human-like posts only.)
- Concise, valuable, scannable. Plain formatting. Emojis sparingly.
- End with a call to action inviting discussion or sharing.
- Respect any length caps the user gives (e.g. news under 200 words).
- If the user says "no voice / simple posts," they mean plain text posts — do
  NOT add a persona, mascot, or stylistic flourish; keep it straightforward.

## Pitfalls

- Scheduling before verifying publish permission → silent failures to a channel
  you can't see. Always do the test post first.
- `schedule="now"` is invalid — use a short duration like `"1m"` for a one-shot
  test.
- One job doing multiple posts → messy timing and rotation. Use one job/slot.
- No anti-repeat log → the engine repeats tools/topics within days. The shared
  log is mandatory.
- Leaving meta/automation language in posts → breaks the human illusion. The
  output rule above must be explicit in every job prompt.
- **`hermes send` + leaving `deliver` on the channel → DOUBLE POST.** When you
  switch a job to clean delivery via `hermes send`, you MUST also change the
  job's `deliver` to `"local"`. If `deliver` still points at the channel, the
  post goes out TWICE: once wrapped through cron's built-in delivery, once clean
  via `hermes send`. Always pair the `hermes send` step with `deliver="local"`.
- Cron runs in a fresh session with no chat context — self-contain every
  prompt (paths, channel ID, rules).
