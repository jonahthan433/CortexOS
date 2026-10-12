# iptv-streaming

# IPTV Streaming & Playlist Curation

Covers two recurring jobs: (1) diagnosing why a stream URL fails in VLC, and
(2) building a verified-live m3u playlist from the iptv-org repo.

## VLC "can't open the MRL" — diagnose the URL, not VLC

VLC's error names no cause. Before touching VLC config, probe the URL directly:

```bash
# HEAD-ish: shows redirect target and final code
curl -sSI "<url>" | head -30

# Follow redirects, full GET, look at final status + body
curl -sSL -o /dev/null -w "code=%{http_code} size=%{size_download} type=%{content_type}\n" "<url>" --max-time 20
```

Read the result:
- **302 + Location header** → the URL is a redirect stub. Follow it before judging.
- **403 + JSON body** → upstream CDN rejection. `{"status_code":403,"message":"channel not permitted for partner"}` is geo-blocking or an expired source. Not fixable from the client.
- **200 + `#EXTM3U`** → manifest is live; if VLC still fails it's a VLC/network issue.
- **200 + HTML** → wrong endpoint, not a stream.

Common upstream failure classes (all NOT client-side bugs):
- Pluto TV `jmp2.uk` stubs that 302 to `stitcher-ipv4.pluto.tv` and return
  `channel not permitted for partner` — geo/partner block.
- Samsung/Pluto embeds that need deviceType query params.
- Streams that need a `Referer` or `User-Agent` header.

The fix is almost always: pick a different source from the playlist, don't
reconfigure VLC.

## Building a verified-live playlist from iptv-org

The repo has no per-sport playlists — it's organized by country
(`streams/{cc}.m3u`, ~326 files). There is no `us_sports.m3u`; that 404s.

Workflow:

1. **Pull regional playlists** (no sport-specific files exist):
   ```bash
   for f in us uk de es fr it in ca au; do
     curl -sS "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/${f}.m3u" -o ${f}.m3u
   done
   ```

2. **Parse m3u → channels**: every `#EXTINF` line is followed by the URL.
   Extract `tvg-name`, `group-title`, `tvg-logo` with regex. One playlist file
   can hold 200-3000 channels.

3. **Filter by category** with a keyword regex on name + group (sport|espn|bein|
   dazn|tennis|golf|nfl|nba|mlb|nhl|ufc|f1|...).

4. **Live-probe in parallel** (ThreadPoolExecutor, 25-30 workers):
   ```bash
   curl -sS -A "Mozilla/5.0" -L --max-time 12 "<url>"
   ```
   Alive = body contains `#EXTM3U` (HLS manifest) or starts with `\x47`
   (MPEG-TS sync byte). A 206/partial-content HEAD check alone gives false
   positives — do a real GET and inspect the body.

5. **Filter out junk**: drop any entry whose name contains `geo-block` or
   `not 24/7`. They'll fail on open.

6. **Dedup by URL**, then write the m3u:
   ```python
   lines = ['#EXTM3U']
   for c in clean:
       lines.append(f'#EXTINF:-1 tvg-name="{c["name"]}" group-title="Sports",{c["name"]}')
       lines.append(c['url'])
   ```

Expected yield: ~3800 channels from 9 regions → ~175 sport-matching → ~104
live after body check → ~89 clean after geo/24-7 filter. Not every "live"
manifest decodes — some are region-locked at the video layer.

## Reliable free sports sources (verified pattern)

Official free feeds that consistently return live manifests: FIFA+ US,
beIN SPORTS XTRA, NBC Sports NOW, ESPN U, Fox Sports 1, NBA TV, MLB Strike
Zone, Tennis Channel, More Than Sports TV, Real Madrid TV, talkSPORT, DD
Sports, Star Sports (India), Cricket Gold.

## Pitfalls

- **Parallel curl belongs in Python threads, not shell `&`** — the terminal
  tool rejects foreground commands with `&` backgrounding.
- Don't trust a HEAD probe; range-request 206s lie. Always GET and sniff the body.
- iptv-org churns: a channel alive today is dead next week. Re-probe before
  telling the user a specific URL works.
- The same channel name often appears 2-6 times (multiple CDN mirrors).
  Dedup by URL, not by name.

## See also

- `system-administration/wifi-connectivity-troubleshooting` — when streaming
  drops are a network-layer problem, not a URL problem.
