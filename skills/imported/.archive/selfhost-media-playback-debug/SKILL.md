# selfhost-media-playback-debug

# Self-Hosted Media Playback Debugging

Class of work: diagnosing why a **video plays in the web UI but not in the mobile app** of a self-hosted service. The pattern is universal — Nextcloud, Immich, Jellyfin, PhotoPrism, Plex, Synology Photos, ownCloud all share the same shape. Symptom: browser `<video>` element plays fine, AVPlayer / ExoPlayer / native player in the official app fails silently or shows a generic "can't play" error.

## The diagnostic ladder (in order)

1. **Segment by user-agent in the access log.** Browser and mobile app use distinct user-agents. Look at the **actual HTTP requests** each makes for a single video the user reports as broken:
   ```bash
   # nginx: typical access log
   tail -F /var/log/nginx/access.log | grep -iE 'mp4|webm|mov|m4v|ts|m3u8'
   ```
   Compare what the browser does vs the app. If the app makes **fewer requests** or **returns 4xx** on the file the browser streams successfully, the app is using a different code path.

2. **Identify the request the app makes that the browser does not.** Common ones:
   - `GET /index.php/core/preview?fileId=...` (Nextcloud) — preview/thumbnail pipeline
   - `GET /api/v1/media/<id>/thumbnail/...` (Immich)
   - `GET /Video/<id>/stream` (Jellyfin)
   - `GET /webapi/entry/path/...` (Synology Photos)
   - `GET /library/parts/<id>/direct-stream/...` (Plex)
   The app's player needs a derivative of the original (HLS, transcoded MP4, or preview thumb). If that derivative endpoint returns 404, the app fails — the browser bypasses it by streaming the original directly.

3. **Check the derivative generator is installed and working.** Almost every service needs an external binary:
   - Nextcloud → **`ffmpeg`** (not just `convert`/ImageMagick)
   - Immich → `ffmpeg` + `ffprobe` (transcoding disabled without them)
   - Jellyfin → `ffmpeg` (with hardware accel optional)
   - PhotoPrism → `ffmpeg`
   - Plex → handles internally, but needs Hardware Transcoding or the mobile app fails on HEVC
   ```bash
   # quick check on the host
   which ffmpeg; ffmpeg -version | head -1
   ```
   `which ffmpeg` returning empty is the #1 root cause. Install the distro package: `apt install ffmpeg` / `apk add ffmpeg` / `dnf install ffmpeg`.

4. **Trigger generation of previews/thumbnails, then re-tap the file in the app.** The fix is often a one-time regeneration:
   ```bash
   # Nextcloud
   occ preview:generate-all
   # Immich
   docker exec -u node immich_server node dist/cli/main.js upload --recursive
   # Jellyfin — runs on library scan, no manual step
   ```

5. **Verify in the log.** After the fix, the same derivative request should return **200** and the app should follow up with a real stream `GET` returning **206** (partial content — AVPlayer uses HTTP Range).

## Why this class of bug happens

Mobile apps use `AVPlayer` (iOS) / `ExoPlayer` (Android) which are **strict**:
- They need HTTP Range support → server must return 206
- They need a proper `Content-Type` (not `application/octet-stream`)
- Many need **HLS or a transcoded MP4** (because direct MP4 with `moov` at the end of the file is not seekable without re-buffering)
- They fail **silently** on TLS errors against self-signed certs (browser shows "Not Secure", lets you click through; the player just refuses)
- They depend on a **preview / derivative** endpoint that the browser does not call

The browser's `<video>` element is forgiving: streams whatever bytes it gets, plays when enough is buffered, ignores `Content-Type` quirks. That's why the same file works in one and not the other.

## Common root causes (ranked by frequency)

| # | Cause | Fix |
|---|-------|-----|
| 1 | `ffmpeg` (or other transcode binary) not installed in the service's container/VM | Install the package; restart the service worker |
| 2 | Preview/thumbnail generation never run for the existing library | Run the one-shot `preview:generate` / library-rescan command |
| 3 | Self-signed cert + mobile app: TLS handshake fails silently for AVPlayer | Install the cert on the device (iOS: Settings → Profile Downloaded → Install + Trust in Certificate Trust Settings) OR use a real cert (Caddy/Let's Encrypt) |
| 4 | Nginx `http2 on` + very large files on weak clients | Try `http2 off` for the video path, or `keepalive_timeout 0` for big files |
| 5 | App-specific transcoding disabled / wrong config (e.g. Immich's `TRANSCODE_POLICY`, Jellyfin's hardware accel) | Enable the policy; verify with the app's log |
| 6 | PHP/FCGI timeout too low for the app's preview endpoint (`fastcgi_read_timeout 30s` cuts off a 5s generation) | Raise `fastcgi_read_timeout` to ≥120s in nginx |
| 7 | App depends on a streaming app the service does not have installed (Nextcloud Memories, Nextcloud Video, etc.) | Install the companion app; the iOS client may talk only to that endpoint |
| 8 | File looks iOS-compatible but `moov` atom is at the end of the MP4 | Re-mux with `ffmpeg -c copy -movflags +faststart` (browser is forgiving, AVPlayer is not) |

## Pitfalls

- **Verify the VMID/service mapping before inspecting.** A service may move to a
  different container over time, and environment notes can point at the wrong
  VMID. Run `pct list` on the host and match the **Name** column to the service
  the user asked about, then use that VMID everywhere.
- **Don't just look at the response on the URL the user typed.** The user says "video X doesn't play" — that doesn't mean URL X is the failing one. The app probably requests a *different* URL (preview, transcoded stream) and fails on THAT.
- **Don't trust "it works on my desktop."** Browser and mobile player have completely different tolerance profiles. If the user says only the app fails, the bug is in the app's code path, not the web UI.
- **Don't assume it's a Range request problem.** Always check the actual request the app makes. If the app doesn't even GET the file (only PROPFINDs/HEADs), the missing request is the symptom — the upstream pipeline is the cause.
- **404 body size of 2 bytes** (Nextcloud: `404 2 "-"`) is a strong signal of "endpoint exists but no derivative yet", not a routing/auth bug. Don't waste time on auth.
- **Re-uploading won't help.** A fresh upload may auto-generate previews; an old one won't. The fix is `preview:generate-all` (or library rescan) on the existing data.

## Cross-service cheat sheet

| Service | Derivative endpoint | Binary | Regen command |
|---------|--------------------|--------|---------------|
| Nextcloud | `/index.php/core/preview?fileId=...` | `ffmpeg` | `occ preview:generate-all` |
| Nextcloud (Memories app) | Memories HLS playlist | `ffmpeg` + Memories app | Re-scan in Memories |
| Immich | `/api/v1/asset/thumbnail/...` | `ffmpeg`, `ffprobe` | Library rescan + `jobs` queue |
| Jellyfin | `/Videos/<id>/stream` (HLS) | `ffmpeg` | Auto on library scan; check dashboard "Encoding" |
| PhotoPrism | `/api/v1/photos/<uid>/thumb/...` | `ffmpeg` | `photoprism index` |
| Plex | `/library/parts/<id>/index.bif` (video preview thumbs) | Plex internal; `ffmpeg` for HW transcode | Auto on library scan |
| Synology Photos | `/webapi/.../streaming/...` | Synology internal (SynoCodec) | Auto on upload; check package |

## Verification

After the fix, the failing app should:
1. Issue the derivative `GET` → return **200** (was 404)
2. Issue the stream `GET` (or play the derivative) → return **206** with `Content-Range`
3. Start playback within ~2 seconds in the app

If the derivative returns 200 but stream still 200 (not 206), the Range handler is broken — check the proxy (`nginx`, Caddy) and the upstream `fastcgi_read_timeout` / `proxy_read_timeout`.

## Related

- For getting access to the service's container/VM, see `proxmox-homelab-administration` (most self-hosted services in this homelab live in Proxmox LXCs).
- For Nextcloud specifically, the bug is documented in detail in `references/nextcloud-video-playback.md`.
- A later session where files were iOS-compatible but the root cause was not yet
  isolated is captured in `references/nextcloud-video-playback-2026-07-26.md`.
