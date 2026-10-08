# Skills

## Capture and promotion rules

- After doing a recurring task by hand twice, capture its repeatable steps as a skill.
- A skill's deliverable should be named in its instructions and saved in `output/`.
- After five user-confirmed successful runs, review whether the inputs and output are stable enough to automate. The HUD offers explicit success/improvement feedback; merely finishing a model run does not count. This is a review threshold, not permission to enable unattended behavior.
- Sending, spending, and publishing remain human-approved by default, even after automation.

## Skill layout

```text
skills/<domain>/<skill-id>/
  skill.json
  SKILL.md
```

The manifest keys are `id`, `name`, `domain`, `description`, `trigger_phrases`, `tier`, `approval_required`, `automation_ready`, and `instructions`. Keep IDs stable and globally unique. Set `approval_required` to true whenever the task could send, spend, or publish.

## Adding a skill

1. Copy a neighboring skill directory and use a kebab-case unique ID.
2. Describe when to use it and what information it needs.
3. Give a clear step sequence and a concrete Markdown deliverable.
4. Add representative trigger phrases, but avoid broad matches such as `help`.
5. Set the approval flag conservatively.
6. Use the HUD/Obsidian front door when available, or submit through `POST /api/requests` now.
