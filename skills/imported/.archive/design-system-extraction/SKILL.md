# design-system-extraction

# Design System Extraction & Clean Rebuild

## When to use
- "Rebuild this landing page using its exact style / colors / fonts / spacing."
- "Clone site X's design but remove the SaaS stuff / AI slop."
- Any task needing a site's real tokens without guessing hex values.

## Lazy path (do this first — no browser needed)
Browser vision (browser_exec / computer_use) is NOT required to get exact tokens. The production CSS is the source of truth and is more precise than eyeballing a screenshot. Fetch it.

1. **Get the HTML** (curl, not a browser):
   ```
   curl -sSL -m 20 https://SITE/ -o /tmp/site.html -w "HTTP %{http_code} size %{size_download}\n"
   ```
2. **Find the CSS link** in the HTML. Modern builds (Next.js etc.) use hashed paths like `/_next/static/css/<hash>.css`. Grep for `\.css` — do NOT hardcode the hash, it changes per deploy.
3. **Fetch the CSS file**:
   ```
   curl -sSL -m 20 https://SITE/<path>.css -o /tmp/site.css
   ```
4. **Parse the minified CSS** (it's one line — do NOT read it raw with read_file, it truncates). Use Python regex to expand rules and filter to the components you care about. Recipe in `references/extract_tokens_recipe.py`.
5. **Capture `:root` vars** — that block IS the token palette (bg, panel, border, accent, text, muted, font families). This is the single most important extraction.
6. **Capture fonts** from the `@import`/`<link>` (common pairing: Inter + JetBrains Mono via Google Fonts).
7. **Rebuild as a single self-contained `index.html`** — inline the `:root` tokens and component CSS. No build step, no framework, no extra deps.

## Pitfalls
- **Chrome not running** → `browser_exec` returns `chrome-not-running: no supported browser is running`. Don't fight it. curl+CSS parse is faster and exact for tokens; vision adds nothing for hex values. (If the user explicitly demands "see it," note the limitation and proceed with curl — it's more precise anyway.)
- **Minified CSS overwhelms read_file** — one giant line; pagination truncates mid-rule. Always parse with Python (`re.findall(r'([^{}]+)\{([^{}]*)\}', css)`), filter by selector keywords, print readable.
- **Hashed asset URLs** change per deploy — always re-fetch the `<link>` from the live HTML; never hardcode a stale hash.
- **Token drift** — SPA loads one CSS; multi-page may vary. Grab the CSS the page you're cloning actually references.

## Clean-rebuild principles (kill the slop)
- Strip: sign-up/sign-in funnels, upsell banners, "Copy to LLM" nudges, walls of marketing prose, injected component-library classes (e.g. `parallel-*`), hero animations / gradient blobs, testimonial/logo clutter.
- Keep: the function. Make forms actually POST to the real endpoint; surface `402 Payment Required` honestly instead of faking auth.
- Prefer native platform features: CSS grid for layout, `<input>` for fields, `:focus` borders, `fetch` — no JS lib for what CSS/HTML already does.
- One file. No bundler. Inline tokens. Works when served beside the gateway (`window.location.origin`) or with a single `BASE` constant.

## Verify (don't claim done untested)
- Serve: `python3 -m http.server 8899` (background) → `curl -o /dev/null -w "%{http_code}" http://localhost:8899/index.html` expect 200.
- Smoke the live API the page calls: `curl -X POST .../api/search -d '{"query":"x"}'` → expect `402` with the same payment payload your frontend surfaces. Confirms your 402 handling matches reality.
- Kill the server when done (`process(action=kill)` / `pkill -f http.server`).

## References
- `references/extract_tokens_recipe.py` — exact curl + Python parse recipe, with the parallelmpp.dev token dump as a worked example.
