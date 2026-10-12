# web-page-rebuild

# Web Page Rebuild (token-faithful, slop-free, visually verified)

## When to use
- "Rebuild this landing page", "clone this site's style", "make our page look like X"
- "restyle using exact colors/fonts from Y", or the user pastes screenshots and says "make it look like this"
- Any task where fidelity to a live reference matters and you must NOT guess the design system.

## Workflow (in order)

### 1. Extract exact design tokens — never guess
The reference's real colors/fonts/spacing live in its CSS, not in your memory.
```bash
curl -sSL <site>/ -o page.html
# find the stylesheet href in page.html, then:
curl -sSL <site>/<css-path> -o site.css
```
Parse with Python (regex on `:root{--var:...}` + component rules) to pull: color tokens, font families + weights (often a Google Fonts `@import`), base type scale, spacing rhythm, layout grid. Also extract the real copy/IA — nav labels, section headings, code snippets, endpoint lists — via script, not by eye. See `references/design-token-extraction.md`.

### 2. Rebuild as a single self-contained .html
- One file. Inline `<style>`, minimal vanilla JS. No framework, no build step (YAGNI).
- Use the EXACT tokens from step 1 — same hex, same font stack, same spacing. `#0a0a0a` vs `#000` is visibly different.
- KILL slop by default: drop fake "Sign up" funnel modals, upsell banners, hero animations, marketing prose, redundant "copy to LLM" walls — UNLESS the reference screenshot shows them and the user wants parity (see Pitfalls).
- Keep it functional where cheap: tab switching, copy buttons, a real fetch that surfaces 402.

### 3. Verify by RENDERING and LOOKING — never claim done unseen
If `browser_exec` works, use it. If it reports `chrome-not-running` / no supported browser, install headless Chromium via Playwright (see `references/visual-verification.md`) and render to PNG, then `vision_analyze` with a specific question (layout regions, overlaps, text overflow, broken styles, truncation). Don't declare success until vision confirms no defects.

### 4. Match parity when the user sends screenshots
If the user pastes screenshots and says "make it look like this", they want CONTENT parity, not a stripped minimal version. My first rebuild over-stripped the Agent tab (removed the system-prompt block, tutorials, rate-limit bullets) thinking they were slop — the user corrected by sending the real screenshots. Lesson: strip only chrome/decoration; keep every content section the reference shows. Match structure 1:1.

## Pitfalls
- **Full-page screenshots clip scrollable content.** A `height:100vh` grid + `overflow:hidden` app shell (very common) makes `full_page=True` screenshots cut off mid-content. Inject a capture-only style override before shooting: `.app{height:auto!important} main,.tab-content,.scroll{overflow:visible!important}`. Verification artifact only — never edit the real CSS for this.
- **Don't trust a viewport screenshot for "is text cut off?"** It only shows the fold. Expand + full-page (with the override) to confirm.
- **No browser / display available — curl-CSS extraction IS the valid path.**
  If `computer_use` reports "no DISPLAY / X11 not reachable" and `browser_exec`
  reports "chrome-not-running", you CANNOT render. Do NOT claim a visual check
  you didn't do. Instead: (1) `curl` the page + its CSS bundle, extract `:root`
  tokens + component rules (step 1) — this is pixel-accurate for
  colors/fonts/spacing; (2) verify the LIVE result by `curl`-ing the deployed
  URL and grepping for the tokens you set (`grep -oE '--bg:#0a0a0a|--orange:#ff5500'`)
  plus a slop grep (`grep -ciE 'Powered by|seamless'`) to prove the old/stale
  version isn't still served (a stale deploy often means a git-divergent prod —
  see the cortexcloud-api-dev drift guard). That combination is a legitimate
  "see exactly" verification when no renderer exists. Only say "rendered and
  visually confirmed" if you actually rendered.
- **Guessing tokens = wrong fidelity.** Always curl the real CSS.
- **AI slop ≠ reference content.** "Agent System Prompt" blocks, endpoint tables, payment-rail tutorials are content, not slop. Only kill decorative/marketing chrome.
- **Verify the live API contract if you wire calls.** A real `POST` to the reference returned `x402Version:2, accepts[].scheme=exact, network:eip155:8453, amount:10000` (= $0.01 USDC) — my frontend's 402 handling matched. Smoke-test before claiming done.

## References
- `references/visual-verification.md` — Playwright install + render + vision_verify recipe, clipping fix, mobile capture.
- `references/design-token-extraction.md` — Python snippet to pull `:root` vars + component rules from minified CSS.
- `scripts/render_page.py` — reusable render-to-PNG (desktop + mobile + expanded full-page).

## Output discipline (user prefers terse, token-faithful)
Rebuild first, then 1–3 lines on what was skipped and when to add it. No essays, no design tours.
