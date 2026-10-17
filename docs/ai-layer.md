# AI layer

How SmartHealth's Part B features find services and answer patients, and why
they are built this way. Sections are added as Weeks 4 and 5 land.

## 1. Embeddings and vector similarity

An embedding model turns a piece of text into a vector: a fixed-length list of
numbers, 384 of them for `all-MiniLM-L6-v2`, the model used here. The model is
trained on large amounts of text so that passages with similar meaning get
similar vectors. With it, "I need a scan of my knee" scores 0.614 against the
Knee X-Ray chunk and no more than 0.221 against the two other services seeded
on Day 1. Week
1's search matches the query against service names with `ILIKE` and finds
nothing, because no service name contains that phrase.

Closeness is measured with cosine similarity, the cosine of the angle between
two vectors. Vectors pointing the same way score 1; unrelated ones score near 0.
It compares direction and ignores length, so a short question can still score
high against a long description. pgvector's `<=>` operator returns cosine
distance, which is `1 - similarity`.

SmartHealth uses this at two moments. When a service is published, the publish
workflow embeds its chunk once and stores the vector with it. When a patient
searches, the question is embedded with the same model, and Postgres returns the
nearest service vectors, with `WHERE` clauses that keep only published services.
Vectors from different models are not comparable, so changing the model means
re-embedding every service.

Two limits shape the rest of the design. Nearest-neighbour search always returns
something, even for gibberish, so results below a minimum similarity are dropped
and an empty result means "we don't offer that". The threshold, 0.31, was chosen
by measuring hit rate on the evaluation set (section 7).

The model also only measures how alike two texts are. A question landing near
the Echocardiogram service means its wording resembles that service's
description, and says nothing about the patient's health. That is why the
assistant routes patients to services and never diagnoses.

## 2. Chunking

Each published service becomes exactly one chunk, built by
`build_service_chunk_text` in `app/ai/chunking.py`:

```
{department} · {specialties}: {name}. {description} Preparation: {prep instructions}
```

The seeded Knee X-Ray service produces:

```
Orthopaedics · Orthopedics: Knee X-Ray. Standard imaging of the knee joint. Preparation: Wear loose clothing; remove any metal jewellery near the knee.
```

That is 151 characters, a few sentences. Splitting has nothing to do at that
size, so the code keeps one chunk per service and `.env.example` has no
chunk-size or overlap settings. A splitter tuned small enough to cut a
service would separate its name from its preparation text, and a question about
preparation would then match a chunk that never names the service.

The department and specialty add the names of areas of care, such as
"Cardiology" or "Orthopaedics", which a patient may type even when the service
name ("Echocardiogram") contains neither. The seeded orthopaedics data carries
both spellings, "Orthopaedics" from the department and "Orthopedics" from the
specialty, so either one matches.

### Where specialties come from

Specialty is stored on providers, so a service's specialties are those of every
provider linked to it through `provider_services`. That is the same link Week 1's
search uses to decide who offers a service. Four rules keep the text stable and
readable:

- Specialties are de-duplicated and sorted. Two providers sharing a specialty
  list it once, and the database's row order cannot change the text. Task 4.9
  re-embeds a chunk only when its text hash changes, so text that changed while
  the service did not would cost an embedding call for nothing.
- A specialty that repeats the department name is dropped, ignoring
  capitalisation. The seed's Cardiology department has Cardiology providers, and
  "Cardiology · Cardiology" adds no word worth matching.
- With no specialty left, or no provider linked yet, the separator goes too:
  `Cardiology: Echocardiogram. ...`.
- Each part ends with exactly one full stop, whether or not staff typed one.

### What stays out

Provider names and bios stay out of the chunk. It describes the service, and a
bio describes one person who may stop offering it. Chunks are built from
catalogue rows only, so no patient data can reach one.

### Known limitations

The text is built when the service is published. A provider linked afterwards
changes the service's specialties, but its chunk is only rebuilt when the service
is published again (task 4.7). `token_count` is still estimated as characters
divided by four; a real count waits for the embedding model's tokenizer.

## 3. Embedding provider

Everything that needs a vector calls `EmbeddingProvider.embed(texts)` in
`app/ai/embeddings.py` and gets back one vector per text, in order. Each
provider also reports its model name and vector size, to be stored beside every
vector so a model change can find what is stale. The contract is an abstract
base class, so a provider missing `embed()` fails when it is constructed. A
`typing.Protocol` would only be checked by mypy, which this repo runs on
`app/models` alone.

`EMBEDDING_PROVIDER` picks one of three implementations:

| Provider | Used by | How it works |
|---|---|---|
| `LocalEmbeddings` | the running app (the default since Day 2) | Runs `all-MiniLM-L6-v2` inside the API and worker processes with `fastembed`, which uses ONNX Runtime. The image downloads the model (87 MB) at build time and then blocks Hugging Face Hub access, so embedding needs no key, no quota and no network. Each process loads the model once, in about 0.6 s; three chunks then take about 0.06 s |
| `HuggingFaceEmbeddings` | optional, `EMBEDDING_PROVIDER=huggingface` | Sends up to `EMBEDDING_BATCH_SIZE` (64) texts per request to Hugging Face's hosted `all-MiniLM-L6-v2` and gets back 384 numbers per text, already scaled to length 1 |
| `FakeEmbeddings` | the test suite | Hashes each word to one of 384 positions, counts it there and scales the result to length 1, so texts that share words point partly the same way. It uses `hashlib`, because the built-in `hash()` is salted per process and would give different vectors on every run |

The hosted provider was the default on Day 1. On Day 2 the account ran out of
free inference credits and every call returned HTTP 402, so the default moved
to `LocalEmbeddings`. It is the same model: it reproduces every score in the
table below to three decimals.

For the hosted provider, each request pays a network round trip and counts
against the rate limit, so texts go in batches. Ten texts in one request took 0.9 s with the model warm;
the first request after an idle period took 8 s while Hugging Face loaded the
model. `EMBEDDING_TIMEOUT_SECONDS` is 20 s: above that cold start, and below the
publish Activities' 30 s timeout, so a slow call fails inside the provider,
which reports it as transient, before Temporal abandons the attempt.

Every failure is raised as one of two types, so task 4.5 can retry only what a
retry can fix:

| Type | Raised for |
|---|---|
| `TransientEmbeddingError` | timeouts, refused connections, failed TLS handshakes (one happened on the first day of testing), HTTP 429, and HTTP 5xx, including the 503 sent while a model loads |
| `PermanentEmbeddingError` | an empty or non-ASCII key (at startup), any other non-200 status, such as 400, 401, 402 (out of credits), 403 or 404, and any response other than one 384-number vector per text |

Error messages carry the status code and the batch size, and leave out the
texts and the response body. A search query is the patient's own words, and an
error body can echo the request.

The unit tests answer HTTP requests in-process with `httpx.MockTransport`, so
the suite needs no network or key. One test calls Hugging Face for real. It is
marked `live` and skips itself unless run with `RUN_LIVE_TESTS=1`:

```
docker compose run --rm -e RUN_LIVE_TESTS=1 test pytest -m live
```

### Measured similarities

Queries against the three seeded services' chunks, on Week 4 Day 1 through
Hugging Face. `LocalEmbeddings` gave the same scores on Day 2, and
`test_embeddings_local.py` checks the first row against the real model:

| Query | Highest score | Next highest |
|---|---|---|
| "I need a scan of my knee" | Knee X-Ray, 0.614 | Echocardiogram, 0.221 |
| "heart ultrasound" | Echocardiogram, 0.643 | Full Skin Check, 0.185 |
| "someone to look at a mole on my skin" | Full Skin Check, 0.341 | Knee X-Ray, 0.143 |
| "should I take off my jewellery before the scan" | Full Skin Check, 0.386 | Knee X-Ray, 0.245 |
| "Do I need to take my rings off?" | Knee X-Ray, 0.098 | Full Skin Check, 0.094 |
| "parking at the clinic" (no service covers it) | Full Skin Check, 0.268 | Knee X-Ray, 0.110 |

The model ranks well when the query shares words with a chunk and poorly on
paraphrase. Both jewellery questions belong to the Knee X-Ray preparation text,
which says "remove any metal jewellery"; one ranks the skin check first, and the
other barely separates the two. A question no service covers scores 0.268,
close to the 0.341 of a genuine skin question, which leaves a single threshold
a narrow gap. The Day 1 placeholder `RETRIEVAL_MIN_SIMILARITY` of 0.65 would
have rejected even the two clear matches; the eval set replaced it with 0.31
(section 7).

## 4. Vector storage

Each chunk's vector is one row in `chunk_embeddings`, in the same Postgres as
everything else. Code never touches the table directly. It goes through
`VectorStore` in `app/ai/vector_store.py`, which has four methods:

| Method | What it does |
|---|---|
| `search(query_embedding, model, top_k, min_similarity)` | The `top_k` published services nearest the query, each scoring at least `min_similarity`, using only vectors from `model`. `POST /search` calls it (section 6) |
| `embedded_chunk_ids(service_id, model)` | The service's chunks that already have a vector from this model, which `embed_chunks` skips (section 5) |
| `store_service_vectors(service_id, records, current_chunk_ids)` | Stores each record, replacing any vector its chunk already has, and deletes the service's vectors whose chunk is no longer current. Vectors of current chunks it was not given are kept. Running it twice leaves the same rows as running it once, so a retried Activity cannot duplicate a vector and a re-publish cannot leave a stale one |
| `set_published(service_id, published)` | Sets the published flag on all of the service's vectors |

`get_vector_store()` is the only place that names the implementation,
`PgVectorStore`, so moving to another store means writing one class. No method
commits: the caller's transaction covers the vectors and the service's own
status together.

Every row carries the metadata Part B requires:

| Column | Holds |
|---|---|
| `service_id` | The service the chunk describes; a real foreign key |
| `department` | The department's name when the vector was written |
| `specialties` | Every linked provider's specialty, as a list |
| `published` | Whether search may return the vector |
| `model` | Which model produced the vector, since vectors from two models cannot be compared |

`department`, `specialties` and `published` are copies of what the service
already says. They are stored on the vector because a store reached through
the interface, Qdrant for example, could not join to `services` at search time.
The flag is the copy that matters for safety, so it has one rule: every vector
is stored with `published = false`, and only `mark_published` sets it true, in
the same transaction that sets the service's status to `PUBLISHED`. A vector
cannot become searchable before its service's publish has finished.

The table enforces three things itself:

- `vector(384)`: Postgres rejects a vector of any other length, so a provider
  configured for a different model fails at its first insert instead of storing
  vectors that compare as nonsense.
- `chunk_id` is unique: one vector per chunk.
- Deleting a chunk deletes its vector (`ON DELETE CASCADE`). `chunk_content`
  replaces a chunk whose text has changed, so without this a re-publish of an
  edited service would either fail or leave the old vector behind.

There is no vector index. Every search compares the query with every stored
vector, which is exact and, for one clinic's catalogue, small. An approximate
index (HNSW) would skip most comparisons, but it applies `WHERE` filters after
its scan, so with the published filter it can return fewer results than asked
for.

## 5. Embedding in the publish workflow

Publishing a service runs five Activities in order: `validate_service`,
`structure_content`, `chunk_content`, `embed_chunks` and `mark_published`.
`embed_chunks` sends the texts of the chunks that need a vector to the embedding
provider in one `embed()` call, stores the vectors with `store_service_vectors`,
and sets those chunks' `embedded_at`, all in one transaction. The vectors are
stored unpublished, and `mark_published` makes them searchable (section 4).

### Skipping unchanged chunks

Each chunk stores `text_hash`, the SHA-256 of its text. On a re-publish,
`chunk_content` compares the new text's hash with the stored chunk's:

- Same hash: the chunk row is left alone, and so is its vector.
- Different hash: the chunk is deleted, the CASCADE deletes its vector, and a
  new chunk is inserted with no vector.

`embed_chunks` then embeds only the chunks with no vector from the current model.
It asks by chunk id, not by hash: a chunk that survived `chunk_content` has
unchanged text by construction. Asking for the current model as well means
changing `EMBEDDING_MODEL` re-embeds everything, because vectors from two models
cannot be compared. When nothing needs embedding, the provider is not called.

`department` and `specialties` on a kept vector need no refresh. Both appear in
the chunk text (section 2), so a change to either changes the hash. The same is
why section 2 sorts and de-duplicates specialties: the same service must always
produce the same text, or the hash would change when nothing else did.

Checked live against the worker and the local model, on a throwaway service:

| Publish | Worker log | Chunk row | Vector row |
|---|---|---|---|
| First | `embedded 1 of 1 chunks` | 41 | 5 |
| Same text again | `embedded 0 of 1 chunks` | 41, same `embedded_at` | 5 |
| After editing the description | `embedded 1 of 1 chunks` | 42 | 6; row 5 gone |

### Re-publishing and unpublishing

Editing a published service with `PATCH /services/{id}` changes its row at once,
but its chunk and vector still hold the old text. Publishing it again is how the
index catches up. `POST /services/{id}/publish` accepts a `PUBLISHED` or
`INACTIVE` service and moves it straight to `PUBLISHING`; only a service already
`PUBLISHING` gets a 409.

Search filters on the vector's own `published` flag, not on `services.status`
(section 4). So `start_publish` sets the service's vectors to unpublished in the
same transaction that moves it to `PUBLISHING`, and `mark_published` sets them
back when the workflow finishes. In between, `chunk_content` and `embed_chunks`
replace whatever changed. If the workflow never starts, the status and the flag
are both rolled back.

`POST /services/{id}/unpublish` moves a `PUBLISHED` service to `INACTIVE` and
unpublishes its vectors, in one transaction and with no workflow. It keeps the
chunks and vectors, so publishing the service again re-embeds nothing unchanged.

Checked live through the API, on a throwaway service:

| Step | Response | Service | Chunk / vector |
|---|---|---|---|
| Publish (from `DRAFT`) | 202 | `PUBLISHED` | 43 / 7, searchable |
| Unpublish | 200 `INACTIVE` | `INACTIVE` | 43 / 7, not searchable |
| Unpublish again | 409 `SERVICE_NOT_UNPUBLISHABLE` | unchanged | unchanged |
| Publish (from `INACTIVE`) | 202 | `PUBLISHED` | 43 / 7, searchable again, not re-embedded |
| Edit the description, publish again | 202 | `PUBLISHED` | 44 / 8, searchable; 43 / 7 gone |
| Publish again with the worker stopped | 202 | `PUBLISHING` | 44 / 8, not searchable while it waits |
| Two publish requests at once | 202 and 409 | `PUBLISHED` once the one workflow ends | 44 / 8 |

### Batching

`HuggingFaceEmbeddings` sends up to `EMBEDDING_BATCH_SIZE` (64) texts per
request. Every request costs a network round trip and counts against the
provider's rate limit, so texts sent together save both. A service is one chunk,
so one publish is one request carrying one text. Batching matters where many
texts are embedded at once, such as the task 4.10 eval script, and those calls
go through the same `embed()` with a list, never one request per text.

### Retries

| Failure | Examples | What happens |
|---|---|---|
| Transient | a timeout, a refused connection, HTTP 429, HTTP 5xx | Raised retryable (`EMBEDDING_UNAVAILABLE`) and retried: five attempts in all, 2, 4, 8 and 16 s apart |
| Permanent | an empty or non-ASCII key, any other non-200 status (including 402, out of credits), a vector of the wrong size | Raised non-retryable (`EMBEDDING_FAILED`): one attempt |

The transient row describes the hosted provider. `LocalEmbeddings` makes no
network call, so its failures are permanent: a model missing from the image, or
vectors of the wrong size.

Once `embed_chunks` stops, the workflow ends the publish in `PUBLISH_FAILED` and
saves the error in `services.publish_error`. Temporal's default policy would
retry forever and leave the service in `PUBLISHING` while the provider stayed
down. A `PUBLISH_FAILED` service can be published again.

The vector size is checked before any request: the column holds 384 numbers, and
a provider configured for another size fails once with a clear message rather
than as a database error on every attempt.

### Progress

`GET /services/{id}/publish-status` reads `status`, `published_at` and
`publish_error` from the service row. While the service is `PUBLISHING` it also
asks Temporal which Activity is pending, its attempt number, and why the
previous attempt failed. Captured live, with a 0.001 s timeout forcing every
attempt to fail:

```json
{
  "status": "PUBLISHING",
  "progress": {
    "step": "embed_chunks",
    "attempt": 4,
    "last_error": "Hugging Face unreachable (ConnectTimeout)"
  },
  "publish_error": null
}
```

`attempt` is the attempt running or waiting to run. In that run the attempts
were 2, 4 and 8 s apart, and 16 s after the last one began waiting, the service
moved to `PUBLISH_FAILED` with `publish_error` set to
`Hugging Face unreachable (ConnectTimeout)`.

The first live run showed `"last_error": "Failure exceeds size limit."` instead.
Temporal sends an exception's whole cause chain, each link with its stack trace,
and keeps a retrying Activity's last failure only up to a size limit; the httpx
chain under a timeout measured 3.7 KB. `embed_chunks` now re-raises provider
errors `from None`, which brought the same failure down to 110 bytes. The
message still names the underlying exception.

### Known limitations

- With the default `LocalEmbeddings`, the retry path is no longer exercised by
  real network failures. The tests and the live check above cover it, and the
  hosted provider is one setting away.
- The image is about 290 MB larger (200 MB of packages, 87 MB of model), and
  every process that embeds holds the model in memory.
- Adding `embed_chunks` changed the publish workflow's steps. A publish that was
  already running when the new worker started would fail to replay, so deploy
  this with no publish in flight.
- A re-publish takes the service out of search, and out of booking (the saga's
  `validate_eligibility` requires `PUBLISHED`), while its workflow runs: a
  second or two with the local model. A re-publish that fails keeps it out until
  a publish succeeds.
- Between an edit and the re-publish, Week 1's keyword search shows the new
  text and semantic search still matches the old. Nothing re-publishes
  automatically after an edit.

## 6. Retrieval: `POST /search`

`POST /api/v1/search` takes a query in the patient's own words and returns the
published services nearest to it in meaning, each with its department,
specialties and similarity score. Any signed-in user may call it.

```json
POST /api/v1/search
{"query": "knee x-ray", "top_k": 5}

200
{
  "results": [
    {"service_id": 5, "name": "Knee X-Ray", "department": "Orthopaedics",
     "specialties": ["Orthopedics"], "similarity": 0.798}
  ],
  "min_similarity": 0.31,
  "message": null
}
```

The query is embedded with the same provider and model that embedded the
services, and the vector store returns the nearest services in one SQL query
(`PgVectorStore.search`). The service layer is `app/ai/retrieval.py`, which the
Week 5 assistant will call too.

### k and the threshold

| Setting | Value | Who sets it |
|---|---|---|
| `top_k` | 5 by default (`RETRIEVAL_TOP_K`), 1 to 20 | The request |
| `RETRIEVAL_MIN_SIMILARITY` | 0.31, chosen by the eval set (section 7) | Server configuration only |

A service scoring below the threshold is dropped, so fewer than `top_k` results,
or none, can come back. None is a real answer: `results` is empty and `message`
says the clinic does not offer it, rather than showing the least-bad matches. The
request has no threshold field, so no client can ask for weak matches; a
`min_similarity` sent anyway is ignored.

The 0.31 holds for `all-MiniLM-L6-v2` only. Another model spreads its scores
differently, so changing `EMBEDDING_MODEL` means running `make eval` again.

### Filtering

Every filter is a `WHERE` clause in the vector query, applied before ranking, so
an excluded vector never takes one of the `top_k` places:

- **Published only.** The vector's own `published` flag (section 4), which is
  false for a `DRAFT`, `PUBLISHING`, `PUBLISH_FAILED` or `INACTIVE` service.
  "Offered" means the same here as in Week 1's catalogue search: a service is
  offered exactly when it is `PUBLISHED`, and `INACTIVE` is how the clinic
  withdraws one.
- **Current model only.** A vector made by another model is not comparable with
  the query's.
- **The threshold**, as a maximum cosine distance (`<=>` is 1 − similarity).

A service with several chunks appears once, at its nearest chunk's score
(`DISTINCT ON (service_id)`). Today every service has one chunk, so this changes
nothing until one has more.

### PHI scoping

Service search has no patient data to scope. The index is built only from
service, department and specialty fields (section 2), so no query can reach a
patient's details through it; a test searches for a synthetic patient's name
and phone number and finds nothing. The query is the caller's own words and can
name a condition or a person, so it is never logged or stored: the log line
carries the result ids only (`search returned 1 services: [5]`), and a test
checks that a name in the query does not reach the log.

Scoping a patient's own data to the caller starts with the Week 5 assistant,
the first retrieval that reads appointments. It will filter by the
authenticated patient's id in the SQL query, in the endpoint, never in the
prompt.

### Failure

| Request | Response |
|---|---|
| No token | 401 |
| Empty or whitespace-only query, over 1,000 characters (`AI_MAX_QUESTION_CHARS`), or `top_k` outside 1–20 | 422 `VALIDATION_ERROR` |
| The query cannot be embedded: provider down, no key, or a provider configured for vectors of another size | 503 `SEARCH_UNAVAILABLE`; the provider's message goes to the log, not the client |

The route is a plain `def`, not `async def`. Embedding with the local model is
CPU work that would stall every other request if it ran on the event loop;
FastAPI runs a plain `def` route in its thread pool instead.

### Known limitations

- A `PUBLISHED` service with no vector cannot be found. A service published
  before Week 4 added embedding is in that state until it is published again.
- After `EMBEDDING_MODEL` changes, search returns nothing until every service is
  published again, because no stored vector matches the new model.
- A query for a kind of service the clinic offers, but for another part of the
  body or another purpose, gets a weak match instead of "we don't offer that":
  "chest x-ray" returns Knee X-Ray (section 7).

## 7. Retrieval evaluation

`scripts/eval_retrieval.py` (`make eval`) measures how often search finds the
service a person meant, and it chose the threshold in section 6. It runs against
the seeded stack, with the embedding provider and vector store `POST /search`
uses, but ranks every published service with no threshold. The threshold is what
it is choosing, so applying the current one would hide the scores the choice
depends on.

### The eval set

`EVAL_SET` holds 34 queries written the way a patient might type them:

- 24 offered: two for each of the 12 seeded services, each paired with the
  service a person reading the catalogue would pick.
- 10 not offered, such as "dental cleaning" and "eye test for new glasses",
  plus one line of gibberish.

The queries name a service, how it is done, or how to prepare for it ("what
should I wear for a knee x-ray"). None describes a symptom, because picking a
test for a symptom is clinical reasoning. Some are written to be hard: "MRI of
my knee" competes with Knee X-Ray, and "chest x-ray", "pregnancy ultrasound
scan", "sports massage" and "hair transplant" each share words with a service
the clinic does offer.

Expected services are named, not numbered, because ids differ between
databases. The script stops if an expected service is not published, so a typo
or an old seed cannot pass for a miss, and a unit test checks that the set
expects exactly the seeded catalogue.

The seed catalogue grew from 3 services to 12 for this (Week 4 Day 4). With 3,
the right service was always in the top 5, so a top-5 hit rate could only read
100%. The 12 include close pairs on purpose: Knee X-Ray and Knee MRI Scan, Full
Skin Check and Mole Mapping, Resting ECG and the 24-hour monitor, and each
department's consultation beside its other services.

### Hit rate

Measured on Week 4 Day 4 with `all-MiniLM-L6-v2`, no threshold:

| Expected service ranked | Offered queries |
|---|---|
| First | 20 of 24 (83%) |
| In the top 3 | 22 of 24 (92%) |
| In the top 5 | 24 of 24 (100%) |

The four that did not rank first are all short, general requests:

| Query | Expected service: rank, similarity | Ranked first |
|---|---|---|
| physio appointment | Physiotherapy Session: 4th, 0.332 | Cardiology Consultation 0.469 |
| see a skin doctor | Dermatology Consultation: 4th, 0.340 | Full Skin Check 0.383 |
| see a cardiologist | Cardiology Consultation: 2nd, 0.437 | Resting ECG 0.451 |
| see a bone and joint specialist | Orthopaedic Consultation: 2nd, 0.463 | Knee X-Ray 0.491 |

Each still found its service within the 5 results `POST /search` returns.

### Choosing the threshold

`POST /search` returns the 5 best services scoring at least the threshold. For
each candidate, the sweep counts the offered queries whose service would be among
them, and the not-offered queries that would get "we don't offer that":

| Threshold | Offered: service found | Not offered: refused | Right, of 34 |
|---|---|---|---|
| 0.20 | 24 | 2 | 26 |
| 0.25 | 24 | 2 | 26 |
| 0.30 | 24 | 7 | 31 |
| **0.31, chosen** | **24** | **7** | **31** |
| 0.35 | 22 | 7 | 29 |
| 0.40 | 21 | 7 | 28 |
| 0.45 | 20 | 9 | 29 |
| 0.50 | 17 | 10 | 27 |
| 0.55 | 15 | 10 | 25 |
| 0.60 | 14 | 10 | 24 |
| 0.65, Day 1 placeholder | 8 | 10 | 18 |

At the 0.65 placeholder, 16 of the 24 offered queries were told the clinic does
not offer what they asked for. At 0.31 all 24 find their service, and 7 of the
10 not-offered queries are still refused.

Any threshold between 0.298 and 0.332 gets the same 31 right. The highest unrelated
not-offered query scored 0.298 ("hair transplant") and the lowest offered
service 0.332 ("physio appointment"). 0.31 sits 0.012 above the first and 0.022
below the second, closer to the not-offered side on purpose. A patient told
"we don't offer that" about a service the clinic does offer cannot book it; a
weak match still names a real service, which the patient can see does not fit.

Checked live with 0.31 set: "physio appointment" returned Physiotherapy Session
fourth at 0.332, and "hair transplant" returned no results and the "we don't
offer" message.

### What no threshold can fix

Three not-offered queries score above three offered ones, so no threshold can
refuse them without also refusing real services:

| Not-offered query | Best match |
|---|---|
| sports massage | Physiotherapy Session 0.489 |
| pregnancy ultrasound scan | Echocardiogram 0.441 |
| chest x-ray | Knee X-Ray 0.428 |

Each asks for a kind of service the clinic offers, for another purpose or part
of the body. "chest x-ray" is closer to Knee X-Ray (0.428) than "physio
appointment" is to Physiotherapy Session (0.332). Search returns these as weak
matches. The Week 5 assistant will see the matched service's name and text, so
it can tell a patient the clinic's X-ray is of the knee; search alone cannot.

### Known limitations

- 34 queries, written by the same person who wrote the catalogue. The gap the
  threshold sits in is 0.034 wide, and new queries could land inside it.
- The numbers hold for this model and this catalogue. A new model, or services
  with different wording, needs `make eval` run again.

## 8. Testing retrieval offline

Every retrieval test embeds with `FakeEmbeddings` or with vectors written by
hand, so the suite needs no model and no network (task 4.11). `FakeEmbeddings`
hashes each word to a position in the vector, so texts that share words score
high. It knows nothing about meaning, which is enough to test filters, the
threshold and re-indexing.

Two autouse fixtures in `tests/conftest.py` make this hold for every test,
including one written in a hurry:

- `fake_embeddings_by_default` sets `EMBEDDING_PROVIDER` to `fake`. The test
  container reads the developer's `.env`, so a test that forgot to pass a fake
  used whatever that file named: the in-image model here, or the hosted API on
  a machine whose `.env` asks for it.
- `no_network` (`tests/network_guard.py`) makes any lookup of a host outside the
  compose stack raise `NetworkAccessBlocked`. Postgres, Redis and this machine
  are allowed. Tests marked `live` are left alone, and skip themselves unless
  `RUN_LIVE_TESTS=1`. The error is deliberately not an `OSError`: httpx would
  turn one into a connection error, the hosted provider would report "Hugging
  Face unreachable", and a test expecting a 503 would pass while it was really
  trying to reach the internet.

With both fixtures on, the existing suite passed unchanged, so no test had been
reaching the network or depending on `.env`'s provider.

| Guarantee | Tested by |
|---|---|
| Published services only | An unpublished vector is never returned, in the vector store and through `POST /search` for each of `DRAFT`, `PUBLISHING`, `PUBLISH_FAILED` and `INACTIVE`. Unpublishing, and a re-publish while it runs, take the vectors out of search |
| Current model only | A vector made by another model is skipped |
| Threshold | A weaker service is dropped, a score exactly at the threshold is kept, nothing above it gives the "we don't offer" message, and a `min_similarity` sent by the client is ignored |
| PHI | A synthetic patient's name and phone number cannot be found, and the query never reaches the log, on success or on a 503 |
| Re-index | Re-publishing edited text leaves one vector, of the new text; unchanged text keeps its vector; a word only the old text had stops finding the service |

Each guarantee was also broken on purpose on Week 4 Day 4, one at a time: 14
breaks, including turning off each fixture above, and every one failed at
least one test.

## 9. LLM provider

Everything that needs a reply from the model calls `LLMProvider` in
`app/ai/llm.py`. It has two methods. `complete()` returns the whole reply, for
callers that need all of it before acting: the safety check, and a report that
must be parsed as JSON. `stream()` yields the reply in pieces as the model
writes it, for `POST /assistant/ask`. Both report the token counts, which
`ai_interactions` stores beside each answer.

`LLM_PROVIDER` picks one of two implementations:

| Provider | Used by | How it works |
|---|---|---|
| `OpenAICompatibleLLM` | the running app: `groq` (the default) or `openai` | Posts to `/chat/completions` with httpx. Groq takes OpenAI's request format, so one class serves both, and only the address and key differ |
| `FakeLLM` | the test suite | Returns a fixed reply, streamed a word at a time, and records the messages of every call so a test can check what the prompt held. It counts tokens as words |

The client is a thin httpx class rather than a vendor SDK or LangChain. httpx
was already a dependency, and the embedding provider's error handling and
`MockTransport` tests carry over unchanged. The cost is parsing the stream
format by hand: keep-alive comments, an error sent as an event after the 200
status, and the token counts.

The interface is async, unlike `EmbeddingProvider`. A model call spends seconds
waiting on the network and almost no CPU, and awaiting it lets the event loop
serve other requests, bookings included. Local embedding is the opposite, CPU
work, which is why `POST /search` is a plain `def`.

Every request is sent at temperature 0, so the same question gets the same
answer and a refusal test that passes once keeps passing.

### Choosing the model

On Week 5 Day 1 Groq no longer served `llama-3.1-8b-instant`, the model
`.env.example` named; it returned 404 `model_not_found`. Each chat model it did
serve was sent the same one-sentence navigation question at temperature 0,
once, whole and streamed:

| Model | Whole reply | First streamed text | Completion tokens | Of which reasoning |
|---|---|---|---|---|
| `qwen/qwen3.8-27b` | 0.20 s | 0.09 s | 13 | none |
| `allam-2-7b` | 0.19 s | 0.08 s | 35 | none |
| `openai/gpt-oss-20b` | 0.58 s | 0.47 s | 142 | 113 |
| `openai/gpt-oss-120b` | 0.70 s | 0.51 s | 73 | 50 |

`qwen/qwen3.8-27b` is the default. It answered in the one short sentence asked
for, and spends no tokens reasoning. The gpt-oss models reason before they
answer, and those tokens count against `max_completion_tokens` and the free
quota, so a low cap, such as the live test's 10, can run out before the answer
starts.

Switching model is a config change. A reasoning model streams its working in a
`reasoning` field beside `content`. The client reads `content` only, so that
working, which could hold exactly the clinical speculation the assistant
refuses to give, never reaches the patient; a unit test holds it to that.

### Token counts in a stream

A streamed reply carries token counts only when the request sets
`stream_options.include_usage`. Groq then sends them twice: on the event that
ends the answer, and again on a last event with no text. The client holds them
until the stream ends and yields them once, as the last chunk; passed on twice,
a caller adding up tokens would count them twice. A stream the patient
abandons never gets them.

### Failures and retries

| Type | Raised for |
|---|---|
| `TransientLLMError` | timeouts, refused or dropped connections, HTTP 429, HTTP 5xx, and an error event mid-stream |
| `PermanentLLMError` | an empty or non-ASCII key, or one containing a space (at startup), any other non-200 status, such as 400, 401 or 404, and a reply in any other format |

A transient failure is retried once, after a second. A second failure in a row
looks like an outage, and the patient is waiting. A stream is retried only
until its first piece reaches the caller: after that, the start of the answer
may already be on the patient's screen, and a retry would send it twice.

`AI_REQUEST_TIMEOUT_SECONDS` (60) limits each step of a call, connecting or
waiting for more of the reply, not the call as a whole, so a long reply that
keeps arriving is never cut off.

When the caller stops reading a stream, as `/assistant/ask` will when a patient
disconnects, the connection to the provider closes at once, and the model stops
writing a reply nobody will read.

Error messages carry the status code only. They leave out the messages sent
and the response body, for the same reason as the embedding provider's.

The key check for a space comes from Week 5 Day 1. With `LLM_API_KEY=` followed
only by a comment, Docker Compose passed the comment's text as the key, and
every call returned 401. A real key never contains a space.

### Testing

The unit tests answer requests in-process with `httpx.MockTransport`.
`fake_llm_by_default` in `tests/conftest.py` sets `LLM_PROVIDER` to `fake` for
every test, as `fake_embeddings_by_default` does for embeddings, and
`no_network` stops the real client before it reaches Groq. One test calls Groq
for real; it is marked `live` and runs only with `RUN_LIVE_TESTS=1`.

### Known limitations

- Each call opens its own connection, which costs a TLS handshake per call. A
  shared client would avoid that, but would have to be closed at shutdown and
  cannot be shared between event loops, and every test runs in its own.
- Anthropic is not offered. Its API has a different request format and would
  need a second client.
