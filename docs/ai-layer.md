# AI layer

How SmartHealth's Part B features find services and answer patients, and why
they are built this way. Sections are added as Weeks 4 and 5 land.

## 1. Embeddings and vector similarity

An embedding model turns a piece of text into a vector: a fixed-length list of
numbers (for example 384 or 1,536, fixed by the model). The model is trained on
large amounts of text so that passages with similar meaning get similar vectors,
even when they share no words. "Do I need to take my rings off?" lands close to
the Knee X-Ray preparation text, "remove any metal jewellery near the knee".
Week 1's search matches the query against service names with `ILIKE` and finds
nothing for that question.

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
