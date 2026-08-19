# MoodNews

Real news articles, retold in the emotional tone you choose — with every fact
kept intact and **verified**.

MoodNews fetches real articles from public RSS feeds, extracts the facts that
must not change (numbers, dates, quotes, names, places), rewrites the article
in a chosen mood with an LLM, and then checks the rewrite against those facts
in two independent layers before showing it to you. The reader sees the
original and the rewrite side by side, with a fact-check badge that reflects
what the checks actually found — including when they failed.

```
RSS feeds → SQLite → anchor-fact extraction → LLM rewrite → two-layer fact check → UI
```

---

## Table of contents

- [Running the project](#running-the-project)
- [Where the news comes from](#where-the-news-comes-from)
- [How the data is stored](#how-the-data-is-stored)
- [How mood rewriting works](#how-mood-rewriting-works)
- [How fact preservation is verified](#how-fact-preservation-is-verified)
- [API reference](#api-reference)
- [Command-line tools](#command-line-tools)
- [Project layout](#project-layout)
- [Design decisions and trade-offs](#design-decisions-and-trade-offs)
- [AI tools used](#ai-tools-used)

---

## Running the project

### With Docker (recommended)

Requires Docker with Compose v2.

```bash
git clone <this-repo> && cd moodnews

# 1. Configure. The app boots without a key, but mood rewriting needs one.
cp .env.example .env
$EDITOR .env          # set LLM_API_KEY (see "Configuration" below)

# 2. Start both services.
docker compose up --build
```

Then open:

| | |
|---|---|
| Frontend | http://localhost:5173 |
| API | http://localhost:8000 |
| Interactive API docs | http://localhost:8000/docs |
| Health check | http://localhost:8000/health |

On first boot the backend creates the SQLite schema, fetches articles from the
RSS feeds, and extracts anchor facts from each one. This takes a few seconds;
the API serves immediately and the grid fills in. Refresh if you get there
first.

The SQLite file lives on a named Docker volume (`moodnews-data`, mounted at
`/data`), so **articles and cached rewrites survive `docker compose down` and
container rebuilds**. To start completely fresh:

```bash
docker compose down -v      # -v also removes the volume
```

### Without Docker (local development)

Requires Python 3.11+ and Node 20+.

**Backend**

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp ../.env.example .env     # or export the variables yourself
uvicorn app.main:app --reload --port 8000
```

**Frontend** (in a second terminal)

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api/*` to the backend, so the browser only ever
talks to one origin. The proxy target is `VITE_API_PROXY_TARGET`
(`http://localhost:8000` locally, `http://backend:8000` under Compose).

### Configuration

Everything is environment-driven; see [`.env.example`](.env.example) for the
full annotated list. The ones that matter:

| Variable | Default | What it does |
|---|---|---|
| `LLM_API_KEY` | *(empty)* | **Required for rewriting.** Without it the app still runs and serves original articles; the rewrite panel explains that rewriting is unavailable. |
| `LLM_BASE_URL` | `https://api.z.ai/api/paas/v4` | Any OpenAI-compatible `/chat/completions` endpoint. |
| `LLM_MODEL` | `glm-4.6` | Model name as the endpoint expects it. |
| `LLM_REWRITE_TEMPERATURE` | `0.7` | Creative latitude for rewriting. |
| `LLM_VERIFY_TEMPERATURE` | `0` | Fact-checking must be deterministic. Leave at 0. |
| `DB_PATH` | `moodnews.db` | SQLite file. Compose overrides this to `/data/moodnews.db`. |
| `FETCH_ON_STARTUP` | `true` | Fetch feeds on boot when the DB holds fewer than `MIN_ARTICLES`. |
| `MIN_ARTICLES` / `MAX_ARTICLES_PER_FEED` | `10` / `5` | Ingestion targets. |
| `CORS_ORIGINS` | `http://localhost:5173,…` | Comma-separated allowed origins. |

Because the client only assumes the OpenAI wire format, switching provider is a
`.env` change, not a code change:

```bash
# OpenAI instead of GLM
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

---

## Where the news comes from

Articles come from four **public RSS feeds**, no API keys or registration
required (`backend/app/services/rss_feeds.py`):

| Source | Feed | Why it is in the list |
|---|---|---|
| BBC News | `feeds.bbci.co.uk/news/world/rss.xml` | Major agency, UK editorial tradition |
| NPR | `feeds.npr.org/1001/rss.xml` | Major agency, US editorial tradition |
| Al Jazeera | `aljazeera.com/xml/rss/all.xml` | Major agency, Qatari editorial tradition |
| Ars Technica | `feeds.arstechnica.com/arstechnica/index` | Technology, so the moods have non-political material to work with |

Three general agencies from different editorial traditions mean the grid is not
one newsroom's worldview, and the technology feed gives the moods something
other than politics and disaster to work on — "joyful" is a more interesting
test on a product launch than on a ferry sinking. Several independent feeds
also remove the single point of failure: each feed is fetched independently and
a failure is recorded rather than raised, so one dead host cannot empty the
grid.

### How fetching works

`backend/app/services/news_fetcher.py`, per feed:

1. **Download and parse** the feed with `feedparser`.
2. **Clean** the entry's HTML snippet down to text (BeautifulSoup + whitespace
   collapse).
3. **Fetch the article page when the snippet is too thin.** Below 500
   characters there is not enough text to rewrite or to mine facts from, so the
   fetcher requests the article page and extracts paragraphs from
   `<article>`/`<main>`, dropping one-line furniture (bylines, cookie notices).
   This is best-effort: any failure falls back to the RSS snippet rather than
   dropping the article.
4. **Store**, skipping anything still under 180 characters and truncating at
   6,000 characters so prompt sizes and costs stay predictable.

De-duplication is enforced by the database: `news.source_url` is `UNIQUE` and
inserts use `INSERT OR IGNORE`, so re-running the fetcher is always safe and
never produces duplicates. A typical run stores ~20 articles across the four
feeds.

Fetching runs automatically on startup when the database holds fewer than
`MIN_ARTICLES` articles, in a worker thread so it never blocks the API, and can
be triggered by hand at any time:

```bash
docker compose exec backend python -m app.cli fetch
```

---

## How the data is stored

**SQLite**, via the standard library `sqlite3` driver — no ORM. The data model
is two flat tables with simple queries, and keeping the SQL explicit makes the
schema readable in review. WAL journaling is enabled so reads are not blocked
by background fetch and rewrite writes.

SQLite is the right choice here specifically because this is a single-node
application with a read-heavy workload, a handful of writers, and a strong wish
to be runnable with one `docker compose up`: no separate database service, no
credentials, no migration tooling, and the whole dataset is one file you can
inspect with `sqlite3` or copy for a colleague. The file lives on a mounted
volume so it survives container restarts. The cost is that it would not survive
horizontal scaling — see [trade-offs](#design-decisions-and-trade-offs).

### Schema

`backend/app/core/schema.py` (idempotent `CREATE TABLE IF NOT EXISTS`, run at
every startup):

```sql
CREATE TABLE news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    original_text TEXT NOT NULL,
    summary       TEXT,               -- card preview, built at fetch time
    source_name   TEXT NOT NULL,
    source_url    TEXT NOT NULL UNIQUE,   -- natural de-duplication key
    author        TEXT,
    published_at  TEXT,               -- ISO-8601 UTC
    fetched_at    TEXT NOT NULL,      -- ISO-8601 UTC
    facts_json    TEXT                -- extracted anchor facts (ground truth)
);

CREATE TABLE rewrites (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id              INTEGER NOT NULL REFERENCES news (id) ON DELETE CASCADE,
    mood                 TEXT NOT NULL,
    rewritten_text       TEXT NOT NULL,
    facts_preserved_json TEXT,        -- the model's own claim, never trusted
    fact_check_status    TEXT NOT NULL DEFAULT 'unknown',  -- passed|warning|failed
    fact_check_notes     TEXT,        -- full FactCheckReport as JSON
    model                TEXT,
    attempts             INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    UNIQUE (news_id, mood)            -- the cache key
);
```

Notes on the design:

- **`facts_json`** holds the anchor facts extracted from `original_text`. It is
  the ground truth every rewrite of that article is checked against.
- **`UNIQUE (news_id, mood)`** makes the rewrite cache structural rather than a
  matter of calling code remembering to check: an article is only ever
  rewritten once per mood, and a repeated request is served from the database
  without touching the LLM. Regeneration upserts in place.
- **Timestamps are ISO-8601 UTC strings.** SQLite has no native date type, and
  ISO-8601 sorts lexicographically, so `ORDER BY` still works correctly.

---

## How mood rewriting works

### The moods

Six, defined in one registry (`backend/app/services/moods.py`) that the API
serves at `GET /moods` and the frontend renders — adding a mood is one entry in
that file and nothing else:

| Mood | Voice |
|---|---|
| `neutral` | Flat newswire register — the baseline |
| `joyful` | Warm, optimistic, energetic |
| `sad` | Sombre, elegiac, attentive to loss |
| `ironic` | Dry, understated, wry |
| `dramatic` | Cinematic urgency, tension, stakes |
| `formal` | Official communiqué, precise and impersonal |

Each mood entry describes **voice only**. The factual constraints live in the
system prompt and are byte-identical for every mood, so a mood can never be the
reason a fact moved. Two moods needed explicit guard rails because the natural
way to write them is to exaggerate: `ironic` is told the irony must come from
arranging real facts against each other and to drop to near zero on stories
involving death, and `dramatic` is told to heighten the telling, not the
events.

### Prompt design

The work is split across two messages on purpose
(`backend/app/services/rewriter.py`):

**System prompt — the factual contract.** Constant for every mood and article,
so no mood instruction can dilute it. It states six numbered prohibitions:
numbers and dates must appear exactly as written; direct quotations word for
word; names spelled identically and keeping their roles; no added facts; no
removed facts; no speculation. It then names what *may* change — sentence
structure, rhythm, word choice, ordering, emotional colouring — and caps length
at ±20% of the original.

**User prompt — the variable parts.** The mood's voice instruction, the
article, and, crucially, **the extracted anchor facts listed explicitly and
grouped by type**:

```
ANCHOR FACTS - every item below must appear in your rewrite, unchanged:
NUMBERS - must appear character-for-character:
  - "93"
  - "168"
DATES AND TIMES - must appear character-for-character:
  - "Tuesday"
DIRECT QUOTES - must appear word-for-word inside quotation marks:
  - "Children were screaming and most of the passengers were vomiting…"
NAMES - spelling and role must not change:
  - "President Emmerson Mnangagwa"
```

Telling the model *which strings* must survive is far more reliable than asking
it to "preserve all facts" and hoping its notion of a fact matches ours. It is
also the same list verification runs against, so the model is asked to satisfy
exactly the test it will be marked against.

### Structured output

The reply is requested as a JSON object (via `response_format: json_object`
where the endpoint supports it, with a text-parsing fallback where it does
not):

```json
{
  "rewritten_text": "…the rewritten article…",
  "facts_preserved": ["93", "Tuesday", "President Emmerson Mnangagwa"]
}
```

`rewritten_text` is the product. `facts_preserved` is the model's **own claim**
about what it kept: it is stored and shown in the API for transparency, but it
is never treated as evidence. A model that has dropped a number is exactly the
model that will also claim it did not — so verification always re-derives the
answer from the text itself.

### Caching

Every `(news_id, mood)` pair is generated once and stored in `rewrites`.
Repeated requests — including a reader switching back to a mood they already
viewed — are served from SQLite without calling the LLM. A per-`(article,
mood)` in-process lock means concurrent requests for the same uncached rewrite
result in **one** generation rather than several, since the cache only helps
once the first call has finished.

---

## How fact preservation is verified

**This is the mechanism that guarantees names, dates, numbers and quotes are
not altered by rewriting.** It runs on every rewrite before it is stored, in
two independent layers (`backend/app/services/fact_checker.py`).

### Step 0 — establishing the ground truth

Before any rewriting happens, `backend/app/services/fact_extractor.py` mines
the original article for **anchor facts** with prioritised regex passes:

| Type | Examples caught | Verbatim-required? |
|---|---|---|
| `number` | `93`, `1,200`, `18 percent`, `$13.99`, `2.5 million` | **yes** |
| `date` | `18 August 2026`, `August 18, 2026`, `2026`, `Tuesday`, `14:30 GMT` | **yes** |
| `quote` | anything inside quotation marks, three words or longer | **yes** |
| `name` | `President Emmerson Mnangagwa`, `Bank of England`, `NATO` | no — see below |
| `place` | `Zimbabwe`, `South Korea`, `Granada` | no — see below |

Passes claim spans in priority order, so `2026` inside `18 August 2026` is
recorded once, as a date, and not again as a bare number. Entities are matched
as whole capitalised runs and *then* classified, which is what keeps
`Bank of England` intact (a gazetteer-first scan would claim "England" and lose
the organisation) and distinguishes `Manchester City` from `Manchester`.

The result is stored in `news.facts_json` and is used twice: pasted into the
rewrite prompt as "these must not change", and used as the checklist below.

> **Why regex, and where NER would be better.** Pattern matching is strongest
> exactly where it matters most — numbers, dates and quoted speech are highly
> regular, and are the facts a tone rewrite is most likely to corrupt. It is
> weakest at entity recognition, where it over-captures (any capitalised phrase
> looks like a name). The code marks the upgrade point: `_extract_entities` is
> a drop-in swap for spaCy `en_core_web_sm` (PERSON/ORG/GPE) or a
> HuggingFace NER model, without changing the `Fact`/`FactSet` contract.
> Over-capture is the deliberate failure mode: a spurious fact costs one line
> of prompt, a missed number is a fact the pipeline can no longer protect.

### Layer 1 — programmatic check (the guarantee)

Every fact flagged `verbatim_required` — **numbers, dates and quotes** — must
be findable in the rewritten text. This is a string search, so it cannot be
talked out of its answer, and its verdict is binding: **one missing number is a
failed check no matter what any model says about it.**

Matching allows only differences that cannot change meaning:

| Type | Tolerance | Passes | Fails |
|---|---|---|---|
| number | exact, else digit core ignoring separators | `1,200` ↔ `1200` | `1,200` vs `1,300` |
| date | exact, else all components present in any order | `18 August 2026` ↔ `August 18, 2026` | `19 August 2026`, `August 2026` |
| quote | exact, else ignoring punctuation and case | `“…”` ↔ `"…"`, sentence-final punctuation | any changed word |

Names and places are deliberately **not** in this layer. A rewrite may
legitimately say "the president" on second mention, and failing it for that
would make the badge cry wolf. They are the auditor's remit instead.

### Layer 2 — LLM auditor pass

A **second, separate LLM call** with:

- **`temperature = 0`** — verification must be deterministic and reproducible.
- **A distinct system prompt** casting the model as an adversarial
  fact-checking auditor, not a writer.
- **Different inputs**: the original article, the anchor facts as JSON, and the
  rewritten text.

The auditor prompt explicitly *forbids* flagging tone, restructuring, length,
or a person referred to by role after being named — otherwise it flags the
entire point of the app — and directs it at what regex cannot see: a dropped
name, a reversed causal claim, a quote re-attributed to the wrong speaker, an
invented consequence. Added facts are to be reported as contradictions. It
returns:

```json
{
  "all_facts_present": true,
  "missing_facts": [],
  "contradictions": []
}
```

The auditor never raises: an auditor that cannot run (no key, upstream error)
downgrades the result to a warning, because "we could not check" is not the
same as "we checked and it is fine".

### Combining the layers

The two are combined, never traded off:

| Result | Condition |
|---|---|
| **`passed`** | Layer 1 clean **and** auditor reports no missing facts and no contradictions |
| **`warning`** | Layer 1 clean, but the auditor flagged a missing detail, or could not run |
| **`failed`** | Layer 1 found a missing number/date/quote, **or** the auditor found a contradiction |

Layer 1 can only fail a rewrite; layer 2 can only downgrade one further. No
model verdict can overturn a failed string match.

### Retry, and never hiding a failure

If the combined result is `failed`, the pipeline retries **once**, with a
stricter prompt that names the exact strings that went missing and lowers the
temperature — a targeted correction rather than another roll of the dice. The
better of the two attempts is kept (ranked by status, then by number of facts
verified), because a retry that comes back worse is not an improvement just
because it came second.

If the second attempt still fails, **the rewrite is kept, stored, flagged and
shown**, logged at WARNING level. It is never silently discarded, never
replaced by the original, and never displayed as if it had passed: the UI
renders a red "Fact check failed 2/8" badge that expands to list every missing
fact, directly above the rewritten text, with the original alongside it for
comparison. Hiding a failed check would defeat the purpose of running one.

The full report — both layers, counts, missing facts, auditor state, attempt
count — is stored as JSON in `rewrites.fact_check_notes` and surfaced through
the API, so the badge's "verified 14/14" is a count of string matches actually
performed, not decoration.

Re-verify any cached rewrite without regenerating it:

```bash
docker compose exec backend python -m app.cli recheck 5 ironic
```

---

## API reference

Interactive documentation at http://localhost:8000/docs.

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness, database reachability, article count, whether an LLM key is configured |
| `GET /moods` | The mood options, in display order |
| `GET /news?limit=&offset=` | The grid: newest first, with preview and fact count per card |
| `GET /news/{id}` | One article, original text only (no LLM call) |
| `GET /news/{id}?mood=ironic` | Original **and** rewrite, from cache or generated on demand |

```bash
curl 'http://localhost:8000/news?limit=3'
curl 'http://localhost:8000/news/5?mood=ironic'
```

`GET /news/{id}?mood=…` returns the comparison payload:

```jsonc
{
  "id": 5,
  "title": "Kariba ferry death toll rises to 93…",
  "source_url": "https://www.bbc.co.uk/news/articles/…",
  "original_text": "The death toll from a ferry disaster…",
  "facts": { "total": 31, "verbatim_total": 14, "counts": { "number": 9, … } },
  "mood": "ironic",
  "rewrite": {
    "text": "…",
    "fact_check": {
      "status": "passed",        // passed | warning | failed | unchecked
      "verified": 14, "total": 14,
      "missing_facts": [], "contradictions": [],
      "auditor": "ok",            // ok | skipped | error
      "summary": "All anchor facts verified by both checks.",
      "attempts": 1
    },
    "model": "glm-4.6",
    "from_cache": true,
    "facts_preserved": ["93", "Tuesday"]   // the model's claim, not evidence
  },
  "rewrite_error": null,
  "cached_moods": ["ironic"]
}
```

Error behaviour is deliberate:

- **404** unknown article, **400** unknown mood, **422** invalid parameters.
- A rewrite that could not be **produced** (no API key, upstream failure)
  returns **200** with `rewrite: null` and a populated `rewrite_error`. The
  original is still readable, so the comparison view degrades to one side with
  an explanation rather than an error page.
- A rewrite that was produced but **failed its fact check** returns normally
  with `fact_check.status: "failed"`. That is a result, not an error.

---

## Command-line tools

```bash
# Under Docker, prefix with:  docker compose exec backend
python -m app.cli init-db            # create the schema
python -m app.cli fetch              # fetch all feeds now, then extract facts
python -m app.cli extract-facts      # backfill anchor facts
python -m app.cli show-facts 5       # inspect one article's ground truth
python -m app.cli moods              # list available moods
python -m app.cli rewrite 5 ironic   # rewrite one article (calls the LLM)
python -m app.cli recheck 5 ironic   # re-run both fact-check layers
python -m app.cli stats              # what is stored, with fact-check verdicts
```

`show-facts` is the quickest way to see what the pipeline is protecting:

```
NUMBER (9)!
    93 x3
    168
DATE (1)!
    Tuesday
QUOTE (4)!
    Children were screaming and most of the passengers were vomiting…

14 fact(s) must appear verbatim in a rewrite.
```

---

## Project layout

```
backend/
  app/
    main.py                  FastAPI app, startup ingestion, health
    cli.py                   operational CLI
    models.py                domain models (Article, Fact, Rewrite, reports)
    core/
      config.py              env-driven settings
      database.py            SQLite connections (WAL, transactions)
      schema.py              DDL and initialisation
    repositories/
      news_repository.py     articles
      rewrite_repository.py  rewrites, and the (news_id, mood) cache
    services/
      rss_feeds.py           the feed registry
      news_fetcher.py        fetch, clean, scrape, store
      fact_extractor.py      anchor-fact extraction  ← ground truth
      moods.py               the mood registry
      llm_client.py          OpenAI-compatible client, JSON handling
      rewriter.py            prompt assembly, generation, retry, caching
      fact_checker.py        two-layer verification  ← the guarantee
    api/
      routes.py              endpoints
      schemas.py             response models
frontend/
  src/
    App.jsx                  state, routing between grid and article
    api/client.js            API access
    components/
      MoodSwitcher.jsx       global mood selection
      NewsGrid.jsx           grid, loading, error and empty states
      NewsCard.jsx           one card
      ArticleView.jsx        side-by-side comparison
      FactCheckBadge.jsx     the fact-check verdict
docker-compose.yml           backend + frontend + persistent volume
.env.example                 every variable, annotated
```

---

## Design decisions and trade-offs

**Anchor facts are extracted before rewriting, not after.** Deriving the
checklist from the original alone means the rewrite cannot influence what it
will be judged on.

**The programmatic layer is the guarantee; the LLM layer is the safety net.**
Asking one model to check another's work is useful but not a guarantee — it can
be wrong in the same direction twice. String matching for numbers, dates and
quotes cannot be. Splitting the two lets each do what it is good at, and means
the strongest claim the app makes ("numbers and dates are never altered") rests
on the layer that cannot be argued with.

**A failed check is shown, not hidden.** The alternative — dropping failed
rewrites and silently regenerating until something passes — would make the
badge meaningless, since the reader could no longer tell a verified rewrite
from a lucky one.

**SQLite and no ORM.** Right for a single-node app with two tables and a
one-command setup; wrong the moment you need multiple backend replicas writing
concurrently. Postgres would be the swap, and the repository layer is where it
would happen — nothing above it writes SQL.

**Rewrites are cached forever.** There is no TTL and no invalidation: the
source article is immutable once fetched, so a rewrite of it does not go stale.
Regeneration is available explicitly (`rewrite --force`).

**Sync handlers, not async.** `sqlite3` and the OpenAI SDK are both blocking.
FastAPI runs sync handlers in a worker thread, so a twenty-second rewrite does
not stall the event loop — simpler and more honest than wrapping blocking calls
in async syntax.

### Known limitations

- **Entity extraction over-captures.** Scraped page furniture ("Getty Images",
  navigation labels) can be recorded as names. Harmless — they are not
  verbatim-required — but a real NER model would clean it up.
- **Article scraping is heuristic.** Some sources (video pages especially) yield
  only their RSS snippet, so a few articles are short and carry few anchor
  facts.
- **The auditor costs a second LLM call per rewrite.** Deliberate: verification
  that shares a call with generation is not independent verification.
- **No pagination in the UI.** The API supports `limit`/`offset`; the grid
  requests the first 30 articles.
- **No automated test suite.** Behaviour was verified by exercising the real
  pipeline: layer-1 matching against crafted positive and negative cases, the
  orchestration across all its outcome paths with a stubbed model, and the API
  and UI against the real database. Pytest coverage of `fact_extractor` and
  `fact_checker` would be the first addition.

---

## AI tools used

**AI used to build this project.** [Claude Code](https://claude.com/claude-code)
(Anthropic) was the **only** AI tool used to build MoodNews. It was used for
the whole implementation — backend, frontend, Docker setup and this
documentation — worked through in reviewed sections, one commit at a time. No
other AI assistant, code generator or autocomplete tool was involved.

**AI used inside the app.** Separately, and by design, the product's core
feature calls an LLM at runtime: **GLM (`glm-4.6`) via
[z.ai](https://z.ai)**, reached through its OpenAI-compatible
chat-completions API. It is called twice per uncached rewrite:

1. once to rewrite the article in the chosen mood (temperature 0.7), and
2. once, separately, to audit that rewrite for factual faithfulness
   (temperature 0, different system prompt).

The distinction matters: the first is a tool that helped write the code, the
second is a dependency the running application has. The provider is not
hard-coded — `LLM_BASE_URL` and `LLM_MODEL` point the same client at OpenAI or
any other compatible endpoint.
