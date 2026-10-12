# expo-react-native-dev

# Expo React Native App Dev (headless verification)

Use when scaffolding, building, or verifying an Expo (React Native) iOS/Android app — especially when you CANNOT run the iOS Simulator (Linux/headless host, CI). Covers the web-export verification path, the exact dependency chain, app.json traps, and how to keep a stable UI contract while swapping mock→real services.

## When to use
- `npx create-expo-app` / existing Expo project needing a typecheck + render check you can't do on a device.
- Wiring a real backend (RAG/API) behind a mock without touching screens.
- Any "expo export" or Metro bundling error on a non-Mac host.

## Headless verification (the core technique)
You cannot run iOS Sim on Linux. Verify in this order — typecheck alone is NOT enough (it misses bundler/resolution errors that only surface at export):
1. `npx tsc --noEmit` — catches type + bad relative-import errors.
2. `npx expo export --platform web` — the real bundler pass. A clean tsc can still fail here on missing modules / unresolvable imports.
3. Serve + assert: `cd dist && python3 -m http.server 8099`, then `curl -s localhost:8099/index.js` (or grep the `_expo/assets` bundle) for a string unique to each screen (e.g. "Continue reading", "Select Version"). This proves the screen code actually made it into the bundle — not just that it compiled.
See `references/headless-verify.md` for the exact command chain + the gotchas that cost iterations.

## Dependency chain for `expo export --platform web`
Add ONLY what you import. Missing deps fail the export with "Unable to resolve module X":
- `npx expo install react-native-web react-dom @expo/metro-runtime` — required for ANY web export.
- `npx expo install react-native-gesture-handler` — required if using @react-navigation (it imports gesture-handler at the entry).
- Do NOT add `expo-blur` / `expo-linear-gradient` unless you actually `import` them — an unused plugin entry in app.json + dep breaks the export (ERR_MODULE_NOT_FOUND). Same for any expo-* module.

## app.json web config traps
- `web.bundler: "metro"` is enough. Do NOT set `web.output: "static"` with a non-router (`index.js` entry) project — it forces `expo-router/node/render.js` and fails. Leave `output` unset, or set `staticRendering: false` only if you must keep static.
- `plugins: []` when no expo module provides a plugin. Empty/unused plugin entries are harmless but dead — prefer none.
- `userInterfaceStyle: "automatic"` lets the OS switch dark mode; pair with a `useColorScheme()` theme.

## TypeScript / module-resolution pitfalls
- Screens live in `src/screens/`, so their imports are `../theme`, `../content/bible` — NOT `./theme`. tsc will flag the wrong one.
- Large JSON assets (e.g. a Bible) need a `declare module '*.json'` shim OR `resolveJsonModule: true`; without it, `Cannot find module '../assets/x.json'`.
- Modules that `import` `@react-native-async-storage/async-storage` (or any RN-only dep) BREAK a node test compile. Decouple pure logic (parsers, validators, retrievers) into JSON-free modules so tests run under `tsc -p tsconfig.test.json` with `lib: ["ES2019","DOM"]`, `types: ["node"]`, and the RN-coupled files EXCLUDED from the test `include`.

## Ponytail notes
- Keep the screen/UI contract (prop + export names) stable when swapping a mock service for the real one. The UI imports the same function; only the implementation changes. Verify with the bundle-grep step above.
- reanimated is dead weight unless you actually drive an animation with it. If you add `react-native-reanimated`, also add `react-native-reanimated/plugin` to babel.config.js plugins AND import+use an `Animated.View` somewhere — otherwise it's an unused dep + babel plugin.
- Don't fabricate a backend contract you can't reach. Build the client to the documented shape, ship a real offline fallback, and flag the live endpoint URL/params as a disclosed gap to confirm against the real OpenAPI.

## References
- `references/headless-verify.md` — exact command chain + the export errors that each fix resolves.
