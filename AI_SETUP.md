# Ask DMWM — AI knowledge assistant

Ask DMWM is a retrieval-augmented assistant built into the existing Django
site. It answers from:

1. **Fazakkir library books** — PDF, EPUB, DOCX, TXT and Markdown files, split
   into page-, chapter- or document-aware passages.
2. **Fazakkir videos and shorts** — WebVTT transcripts split with start/end
   timestamps.
3. **Approved external web pages** — only domains an administrator has allowed.

The assistant does **not** invent citations. Every substantive claim is
supposed to cite a retrieved passage as `[S1]`, `[S2]`, and the chat UI shows a
source panel with the book page or video timestamp.

> **Important:** the production index is not built automatically by deploying
> the code. Until you run the indexing command (or use the dashboard button),
> the assistant answers from the catalogue and general model knowledge only.
> Do not describe the site as “trained” or “fully indexed” until the coverage
> cards on `/manage/ai/` show indexed books and videos.

---

## Do I need an API key?

Short answer: **you need one key for high-quality chat answers; everything else
works without a key, with reduced quality.**

| Feature | Works without a key? | What the key adds |
| --- | --- | --- |
| Ask DMWM chat | No | A language model is required to compose the answer. **Groq has a free API key.** |
| OpenAI chat | No | Paid. Used automatically if `GROQ_API_KEY` is absent. |
| Embeddings | **Yes** | The local hashing embedding keeps search working offline. `OPENAI_API_KEY` enables true multilingual semantic search. |
| Translation panel | **Yes** | MyMemory is keyless. OpenAI, Google and public LibreTranslate give better quality but need keys/accounts. |
| Library reader | **Yes** | No key needed. |
| External web research | **Yes** | Wikipedia and DuckDuckGo HTML are keyless. |

The **API endpoint itself does not require a client key**. Visitors call
`/ask/send/` on the same site; the server uses its own provider key. You do not
need to expose a key in the browser.

Recommended minimal production setup:

```env
GROQ_API_KEY=gsk_...          # free tier: https://console.groq.com
EMBEDDING_PROVIDER=local      # or "auto" to use OpenAI when available
```

Then build the index. For the best Arabic/French/English search, also set:

```env
OPENAI_API_KEY=sk-...
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=384
```

Changing the embedding provider **requires rebuilding the whole index**, because
vectors from different providers cannot be compared.

---

## Configuration

Add these variables to `.env` (local) or the Northflank environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GROQ_API_KEY` | empty | Free chat provider, preferred when set. |
| `GROQ_ASK_MODEL` | `openai/gpt-oss-20b` | Groq model used for answers. |
| `GEMINI_API_KEY` | empty | Free Google Gemini chat provider (preferred). |
| `GEMINI_ASK_MODEL` | `gemini-3.5-flash` | Gemini model used for answers. |
| `OPENROUTER_API_KEY` | empty | Free OpenRouter chat provider (Llama and others). |
| `OPENROUTER_ASK_MODEL` | `meta-llama/llama-3.3-70b-instruct:free` | OpenRouter model used for answers. |
| `OPENAI_API_KEY` | empty | Fallback chat provider, embeddings and translation. |
| `OPENAI_ASK_MODEL` | `gpt-4o-mini` | OpenAI chat model. |
| `EMBEDDING_PROVIDER` | `auto` | `auto`, `local` or `openai`. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model. |
| `EMBEDDING_DIMENSIONS` | `384` | Vector size written to the index. |
| `KNOWLEDGE_VECTOR_SCAN_LIMIT` | `4000` | Maximum chunks scanned for vector similarity per question. |
| `AI_ASK_HOURLY_LIMIT` | `40` | Questions per account per hour; staff are exempt. `0` disables. |
| `AI_WEB_ALLOWED_DOMAINS` | curated list | Comma-separated external domains allowed by default. |

The answer policy (system prompt, temperature, evidence size, external search
on/off, assistant on/off) is stored in the database and edited at
**`/manage/ai/`**, so it can be changed without a deployment.

---

## Local setup

```bash
# from the repository root
uv sync                       # or: pip install -r requirements.txt
cd fazakkir_dmwm
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open:

- `http://127.0.0.1:8000/ask/` — the chat page.
- `http://127.0.0.1:8000/manage/ai/` — the AI dashboard (staff only).

If the dashboard says no provider is configured, add `GROQ_API_KEY` to
`fazakkir_dmwm/.env` and restart the server.

---

## Building the index

### Whole site

```bash
cd fazakkir_dmwm
python manage.py build_knowledge_index --all
```

Useful flags:

```bash
# Only sources that have no chunks yet
python manage.py build_knowledge_index

# Rebuild everything, replacing old chunks
python manage.py build_knowledge_index --all --force

# One book or one video
python manage.py build_knowledge_index --book-slug the-book-slug --max-pages 200
python manage.py build_knowledge_index --video-id 42

# Limit this run (useful on a small host)
python manage.py build_knowledge_index --limit 5
```

The command is idempotent: reindexing a source deletes its old chunks in a
transaction and writes the new ones.

### From the dashboard

`/manage/ai/` has an **Index next batch** button that indexes up to 5 books,
10 videos and 10 shorts per click. Run it repeatedly until coverage reaches
100%, or use the management command in a console for a single long run.

### What gets indexed

- **Books:** the stored file is downloaded from R2, its text is extracted, and
  each passage keeps the page number (PDF), chapter number (EPUB) or document
  marker (DOCX/TXT/MD). The combined text and page map are cached on the book
  row (`content_text`, `content_pages`).
- **Videos:** the preferred WebVTT track is parsed into cues; chunks keep
  `timestamp_start` / `timestamp_end`.
- **Shorts:** title and description (metadata only).
- **Fallback:** a book with no extractable text (for example a scanned PDF)
  still contributes its title and description as a low-authority passage.

Scanned PDFs need OCR before upload; the current pipeline does not OCR images.

---

## Source review and authority

Every book/video gets a `SourceProfile` in the dashboard:

| Authority | Meaning | Ranking weight |
| --- | --- | --- |
| Primary | Qur'an, authentic Sunnah, orator’s own recorded words | 1.00 |
| Secondary | Recognised scholarly explanation | 0.72 |
| Reference | General or historical material | 0.45 |
| Unverified | Needs editorial review; ranked last | 0.15 |

Unapproved sources are excluded from retrieval. Administrators can assign a
school/perspective and editorial notes. External domains are managed separately
with `WebSourceRule`; an explicit rule overrides the environment allowlist.

The default policy prompt (`knowledge/prompts.py`) tells the model:

- to prefer Qur'an and authentic Sunnah, then scholarship, then the library,
  then approved external sources;
- to cite with `[S1]`-style markers and never invent verses, hadith, pages,
  speakers or URLs;
- to distinguish internal library material from external web material;
- not to claim to be a mufti/imam/scholar, not to issue definitive personal
  fatwas on divorce, inheritance, finance or medical matters, and never to
  issue takfir.

---

## How retrieval works

1. The question is normalised (Arabic diacritics, alef/ya/ta-marbuta variants,
   light stemming and stopwords) and tokenised.
2. Keyword prefiltering selects candidate chunks with `search_text__icontains`.
   A recent-chunk fallback keeps recall acceptable on a young index.
3. Each candidate receives:
   - keyword overlap score (40%),
   - cosine similarity with the stored embedding (42%),
   - authority weight (9%),
   - title match (6%),
   - language match (3%).
4. The top 40 are reranked with exact-phrase and unverified-source penalties.
5. At most three passages from one book/video are kept, for source variety.
6. External research runs only when the question is time-sensitive, internal
   evidence is thin, or the best internal score is low. External results are
   filtered through the domain allowlist before reaching the model.

No separate vector database is required: vectors are JSON columns next to the
rows, which keeps SQLite development and Postgres production identical. For a
very large corpus, raise `KNOWLEDGE_VECTOR_SCAN_LIMIT` or replace
`knowledge/retrieval.py` with a dedicated vector store.

---

## Chat UI

`/ask/` provides:

- conversation history (last 6 messages sent to the model);
- language selection for the answer;
- retrieval-grounded answers with clickable `[S1]` citations;
- a **Sources and evidence** panel showing title, author, page/chapter or
  timestamp, authority, origin (Library/External) and a snippet;
- copy, translation and follow-up controls on each answer;
- helpful/not-helpful feedback stored in `AnswerFeedback`;
- a clear warning that the assistant is not a substitute for a scholar.

Answer translations use the existing `/library/translate/` endpoint, so the
same provider chain applies as the library translation panel.

---

## Administration

`/manage/ai/` (staff) and `/admin/knowledge/` (Django admin) expose:

- provider and embedding status;
- index coverage and a batch indexing button;
- per-source review: authority, perspective, notes, approve/block, reindex;
- external domain allow/block rules;
- recent feedback with a “mark reviewed” action;
- unanswered questions and errors, with resolution notes;
- the editable system prompt and generation settings;
- recent answer history.

The Django admin adds raw access to chunks, interactions and feedback for
debugging.

---

## Tests

```bash
cd fazakkir_dmwm
python manage.py test knowledge
```

The suite covers Arabic/Latin normalisation, local embeddings, chunking, WebVTT
timestamps, PDF/EPUB/DOCX/TXT extraction, page-aware indexing, retrieval
filtering, authority exclusion, external allowlists, answer logging, feedback,
and dashboard access. Network calls are mocked; the tests do not need an API
key.

---

## Deployment (Northflank)

1. Commit and push to `master`. Northflank deploys automatically and runs
   `manage.py migrate` in the start command, which creates the `knowledge`
   tables.
2. Add `GROQ_API_KEY` (and optionally `OPENAI_API_KEY` /
   `EMBEDDING_PROVIDER`) to the Northflank environment.
3. Open the Northflank console and build the index:

   ```bash
   python manage.py build_knowledge_index --all
   ```

   Or open `/manage/ai/` and click **Index next batch** repeatedly.
4. Check `/manage/ai/` until the books/videos coverage cards reflect the real
   collection, then ask a few questions and verify:
   - citations point to real pages/timestamps;
   - external sources are labelled **External**;
   - feedback is saved;
   - `/admin/knowledge/askinteraction/` logs the questions.

Only after step 4 should the site claim that Ask DMWM is indexed and grounded
in the library.

### Rebuilding after a provider change

Changing `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL` or `EMBEDDING_DIMENSIONS`
invalidates the stored vectors. Run:

```bash
python manage.py build_knowledge_index --all --force
```

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| “The AI assistant is not configured.” | Set `GROQ_API_KEY` or `OPENAI_API_KEY`, then restart the app. |
| Answers say no matching sources were found. | The index is empty. Run the indexing command and check `/manage/ai/`. |
| Book coverage stays at 0. | The file may be scanned/encrypted, missing from R2, or the source is unapproved. Open the book in the dashboard and click **Reindex**; check the server log. |
| Citations show no page number. | The file is EPUB/DOCX/TXT (chapter/document labels), or the PDF produced no text on that page. |
| Search feels keyword-only. | Local hashing embeddings are active. Set `OPENAI_API_KEY` and rebuild with `--force`. |
| External results never appear. | The domain is not on the allowlist, external search is off in `/manage/ai/`, or the question did not trigger research. |
| Translation unavailable. | MyMemory may be rate-limited. Add `OPENAI_API_KEY` or configure Google/LibreTranslate. |

---

## Files

| Path | Purpose |
| --- | --- |
| `knowledge/models.py` | Chunks, source reviews, web rules, settings, interactions, feedback. |
| `knowledge/embeddings.py` | Local hashing + OpenAI embeddings, Arabic normalisation. |
| `knowledge/indexing.py` | Chunking, WebVTT parsing, book/video/short indexing. |
| `knowledge/retrieval.py` | Hybrid search, authority filters, context building. |
| `knowledge/web.py` | Keyless Wikipedia/DuckDuckGo research and domain rules. |
| `knowledge/answer.py` | Provider selection, prompt assembly, logging. |
| `knowledge/views.py` | Feedback endpoint and `/manage/ai/` dashboard. |
| `knowledge/prompts.py` | Safety-reviewed default prompt. |
| `library/content.py` | PDF/EPUB/DOCX/TXT/Markdown extraction. |
| `videos/templates/videos/ask.html` | Chat UI, citations, sources, translation, feedback. |
