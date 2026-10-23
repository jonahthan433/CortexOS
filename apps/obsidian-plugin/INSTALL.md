# CortexOS Bridge — Obsidian plugin install

Built artifacts live in `dist/cortexos-bridge/` (main.js, manifest.json, styles.css).

1. On the Obsidian machine, create/open the vault you want to use as the shared
   CortexOS vault.
2. Copy the three files into `<vault>/.obsidian/plugins/cortexos-bridge/`.
3. Obsidian → Settings → Community plugins → enable **CortexOS Bridge**
   (turn off Restricted mode if prompted).
4. In the plugin settings set:
   - **Bridge URL**: `https://192.168.1.13:8765` (self-signed cert: open
     `https://192.168.1.13:8765` once in a browser and accept it).
   - **Bridge token**: run `.venv/bin/cortexos token` on CT103 and paste the value.
5. Verify: run any skill from the plugin command palette entry
   ("CortexOS: Run a skill") — the answer appears in the modal and the receipt
   lands in the Bridge vault.

Notes
- The plugin talks to the same Bridge as the HUD (skill grid, voice, terminal,
  approvals). No second service, no extra credentials beyond the bridge token.
- Rebuild after any `main.ts` change:
  `cd apps/obsidian-plugin && npm run build` (emits main.js + styles.css and
  refreshes `dist/cortexos-bridge/`).
- The Bridge vault (`/opt/cortexos/app/vault` on CT103, root-owned) is NOT this
  vault. Point Obsidian at a vault you own; the Bridge only ever writes into its
  own vault directory.