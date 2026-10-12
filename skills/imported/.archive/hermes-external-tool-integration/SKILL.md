# hermes-external-tool-integration

# Hermes External Tool/Skill Integration

This skill outlines the process for integrating external tools or skills (e.g., from GitHub repositories) that are not simple `SKILL.md` files. This often involves cloning a repository, executing installation scripts, or using package managers.

Runnable per-repo recipes: `references/repo-install-patterns.md`.

## Trigger Conditions

Use this skill when the user asks to:
*   Install a new tool or skill from an external source (e.g., a GitHub repository).
*   Integrate a plugin that extends Hermes' capabilities.
*   Set up a development environment from a cloned repository.
*   Wire a tool's own MCP server into Hermes (`hermes mcp add <name>`) — full recipe + pitfalls in `references/mcp-server-registration.md`.

## Workflow

1.  **Clone the Repository:**
    *   Use `git clone <repository_url>` to download the repository.
    *   The `terminal` tool typically sets the current working directory to the cloned repository's root after a successful clone. Verify this with `pwd` if unsure.

2.  **Inspect for Installation Instructions:**
    *   List the contents of the cloned repository: `ls -F <repository_name>`
    *   Look for common installation files:
        *   `install.sh`, `setup.py`, `configure`, `Makefile` (in `scripts/` or root)
        *   `package.json` (for `npm`/`npx` commands)
        *   `skill.json` (may contain specific `install` instructions as seen in this session)
        *   `README.md` or `docs/` directory (for general instructions)
    *   Read relevant files using `read_file <path_to_file>`.

3.  **Determine Installation Method:**

    Five recurring patterns (see `references/repo-install-patterns.md` for
    full runnable recipes):

    *   **Two-step convert-then-install (plugin):** repo ships
        `scripts/convert.sh` + `scripts/install.sh`. Run `convert.sh` FIRST —
        `install.sh` fails (missing generated files) if you skip it.
        `install.sh` writes to `~/.hermes/config.yaml` and
        `~/.hermes/plugins/<name>` → needs a restart.

    *   **`skill.json`-driven `npx` installer:** repo root has `skill.json`
        with an `"install"` field like `npx <cli> init --ai {{platform}}`.
        Pass the model FAMILY as `--ai` (`gemini`, `claude`, `codex`, ...),
        NOT `hermes` — `hermes` is rejected as "Invalid AI type". It may
        write to a harness dir (e.g. `.gemini/skills/`), not `~/.hermes/skills/`.

    *   **Python package + bundled `skill/` dir:** repo is a pip package with
        a CLI AND an `agent_reach/skill/SKILL.md` (or similar). Install the
        package AND copy the skill dir into `~/.hermes/skills/`. Use `uv` for
        venvs when system `python3 -m venv` lacks the `venv` module.

    *   **Bulk `skills/` tree copy:** repo has `skills/<name>/SKILL.md` for
        many skills. `cp -rf skills/. ~/.hermes/skills/<category>/` using the
        repo's own folder names as the category to avoid clobbering existing
        Hermes skills.

    *   **Direct File Placement:** if it's a single `SKILL.md`, copy to
        `~/.hermes/skills/<category>/<name>/SKILL.md`.


4.  **Execute Installation:**
    *   Use the `terminal` tool to run the identified command or script.

5.  **Verify and Follow Post-Installation Steps:**
    *   Review the output for success messages or error indications.
    *   **Crucially, if the installation involves enabling a Hermes plugin or modifying core configuration (e.g., `~/.hermes/config.yaml`), inform the user that a **Hermes session or gateway restart** is required for the changes to take effect.**

## Pitfalls and Troubleshooting

*   **Terminal `cd` loop / "No such file or directory" on a repo that exists:**
    When `terminal` calls are chained (`cd repo && ./script.sh`), a failed
    early `cd` leaves the cwd unchanged, so the next `cd repo` fails again
    ("directory not found") and you repeat the same error — looks like a loop.
    **Diagnosis recipe:** always first run a standalone `pwd && ls -F` (and
    `ls -d repo1 repo2 ...` to confirm which clones exist) in ONE call, then
    use **absolute paths** for every subsequent command. Prefer
    `python3 /abs/path/to/cli.py` over `cd dir && python -m pkg`. After
    confirming state, the real fix (e.g. the right venv command) works.

*   **System `python3 -m venv` fails ("ensurepip not available"):**
    The active Python lacks the `venv` module. Install `uv`
    (`curl -LsSf https://astral.sh/uv/install.sh | sh`; lands in
    `~/.local/bin`) and use `uv venv .venv && uv pip install -e .`.
    If a stale/broken `.venv` exists, `rm -rf .venv` before recreating.

*   **`Invalid AI type` / `unknown platform`:**
    The installer wants the MODEL FAMILY (`gemini`, `claude`, `codex`, ...),
    not `hermes`. Check the CLI's error output for the valid-type list.

*   **convert.sh missing files:** run `convert.sh` before `install.sh` (see
    pattern 1 above).

*   **Silent Failures:** if a script ends with no clear success/error, verify
    its effect (`ls ~/.hermes/skills/...`, `hermes plugins list`,
    `cat ~/.hermes/config.yaml`).

*   **Dependency Issues:** "command not found" / missing packages → install
    the dep, then retry. Capture the *fix* here, not "X does not work".

Full runnable recipes per pattern: `references/repo-install-patterns.md`.
