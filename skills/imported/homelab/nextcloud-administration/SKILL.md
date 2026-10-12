# nextcloud-administration

# Nextcloud administration (homelab CT 101, Alpine)

## When to Use
Use when the user reports missing Nextcloud video thumbnails, asks to update/upgrade
Nextcloud on the homelab, or wants Nextcloud diagnostics/fixes. Covers CT 101 only.
For host/other-CT ops, see proxmox-homelab-administration.

Nextcloud runs in LXC CT 101 on PVE (192.168.1.11). Webroot `/usr/share/webapps/nextcloud`, data `/var/lib/nextcloud/data`, runs as user `nextcloud`, PHP 8.5 (`php85`). Invoke occ as: `pct exec 101 -- bash -c 'occ <args>'`.

## Enable video thumbnails (MP4/MOV/MKV/WebM)
**Root cause of "no video thumbnails in iPhone app":** `enabledPreviewProviders` is empty/default and `OC\Preview\Movie` is NOT enabled by default. Images work; videos don't.

**Fix:** enable the Movie provider + standard set. The provider class is `OC\Preview\Movie`.

```sh
# ALL providers in ONE call — each `config:app:set` OVERWRITES, it does not append.
pct exec 101 -- bash -c 'occ config:app:set preview enabledPreviewProviders --value '"'"'["OC\\Preview\\Movie","OC\\Preview\\Image","OC\\Preview\\JPEG","OC\\Preview\\PNG","OC\\Preview\\GIF","OC\\Preview\\BMP","OC\\Preview\\HEIC","OC\\Preview\\TIFF","OC\\Preview\\PDF","OC\\Preview\\MP3","OC\\Preview\\MarkDown","OC\\Preview\\Office","OC\\Preview\\OpenDocument"]'"'"''
# verify
pct exec 101 -- bash -c 'occ config:app:get preview enabledPreviewProviders'
# test on one real video (proves ffmpeg pipeline works)
pct exec 101 -- bash -c 'occ preview:generate "USER/files/path/to/video.mp4"'
```
ffmpeg must be present (`which ffmpeg`); imagick/gd PHP modules too. See `references/preview-providers.md`.

## occ upgrade leaves maintenance mode ON
After `apt`/`apk` bumps the NC package, `occ status` shows `needsDbUpgrade: true`. Run the upgrade, then **you must disable maintenance manually**:
```sh
pct exec 101 -- bash -c 'occ upgrade'            # DB migrates, but "Maintenance mode is kept active"
pct exec 101 -- bash -c 'occ maintenance:mode --off'
pct exec 101 -- bash -c 'occ check'              # should be clean
```
See `references/upgrade-maintenance.md`.

## pct exec quirks (cost me time — avoid)
- **PATH loss in subshells:** `D=$(occ config:system:get datadirectory)` inside a nested `bash -c` loses `occ` from PATH → "occ: command not found". Call `occ` directly, or use `bash -lc`.
- **Background job logs land INSIDE the CT.** `nohup pct exec 101 -- bash -c '... > /tmp/x.log &'` writes `/tmp/x.log` *in the container*, not on the PVE host. Read it with `pct exec 101 -- tail /tmp/x.log`.
- **Avoid long chained one-liners** (`&&`, `$()`, parens) — the agent command parser blocks oversized payloads. Push a script instead: `scp script root@192.168.1.11:/tmp/s.sh` → `pct push 101 /tmp/s.sh /tmp/s.sh` → `pct exec 101 -- bash /tmp/s.sh`.
- Killing stray debug processes: a hung `find /` from a timed-out probe can keep running; `pct exec 101 -- kill <pid>` it.

## Fix preview aspect ratio (square → widescreen)
Default `preview_max_x=2048` + `preview_max_y=2048` forces square previews. To use 16:9:
```sh
pct exec 101 -- bash -c 'occ config:system:set preview_max_x --value 1920'
pct exec 101 -- bash -c 'occ config:system:set preview_max_y --value 1080'
```
**Values are STRINGS in config.php** (`'preview_max_x' => '1920'`) — `occ config:system:get` returns the raw string. Always quote the `--value`.

Then **re-index and regenerate**:
```sh
# 1. Re-scan the user's files (so NC knows about renamed/moved files)
pct exec 101 -- bash -c 'occ files:scan --path ncAdmin/files/Photos'
# 2. Clear old cached square previews (background — thousands of files)
pct exec 101 -- bash -c 'nohup rm -rf /var/lib/nextcloud/data/appdata_*/preview/* > /dev/null 2>&1 & disown'
# 3. Regenerate on access — open Photos/Memories app; each image gets a fresh 16:9 preview.
```

Or set one dimension to `0` for free aspect ratio (e.g., `preview_max_x=2048`, `preview_max_y=0`).

**Pitfall:** `preview_max_x`/`preview_max_y` controls **preview generation** (the stored thumbnail files), NOT the **Files app grid display**. The Files grid hardcodes `data-preview-x="250" data-preview-y="250"` in its template and applies CSS `object-fit: cover`, cropping all thumbnails to square regardless of the source preview aspect ratio. Changing these values alone will NOT fix the Files grid view — install the **Photos** app (separate, not bundled) for a natural-aspect-ratio gallery.

## Dot-folder indexing (invisible files)
Nextcloud **ignores files and folders starting with `.`** (dot-files) during indexing. A folder named `.Raw` (common for photo RAW files) will NOT appear in the file list or get previews generated, even though the files physically exist on disk.

**Diagnosis:** Files exist on disk (`ls -la .../.Raw/` shows them) but `occ files:scan` reports them as 0 new files, and they're absent from the web UI.

**Fix:** rename the dot-folder to a non-dot prefix, then re-scan:
```sh
# On the container (as root, since the folder may be owned by the web user)
pct exec 101 -- bash -c 'mv /var/lib/nextcloud/data/USER/files/Path/.Raw /var/lib/nextcloud/data/USER/files/Path/Raw'
pct exec 101 -- bash -c 'occ files:scan --path USER/files/Path'
```
The files immediately appear in the web UI and previews generate on access. This affects any dot-folder (`.thumbnails`, `.cache`, `.synced`, etc.) — rename them all to proceed with the dot.

**Version note:** CT101 runs Nextcloud 33.0.8. The `occ preview:generate-all` command hits a known SQL bug in NC33 (`SQLSTATE[23000]: Column 'file_id' in WHERE is ambiguous`) and also times out on large libraries. **Never run `preview:generate-all` on this instance.** Delete the preview dir and let NC regenerate per-file on access instead.

---

## Face-based photo organization (Recognize + Memories)

### When to use
User asks to set up face recognition / People/Faces view on the Nextcloud instance, or to index an existing photo library with face detection.

### Apps involved
- **Recognize 11.x** — face detection + object/landmark/music/video tagging. Installs via `occ app:install recognize`.
- **Memories** — photo timeline + memories. Installs via `occ app:install memories`.
- **People app** — face browsing UI that Recognize integrates with. **Not on the NC 33 app store** — must be installed manually from GitHub if a visual face browser is needed (see `references/face-recognition.md`).

### Installation + enablement
```sh
pct exec 101 -- bash -c 'occ app:install recognize memories'
pct exec 101 -- bash -c 'occ app:enable recognize memories'
```
Verify: `pct exec 101 -- bash -c 'occ app:list | grep -iE "recognize|memories"'`.

### Dot-folder photos and face indexing
Face detection only indexes files Nextcloud can see. If the photo library has a **dot-folder** (e.g. `Photos/.Raw/`), those files are invisible to Recognize and will NOT be scanned.

**Fix (same as dot-folder section above):** rename `.Raw` → `Raw`, then trigger a re-scan so Recognize picks up the newly visible files:
```sh
pct exec 101 -- bash -c 'mv /var/lib/nextcloud/data/USER/files/Photos/.Raw /var/lib/nextcloud/data/USER/files/Photos/Raw'
# Trigger Recognize to re-scan all visible files (adds to background job queue)
pct exec 101 -- bash -c 'occ recognize:rescan'
```
**Do NOT move or modify the original photo files** — renaming the parent folder is safe and does not touch file contents.

### Enable face recognition
```sh
pct exec 101 -- bash -c 'occ config:app:set recognize faces.enabled --value true'
pct exec 101 -- bash -c 'occ config:app:set recognize faces.batchSize --value 50'
```
Default batch size is 500; reduce to 50 on resource-constrained CTs (CT101 has 3.8GB RAM, 2 cores).

### Download ML models (required before indexing)
```sh
pct exec 101 -- bash -c 'occ recognize:download-models'
```
Downloads ~2.8GB of models to `/var/lib/nextcloud/apps/recognize/data/`. **Runs synchronously and can take 10+ minutes** — run in background:
```sh
# Push a wrapper script to the CT, then run it in background
# See scripts/download-models.sh (pushes to CT, nohup's the occ command, logs to /tmp/recognize_models.log)
scp scripts/download-models.sh pve:/tmp/download-models.sh
pct push 101 /tmp/download-models.sh /tmp/download-models.sh
pct exec 101 -- bash -c 'chmod +x /tmp/download-models.sh && nohup bash /tmp/download-models.sh > /tmp/download-models.out 2>&1 &'
# Poll: pct exec 101 -- bash -c 'tail -5 /tmp/recognize_models.log; ls -lh /var/lib/nextcloud/apps/recognize/models.tar.gz 2>/dev/null'
```
Models include: face detection (@vladmandic/face-api), object detection (efficientnetv2 ~793MB), landmarks (per-region), video action (movinet ~54MB), music genre (musicnn ~50MB).

### Background indexing (safe, non-blocking)
Recognize's `MaintenanceJob` runs every 12 hours and processes the face/object/landmark queues. The Nextcloud cron (`*/5 * * * * php -f /usr/share/webapps/nextcloud/cron.php` in the `nextcloud` user's crontab) drives background jobs.

**Verify cron is running:**
```sh
pct exec 101 -- bash -c 'crontab -l -u nextcloud | grep cron.php'
# Force a cron run to process queued jobs now:
su -s /bin/sh nextcloud -c "php /usr/share/webapps/nextcloud/cron.php" 2>&1 | tail -5
```

**Check queue progress:**
```sh
pct exec 101 -- bash -c 'occ config:list recognize | grep -E "faces.status|faces.lastFile|faces.batchSize"'
```
`faces.status: true` + advancing `faces.lastFile` timestamp = indexing is active.

### Check face detection results (database)
Recognize stores detections in MySQL tables `oc_recognize_face_detections` and `oc_recognize_face_clusters`. To inspect without the People UI:
```sh
# Push check script to CT, run as nextcloud user
scp scripts/check-faces.sh pve:/tmp/check-faces.sh
pct push 101 /tmp/check-faces.sh /tmp/check-faces.sh
pct exec 101 -- su -s /bin/sh nextcloud -c 'php /tmp/check-faces.sh'
```
See `scripts/check-faces.sh` for the PHP script that queries face counts, file paths, and cluster status.

### Clustering threshold (key limitation)
Recognize uses HDBSCAN for face clustering. The minimum dataset size is **120 face detections per user** (`FaceClusterAnalyzer::MIN_DATASET_SIZE = 120`). Below that, `occ recognize:cluster-faces` exits with "Not enough face detections found" and no clusters are created.

**Workarounds:**
- Add more photos to grow the detection count past 120 for at least one user.
- Once enough detections exist, cluster manually: `pct exec 101 -- bash -c 'occ recognize:cluster-faces'`.
- The `MaintenanceJob` auto-triggers clustering every 12h when users have unclustered detections — but only if the 120 threshold is met.

### Accessing People/Faces view
**Without the People app:** Recognize stores face data but has **no standalone face-browsing UI**. The only access path is the WebDAV collection at `https://NC_HOST/remote.php/dav/faces/` (requires authentication; returns 401 without credentials).

**With the People app:** Install from GitHub (not on NC 33 app store):
```sh
# Manual install — clone into the apps dir, then enable
pct exec 101 -- bash -c 'git clone https://github.com/nextcloud/people /var/lib/nextcloud/apps/people'
pct exec 101 -- bash -c 'occ app:enable people'
```
After People is enabled and clustering has run, the People app provides a face album/browser at `https://NC_HOST/index.php/apps/people/`. See `references/face-recognition.md` for the full People app integration notes.

### Pitfalls
- **Recognize admin UI is settings-only** — `recognize-admin.js` (3.4MB) is the admin settings page; `recognize-main.js` is 0 bytes (no user-facing UI). Do not expect a face browser after installing Recognize alone.
- **models.tar.gz download is synchronous** — `occ recognize:download-models` blocks for 10+ minutes. Always background it.
- **WASM mode on Alpine** — CT101 uses `tensorflow.purejs: true` (WebAssembly, no GPU). Face detection works but is CPU-bound. Batch size 50 is appropriate for 2-core/3.8GB CTs.
- **memories last_index** — check `occ config:list memories | grep last_index` to see if Memories has indexed the library. It only scans NC-visible files, so the `.Raw` → `Raw` rename must happen before Memories indexing too.
- **Do not run face clustering below 120 detections** — it silently exits. Check detection count first.
- **SSH inline PHP quoting** — complex PHP one-liners through `ssh pve "pct exec ..."` mangle quotes. Write PHP to a file, push to CT, run from file.
- **SCP to PVE host ≠ SCP to CT** — `scp file pve:/tmp/` lands on the PVE host, not inside CT101. Use `cat file | ssh pve "pct exec 101 -- bash -c 'cat > /tmp/file'"` or `scp file pve:/tmp/` then `pct push 101 /tmp/file /tmp/file`.

## Files app grid shows square-cropped thumbnails (no aspect ratio)
Symptom: Files app grid view crops all image thumbnails to square tiles, ignoring the source image's aspect ratio. The Photos app (if installed) shows a masonry/justified grid with natural aspect ratios.

**Root cause:** The Files app template (`apps/files/templates/list.php`) hardcodes `data-preview-x="250" data-preview-y="250"` and the CSS enforces `object-fit: cover` on thumbnails. This is a Files app design choice, not a preview config issue.

**Check if Photos app is installed:**
```sh
ssh pve "pct exec 101 -- bash -c 'occ app:list 2>&1 | grep -i photo'"
```

**Fix 1 (recommended): Install the Photos app** — provides a proper gallery with natural aspect ratio grid, timeline, and albums:
```sh
ssh pve "pct exec 101 -- bash -c 'occ app:install photos'"
```
Access at `https://nc/index.php/apps/photos/`. The Photos app has an "Enable squared photos view" toggle (stored as `croppedLayout` per-user) — when OFF, images display in their natural aspect ratio.

**Fix 2: Custom CSS override for Files grid** — if you must use the Files app grid, add via **Settings → Theming → Custom CSS**:
```css
.files-filestable.view-grid .thumbnail {
    object-fit: contain !important;
    background: var(--color-background-darker, #e8e8e8);
}
```
Then hard-refresh (Ctrl+Shift+R). This is a band-aid that may break on updates.

**Pitfall:** The Photos app is NOT bundled with Nextcloud server — it's a separate app that must be explicitly installed. CT 101 (NC 33.0.8) ships with only `viewer` and `previewgenerator`, not `photos`.

## `preview:generate-all` SQL bug in Nextcloud 33
`occ preview:generate-all` fails with `SQLSTATE[23000]: Integrity constraint violation: 1052 Column 'file_id' in WHERE is ambiguous`. Regenerate per-file instead, or delete the preview dir and let NC regenerate on access.

## Memory pressure: PHP-FPM pool reduction
On memory-constrained CTs (like CT101 with 3GB total), Nextcloud's default PHP-FPM pool (`pm.max_children=5`) can consume ~230MB. Reduce `/etc/php85/php-fpm.d/www.conf`:
```
pm.max_children = 2-3
pm.start_servers = 1
pm.min_spare_servers = 1
pm.max_spare_servers = 2
```
Then reload: `pct exec 101 -- bash -c 'kill -USR2 $(cat /run/php85/php-fpm.pid 2>/dev/null) 2>/dev/null || rc-service php-fpm85 reload'`
This saves ~100-150MB of RSS. The preview:generate-all command had a SQL bug in NC33 (Column 'file_id' ambiguous) — delete the preview dir instead and let NC regenerate on access.

## Diagnostic first-aid
- `occ check` (clean = no errors), `occ status` (version, needsDbUpgrade, maintenance).
- Previews stored under `appdata_<id>/preview/<fileid>/` — don't `find` the whole tree (slow on big libraries); trust `occ preview:generate` OK output.
- Regenerate previews for existing videos: loop `occ preview:generate` per file (some 0-frame WhatsApp clips fail — non-critical).

See `references/preview-providers.md` and `references/upgrade-maintenance.md`.
