# nextjs-landing-page

# Premium Next.js Landing Page (static export)

## When to use
- User wants a marketing/landing page that looks like a well-funded dev-tool startup (Vercel/Linear/Stripe polish): dark mode, glassmorphism, animated gradients/blobs, scroll-triggered reveals, magnetic CTAs.
- The page must **replace an existing served page** (e.g. FastAPI serves `static/index.html` at `/`) — prefer `output: 'export'` so no new Node server/port is needed.
- Do NOT clone another site's branding/copy/assets. Study competitors for *quality and UX only*, then build an original identity.

## Stack (pin these — see Pitfalls)
- Next.js **15.x** (NOT 16 — see bus-error pitfall)
- React 19, TypeScript
- Tailwind CSS **v4** (CSS-based `@theme`, no `tailwind.config.js`)
- `motion` (the Framer Motion package; import from `motion/react`)

## Build sequence
1. Scaffold: `npx create-next-app@latest <dir> --ts --tailwind --app --eslint --src-dir --import-alias "@/*" --no-turbopack --use-npm`
2. Pin Next to 15: `npm i next@15.5.4 eslint-config-next@15.5.4 --save-exact` (verify SWC loads — see Pitfalls).
3. `npm i motion`, plus install `@eslint/eslintrc` for the flat eslint config fix below.
4. `next.config.ts`: set `output: 'export'`, `images: { unoptimized: true }`, `trailingSlash: true`, `reactStrictMode: true`. For export under a monorepo/odd root, add `outputFileTracingRoot: __dirname`.
5. `globals.css`: Tailwind v4 `@import "tailwindcss";` + `@theme` tokens (colors, fonts, spacing on 8px scale), plus custom `@keyframes` for blobs/marquee/pulse and utility classes (`.glass`, `.glass-strong`, `.border-gradient`, `.text-gradient`). Keep dark by default.
6. `layout.tsx`: load fonts via `next/font/google` (sans + serif + mono), set `<html lang>`, metadata (title/description/canonical/OG/Twitter), JSON-LD `<script>`, skip-link, and `prefers-reduced-motion` handled per-component.
7. Build reusable primitives (`src/components/ui/`): `Reveal` (scroll-in via `useInView`), `MagneticButton` (spring-follow + `motion.a`), `GlassCard` (3D tilt + spotlight), `Blobs`/`GridBackground`, `Counter` (count-up), `CopyButton`, `Icon` (inline SVG set), `SectionHeading`, `CodeBlock` (zero-dep highlighter for Lighthouse).
8. Build sections (`src/components/sections/`): Navbar (transparent→solid on scroll, mobile menu), Hero (animated gradient + LIVE data card that fetches a real endpoint with fallback), Architecture (animated SVG flow w/ traveling packets), Features grid, Providers (marquee + model table), Capabilities, CodeExamples (tabbed + copy), Metrics (Counters), Pricing (3 tiers), Security, Testimonials/logos, FAQ (accordion), FinalCTA, Footer (animated). Assemble in `page.tsx` with a scroll-progress bar.
9. SEO: `app/sitemap.ts` + `app/robots.ts` (both `export const dynamic = 'force-static'`), `public/og.svg` (1200×630), JSON-LD in layout.
10. Content in `src/lib/content.ts` (one file: nav, features, models, pricing, faq, footer) — original copy, invented-but-plausible prices (never fake transaction volume).
11. `lib/motion.ts`: shared `Variants` (fadeUp, stagger, scaleIn) for consistency.

## Verify before claiming done
- `npm run build` must exit 0 and print the export route table.
- Serve `out/` (`python3 -m http.server <port>`) and open in browser: screenshot + `browser_vision` to confirm premium look; check `browser_console` for **0 JS errors**.
- A static full-page screenshot will show sections at `opacity:0` (scroll reveals) — that is CORRECT, not a bug.

## Deploy (replace FastAPI-served page) — generic steps
1. `rsync -a out/ <host>:/opt/<app>/static_site/`
2. In the FastAPI app, mount `StaticFiles` and point `/` at `static_site/index.html` (swap the old `index.html` route).
3. Restart the service, hit `/`, confirm live. (CortexCloud-specific values in references/cortexcloud-deploy.md.)

## PITFALLS
### Next 16 SWC bus-error on new glibc
On Debian 13 / glibc 2.41 (and similar very-new libc), `next@16` build dies instantly with `Bus error` (exit 135) — its prebuilt `@next/swc-linux-x64-gnu` binary crashes on `require`. **Diagnosis:** `node -e "require('@next/swc-linux-x64-gnu')"` → Bus error. **Fix:** pin `next@15.5.4` + `eslint-config-next@15.5.4`; re-test `require('@next/swc-linux-x64-gnu')` → should print OK. Next 15 has a compatible SWC build and still works. Full recipe in references/next16-swc-buserror.md.

### eslint-config-next flat config breakage (Next 15)
Next 15's `eslint-config-next` subpaths are `.js` (`eslint-config-next/core-web-vitals.js`), but the Next-16-style `eslint.config.mjs` imports `eslint-config-next/core-web-vitals` (no ext) and fails with "Cannot find module". **Fix:** replace with `@eslint/eslintrc` `FlatCompat`:
```
import { FlatCompat } from "@eslint/eslintrc";
const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });
export default [...compat.extends("next/core-web-vitals", "next/typescript"), { ignores: [".next/**","out/**","build/**","next-env.d.ts"] }];
```
and `npm i -D @eslint/eslintrc`.

### Motion hook/type traps
- `useTransform` must NOT be called inside JSX `style={{ background: useTransform(...) }}` — that's a conditional hook (react-hooks/rules-of-hooks error). Hoist it to a `const spotlightBg = useTransform(...)` before return.
- `motion.a` does not accept raw `React.AnchorHTMLAttributes` spread — its `onDrag` conflicts with Motion's drag signature. Type the component's props explicitly (omit the spread) and pass `aria-label` directly.
- Remove unused imports (e.g. `useTransform`) — `prefer-const`/no-unused-vars will fail `next build` lint.

### Static export constraints
No API routes, ISR, or server functions. Fetch live data **client-side** from an existing endpoint. Set `images.unoptimized` (no image optimizer in export).

### Serving root-level assets under a static export (FastAPI/nginx)
Static export puts JS/CSS under `/_next/`, but files in `public/` (logo, og.svg, favicon) render at the site root (`/logo.png`). A `/_next` StaticFiles mount does NOT serve them. For FastAPI, add a guarded catch-all BEFORE the mount:
```python
@app.get("/{asset}", include_in_schema=False)
async def _site_asset(asset: str):
    from fastapi.responses import FileResponse
    from fastapi import HTTPException
    import os
    allowed = (".png",".jpg",".jpeg",".svg",".ico",".webp",".txt",".xml",".json",".woff2",".woff")
    if not asset.lower().endswith(allowed):
        raise HTTPException(404)
    path = os.path.join("/srv/site", asset)
    if not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path)
```
Explicit routes (`/health`, `/docs`, API prefixes) are registered earlier so they win; the extension whitelist blocks catching real API paths.

### CDN/edge caches 404s (Cloudflare)
If you deploy a new root asset and curl it publicly BEFORE the serve route exists, Cloudflare (or any edge cache) caches the `404` (`cf-cache-status: HIT`, `age:177`) and keeps serving it for hours even after the origin is fixed. Symptom: origin returns 200 but the public URL returns 404. **Fix without dashboard access:** ship the asset under a FRESH filename the CDN has never seen (e.g. `cortex-logo-v2.png`) and update all references (`cf-cache-status: MISS` confirms bypass). Preferred fix: purge the cache for that path or set a Cache Rule to bypass caching for asset paths.

### Brand assets: use what the user gives
When the user supplies a real brand asset (logo, screenshot, image), use it verbatim — do NOT substitute an invented mark/placeholder glyph. Honor explicit "remove this badge/section" instructions literally instead of keeping invented filler. Reviews often arrive as mobile screenshots: read them with vision_analyze rather than re-fetching or asking the user to resend.

## References
- `references/next16-swc-buserror.md` — full repro + fix for the Next 16 `Bus error` (pin to 15.5.4).
- `references/secure-remote-push.md` — push from a repo host with no GitHub creds (token via stdin → 600 file → `credential.helper=store` → delete).
- `references/cortexcloud-deploy.md` — CortexCloud-specific values (CT 105 path `/opt/CortexCloudAPI`, FastAPI `/` swap, live endpoint `https://api.cortexcloud.org/x402/v1/activity`).
