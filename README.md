# Front Link Sync

Monitors Front conversations tagged **Linked**, checks each referenced (linked) conversation for new activity, asks Claude whether the updates are significant, and posts a concise summary back to the parent conversation as an internal comment.

This is a Python/FastAPI port of the Google Apps Script `Front Link Sync`, running on Railway alongside [`party-finder`](https://github.com/chiphanna-foco/party-finder) and following the same operational patterns.

## Architecture

```
              ┌──────────────────────────┐
              │   Front  (Linked tag)    │
              └────────────┬─────────────┘
                           │  GET /tags/{id}/conversations
                           │  GET /conversations/{id}/comments
                           │  GET /conversations/{id}/messages
                           ▼
    ┌──────────────────────────────────────────┐
    │  FastAPI service (this repo)             │
    │  - scheduler: every CHECK_INTERVAL_HOURS │
    │  - quiet window: QUIET_HOURS_START..END  │
    │  - SQLite cache of last_checked_at       │
    └──────────┬───────────────────┬───────────┘
               │                   │
               ▼                   ▼
       ┌──────────────┐     ┌──────────────┐
       │ Anthropic    │     │  Front       │
       │ Haiku 4.5    │     │  POST comment│
       └──────────────┘     └──────────────┘
```

## Setup

### Local

```bash
cp .env.example .env
# Fill in FRONT_API_KEY, LINKED_TAG_ID, ANTHROPIC_API_KEY
pip install -r requirements.txt
python run.py
```

### Docker

```bash
docker compose up --build
```

### Railway

1. Add a persistent volume mounted at `/data`.
2. Set every env var from `.env.example` in the Railway dashboard.
3. Deploy from the `main` branch.

## Environment Variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `FRONT_API_KEY` | ✅ | — | Front API bearer token |
| `FRONT_API_URL` | | `https://api2.frontapp.com` | Front API base URL |
| `LINKED_TAG_ID` | ✅ | — | Front tag ID for "Linked" (e.g. `tag_27f7wp`) |
| `ANTHROPIC_API_KEY` | ✅ | — | Anthropic API key |
| `ANTHROPIC_MODEL` | | `claude-haiku-4-5` | Model used for summarization |
| `CHECK_INTERVAL_HOURS` | | `6` | How often the scheduler fires |
| `RUN_ON_STARTUP` | | `false` | Run a check immediately when the server starts |
| `QUIET_HOURS_START` | | `23` | Quiet window start hour (0-23) |
| `QUIET_HOURS_END` | | `6` | Quiet window end hour (0-23, exclusive) |
| `TIMEZONE` | | `America/Denver` | IANA timezone for the quiet window |
| `DATABASE_PATH` | | `/data/links.db` or `./links.db` | SQLite location |
| `HOST` | | `0.0.0.0` | Bind address |
| `PORT` | | `8000` | Bind port |

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/status` | Health + last sync info + quiet-window state |
| `POST` | `/api/check` | Trigger a full sweep in the background (ignores quiet window) |
| `POST` | `/api/check/{conversation_id}` | Force-check a single parent conversation (replaces Apps Script `testConversation`) |

## Behavior

On every run:

1. Fetch every conversation tagged `LINKED_TAG_ID`.
2. For each, scan comments for `https://app.frontapp.com/open/cnv_…` links.
3. For each referenced conversation, collect messages + comments created since `max(last_checked_at, link_added_at)`.
4. Send the activity to Claude (Haiku 4.5 via structured outputs). It returns `{shouldPost, reasoning, message}`.
5. If `shouldPost`, post an internal comment prefixed with `🔗 **Linked Conversation Update**` on the parent conversation, and advance the cache to the latest reported timestamp.

The scheduler skips runs whose current local hour falls inside `[QUIET_HOURS_START, QUIET_HOURS_END)`. Manual endpoint calls ignore the quiet window.
