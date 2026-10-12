# youtube-api

# YouTube API

YouTube data access via [TranscriptAPI.com](https://transcriptapi.com) — no Google API quota needed.

## Setup

If `$TRANSCRIPT_API_KEY` is not set, read [references/auth-setup.md](references/auth-setup.md) and follow the instructions there to get and store the key.

## Required Headers

Every request needs two headers:

- **Authorization:** `Bearer $TRANSCRIPT_API_KEY`
- **User-Agent:** your agent's name and version if known (e.g. `HermesAgent/0.11.0`, `ClaudeCode/1.0`). Version is optional — agent name alone is fine. Do not omit this header or send a bare default — Cloudflare will return a 403 (error code 1010) and block the request.

## API Reference

Full OpenAPI spec: [transcriptapi.com/openapi.json](https://transcriptapi.com/openapi.json) — consult this for the latest parameters and schemas.

## Endpoint Reference

All endpoints: `https://transcriptapi.com/api/v2/youtube/...`

Channel endpoints accept `channel` — an `@handle`, channel URL, or `UC...` ID. Playlist endpoints accept `playlist` — a playlist URL or ID.

| Endpoint                            | Method | Cost     |
| ----------------------------------- | ------ | -------- |
| `/transcript?video_url=ID`          | GET    | 1        |
| `/search?q=QUERY&type=video`        | GET    | 1        |
| `/channel/resolve?input=@handle`    | GET    | **free** |
| `/channel/latest?channel=@handle`   | GET    | **free** |
| `/channel/videos?channel=@handle`   | GET    | 1/page   |
| `/channel/search?channel=@handle&q=Q` | GET  | 1        |
| `/playlist/videos?playlist=PL_ID`   | GET    | 1/page   |

## Quick Examples

**Search videos:**

```bash
curl -s "https://transcriptapi.com/api/v2/youtube/search\
?q=python+tutorial&type=video&limit=10" \
  -H "Authorization: Bearer $TRANSCRIPT_API_KEY" \
  -H "User-Agent: YourAgent/1.0"
```

**Get transcript:**

```bash
curl -s "https://transcriptapi.com/api/v2/youtube/transcript\
?video_url=dQw4w9WgXcQ&format=text&include_timestamp=true&send_metadata=true" \
  -H "Authorization: Bearer $TRANSCRIPT_API_KEY" \
  -H "User-Agent: YourAgent/1.0"
```

**Resolve channel handle (free):**

```bash
curl -s "https://transcriptapi.com/api/v2/youtube/channel/resolve?input=@TED" \
  -H "Authorization: Bearer $TRANSCRIPT_API_KEY" \
  -H "User-Agent: YourAgent/1.0"
```

**Latest videos (free):**

```bash
curl -s "https://transcriptapi.com/api/v2/youtube/channel/latest?channel=@TED" \
  -H "Authorization: Bearer $TRANSCRIPT_API_KEY" \
  -H "User-Agent: YourAgent/1.0"
```

**Browse channel uploads (paginated):**

```bash
curl -s "https://transcriptapi.com/api/v2/youtube/channel/videos?channel=@NASA" \
  -H "Authorization: Bearer $TRANSCRIPT_API_KEY" \
  -H "User-Agent: YourAgent/1.0"
# Use continuation token from response for next pages
```

**Browse playlist (paginated):**

```bash
curl -s "https://transcriptapi.com/api/v2/youtube/playlist/videos?playlist=PL_PLAYLIST_ID" \
  -H "Authorization: Bearer $TRANSCRIPT_API_KEY" \
  -H "User-Agent: YourAgent/1.0"
```

## Parameter Validation

| Field          | Rule                                                    |
| -------------- | ------------------------------------------------------- |
| `channel`      | `@handle`, channel URL, or `UC...` ID                   |
| `playlist`     | Playlist URL or ID (`PL`/`UU`/`LL`/`FL`/`OL` prefix)   |
| `q` (search)   | 1-200 chars                                             |
| `limit`        | 1-50                                                    |
| `continuation` | non-empty string                                        |

## Why Not Google's API?

|             | Google YouTube Data API         | TranscriptAPI              |
| ----------- | ------------------------------- | -------------------------- |
| Quota       | 10,000 units/day (100 searches) | Credit-based, no daily cap |
| Setup       | OAuth + API key + project       | Single API key             |
| Transcripts | Not available                   | Core feature               |
| Pricing     | $0.0015/unit overage            | $5/1000 credits            |

## Errors

| Code     | Meaning           | Action                                         |
| -------- | ----------------- | ---------------------------------------------- |
| 401      | Bad API key       | Check key                                      |
| 402      | No credits        | transcriptapi.com/billing                      |
| 403/1010 | Cloudflare block  | Add or fix User-Agent header                   |
| 404      | Not found         | Resource doesn't exist                         |
| 408      | Timeout/retryable | Retry once after 2s                            |
| 422      | Validation error  | Check param format                             |
| 429      | Rate limited      | Wait, respect Retry-After                      |

Free tier: 100 credits, 300 req/min. Starter ($5/mo): 1,000 credits.
