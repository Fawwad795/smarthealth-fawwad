# AI layer

How SmartHealth's Part B features find services and answer patients, and why
they are built this way. Sections are added as Weeks 4 and 5 land.

## 1. Embeddings and vector similarity

An embedding model turns a piece of text into a vector: a fixed-length list of
numbers, 384 of them for `all-MiniLM-L6-v2`, the model used here. The model is
trained on large amounts of text so that passages with similar meaning get
similar vectors. With it, "I need a scan of my knee" scores 0.614 against the
Knee X-Ray chunk and no more than 0.221 against any other seeded service. Week
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
and an empty result means "we don't offer that". The threshold will be chosen by
measuring hit rate on the evaluation set (task 4.10).

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

`EMBEDDING_PROVIDER` picks one of two implementations:

| Provider | Used by | How it works |
|---|---|---|
| `HuggingFaceEmbeddings` | the running app | Sends up to `EMBEDDING_BATCH_SIZE` (64) texts per request to Hugging Face's hosted `all-MiniLM-L6-v2` and gets back 384 numbers per text, already scaled to length 1 |
| `FakeEmbeddings` | the test suite | Hashes each word to one of 384 positions, counts it there and scales the result to length 1, so texts that share words point partly the same way. It uses `hashlib`, because the built-in `hash()` is salted per process and would give different vectors on every run |

Each request pays a network round trip and counts against the rate limit, so
texts go in batches. Ten texts in one request took 0.9 s with the model warm;
the first request after an idle period took 8 s while Hugging Face loaded the
model. `EMBEDDING_TIMEOUT_SECONDS` is 20 s: above that cold start, and below the
publish Activities' 30 s timeout, so a slow call fails inside the provider,
which reports it as transient, before Temporal abandons the attempt.

Every failure is raised as one of two types, so task 4.5 can retry only what a
retry can fix:

| Type | Raised for |
|---|---|
| `TransientEmbeddingError` | timeouts, refused connections, failed TLS handshakes (one happened on the first day of testing), HTTP 429, and HTTP 5xx, including the 503 sent while a model loads |
| `PermanentEmbeddingError` | an empty key (at startup), HTTP 400, 401, 403 or 404, and any response other than one 384-number vector per text |

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

Queries against the three seeded services' chunks, on Week 4 Day 1:

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
a narrow gap. The placeholder `RETRIEVAL_MIN_SIMILARITY` of 0.65 would reject
even the two clear matches; task 4.10's eval set will choose the value.

## 4. Vector storage

Each chunk's vector is one row in `chunk_embeddings`, in the same Postgres as
everything else. Code never touches the table directly. It goes through
`VectorStore` in `app/ai/vector_store.py`, which has two methods:

| Method | What it does |
|---|---|
| `replace_service_vectors(service_id, records)` | Deletes every vector the service has, then stores the new ones. Running it twice leaves the same rows as running it once, so a retried Activity cannot duplicate a vector and a re-publish cannot leave a stale one |
| `set_published(service_id, published)` | Sets the published flag on all of the service's vectors |

`get_vector_store()` is the only place that names the implementation,
`PgVectorStore`, so moving to another store means writing one class. Neither
method commits: the caller's transaction covers the vectors and the service's
own status together.

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
  replaces a service's chunks on every publish, so without this the second
  publish of a service would either fail or leave the old vector behind.

There is no vector index. Every search compares the query with every stored
vector, which is exact and, for one clinic's catalogue, small. An approximate
index (HNSW) would skip most comparisons, but it applies `WHERE` filters after
its scan, so with the published filter it can return fewer results than asked
for.
