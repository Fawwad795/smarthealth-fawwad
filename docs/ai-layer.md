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

The model also only measures how alike two texts are. A question landing near the Echocardiogram
service means its wording resembles that service's description, and says
nothing about the patient's health. That is why the assistant routes patients to
services and never diagnoses.
