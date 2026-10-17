# Retrieval demo runbook

A 15-minute walk through Week 4: a patient's search, a service published and
found, re-indexed after an edit, withdrawn, and an embedding failure surfacing
in the publish status. Each section names the Definition of Done item it
proves. Every command here was run against this stack on 2026-10-15, and the
outputs shown are from that run.

Run it from Git Bash. Swagger UI (<http://localhost:8000/docs>) can make the
same calls, but the helpers below keep each beat to one line.

---

## 0. Before the meeting

```bash
docker compose ps                     # 12 services, all running
docker compose run --rm test pytest --cov=app -q   # 548 passed, 98.15% (3 min)
```

Open <http://localhost:8233> (Temporal UI) and `docs/ai-layer.md`. In a second
terminal, tail the worker; the re-index beat happens here:

```bash
docker compose logs -f --since 1s temporal-worker | grep --line-buffered embedded
```

Then define the helpers in the main terminal:

```bash
B=http://localhost:8000/api/v1
tok() { curl -s -X POST $B/auth/login -H 'Content-Type: application/json' -d '{"email":"'"$1"'","password":"ChangeMe123!"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'; }
ADMIN=$(tok admin@medinova.example); PAT=$(tok patient.one@example.com)
search() { curl -s -X POST $B/search -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' -d "{\"query\":\"$1\",\"top_k\":3}"; echo; }
rows() { docker compose exec -T postgres psql -U app -d app -c "SELECT c.id AS chunk, left(c.text, 60) AS text, e.id AS vector, e.published, e.department, e.specialties FROM content_chunks c LEFT JOIN chunk_embeddings e ON e.chunk_id = c.id WHERE c.source_type = 'SERVICE' AND c.source_id = $SID;"; }
watch_status() { prev=""; while :; do cur=$(curl -s $B/services/$SID/publish-status -H "Authorization: Bearer $ADMIN"); [ "$cur" != "$prev" ] && echo "$cur" && prev=$cur; case $cur in *'"status":"PUBLISHED"'*|*'"status":"PUBLISH_FAILED"'*) break;; esac; sleep 0.2; done; }
```

---

## 1. A patient searches (1 min)

*DoD: relevant services with scores and department/specialty; a below-threshold
query gets "we don't offer that".*

```bash
search "I need a scan of my knee"
search "walking boot fitting"
```

The first returns Knee MRI Scan 0.694, Knee X-Ray 0.614 and Orthopaedic
Consultation 0.386, each with its department and specialties. The second
returns no results and the "We don't offer a service that matches this search"
message: its best match is below the 0.31 threshold.

**Say:** search is cosine similarity between the query's vector and each
published service's vector, with every filter in the SQL query.

## 2. Publish a service, then find it (3 min)

*DoD: publishing produces chunks and embeddings in the workflow, with visible
progress; search excludes drafts; chunk contents and metadata are deliberate.*

```bash
SID=$(curl -s -X POST $B/services -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d '{"department_id": 2, "name": "Walking Boot Fitting", "description": "Fitting and adjustment of a walking boot or ankle brace.", "prep_instructions": "Wear a loose sock and bring the boot or brace if you already have one."}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])'); echo $SID
search "walking boot fitting"          # still not offered: the service is DRAFT
curl -s -X POST $B/services/$SID/publish -H "Authorization: Bearer $ADMIN"; echo
watch_status
```

The publish returns 202 with `"workflow_id":"publish-service-<id>"`.
`watch_status` prints each change: `PUBLISHING` with the step in progress
(`progress.step`), then `PUBLISHED`, in about 5 s. The run is quick, so it
catches some steps, not all five. Temporal UI's event history for
`publish-service-<id>` shows every one: `validate_service`,
`structure_content`, `chunk_content`, `embed_chunks`, `mark_published`.

```bash
rows
search "walking boot fitting"
```

One chunk, one vector, `published = t`, department `Orthopaedics`. Search now
finds it at 0.636.

**Say:** one chunk per service, `{department} · {specialties}: {name}.
{description} Preparation: {prep}`. A service is a few sentences, so splitting
would only separate its name from its preparation text (`docs/ai-layer.md`
section 2). The vector stays unpublished until `mark_published`, in the same
transaction as the status.

## 3. Re-publish and re-index (2 min)

*DoD: re-publishing re-indexes and leaves no stale vectors.*

```bash
curl -s -X POST $B/services/$SID/publish -H "Authorization: Bearer $ADMIN"; echo; watch_status
```

The worker terminal prints `embedded 0 of 1 chunks`: the text hash is
unchanged, so nothing is re-embedded. Now edit the wording:

```bash
curl -s -X PATCH $B/services/$SID -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d '{"description": "Fitting and adjustment of a walking boot.", "prep_instructions": "Wear a loose sock and bring the boot if you already have one."}'; echo
search "ankle brace fitting"           # 0.683: search still holds the old text
curl -s -X POST $B/services/$SID/publish -H "Authorization: Bearer $ADMIN"; echo; watch_status
rows
search "ankle brace fitting"           # 0.516: the new vector
```

The worker prints `embedded 1 of 1 chunks`. `rows` shows a new chunk id and a
new vector id, and still one row: the CASCADE removed the old vector with its
chunk.

**Say:** between the edit and the re-publish, search matched the old text. That
gap is a recorded limitation; nothing re-publishes automatically.

## 4. Withdraw it (1 min)

*DoD: search excludes withdrawn services.*

```bash
curl -s -X POST $B/services/$SID/unpublish -H "Authorization: Bearer $ADMIN"; echo
search "walking boot fitting"          # not offered again
curl -s -X POST $B/services/$SID/unpublish -H "Authorization: Bearer $ADMIN"; echo   # 409
```

200 with `"status":"INACTIVE"`, then the "we don't offer" message, then 409
`SERVICE_NOT_UNPUBLISHABLE`. The chunk and vector are kept, unpublished.

## 5. An embedding failure, retried and surfaced (2 min, optional)

*DoD: embedding failures retry and surface in the publish workflow status.*

Swap the worker for one using the hosted provider with a 0.001 s timeout, so
every embedding call fails. Change the text first: an unchanged chunk already
has its vector, so `embed_chunks` would skip the provider and nothing would
fail.

```bash
curl -s -X PATCH $B/services/$SID -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d '{"description": "Fitting and adjustment of a walking boot, with a strap check."}'; echo
docker compose stop temporal-worker
docker compose run -d --rm --name demo-failing-worker -e EMBEDDING_PROVIDER=huggingface -e EMBEDDING_API_KEY=demo-not-a-key -e EMBEDDING_TIMEOUT_SECONDS=0.001 temporal-worker
curl -s -X POST $B/services/$SID/publish -H "Authorization: Bearer $ADMIN"; echo; watch_status
```

The new worker takes about 10 s to pick the workflow up. Then `embed_chunks`
shows attempts 2, 3, 4 and 5, each with `"last_error":"Hugging Face unreachable
(ConnectTimeout)"`, 2, 4, 8 and 16 s apart, and the service ends
`PUBLISH_FAILED` with that `publish_error`, about 43 s after the publish.
Restore the real worker and recover:

```bash
docker stop demo-failing-worker
docker compose start temporal-worker
curl -s -X POST $B/services/$SID/publish -H "Authorization: Bearer $ADMIN"; echo; watch_status
```

**Say:** timeouts, 429 and 5xx retry; a bad key or a wrong-sized vector fails
at once. Temporal's default would retry forever and leave the service in
`PUBLISHING`.

## 6. PHI (1 min)

*DoD: PHI-scoping test included.*

```bash
docker compose exec -T postgres psql -U app -d app -c "SELECT source_type, count(*) FROM content_chunks GROUP BY source_type;"
docker compose logs api --since 15m | grep -o "search returned.*" | tail -3
docker compose logs api --since 15m | grep -ci "walking\|ankle\|knee"
```

Only `SERVICE` chunks exist, so no query can reach a patient's details. The log
lines carry result ids (`search returned 2 services: [14, 5]`; ids vary), and
the last command prints `0`: no query text reached the log.

**Say:** patient-scoped retrieval starts with the Week 5 assistant, the first
retrieval that reads appointments. It will filter by the caller's patient id in
SQL, never in the prompt.

## 7. How the threshold was chosen (2 min)

*DoD: eval set committed; hit-rate results in `docs/ai-layer.md`.*

```bash
docker compose exec -T api python -m scripts.eval_retrieval
```

About 10 s. Top-1 20/24, top-3 22/24, top-5 24/24; the sweep marks 0.31 as
current, 31 of 34 right.

**Say:** any threshold from 0.298 to 0.332 gets the same 31 right. 0.31 sits
nearer the not-offered side, because a patient wrongly told "we don't offer
that" cannot book. "chest x-ray" (0.428 on Knee X-Ray) scores above three real
answers, so no threshold refuses it.

## 8. Offline tests and the key check (1 min)

*DoD: tests pass without network calls; no API key in git history.*

```bash
docker compose run --rm test pytest tests/unit/test_offline_guards.py tests/integration/test_search_routes.py tests/integration/test_service_reindex.py -q
git log --all -p | grep -E "hf_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}" | head
```

36 passed, in under a minute. The `git log` search prints nothing.

## 9. Questions for the mentor

- Is the in-image local model acceptable in place of a hosted API?
- Is "no patient data in the index" enough PHI scoping for `/search` this week?
- Near misses get a weak match, not a refusal. Acceptable, with the Week 5
  assistant saying what the clinic does offer?

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| Create returns 409 `SERVICE_NAME_TAKEN` | A rehearsal left the service. Rename it (below) and create again |
| Publish stays `PUBLISHING` with no progress | No worker: `docker compose start temporal-worker` |
| Every publish fails after section 5 | The failing worker is still up: `docker stop demo-failing-worker`, then start the real one |
| `tok` prints a `KeyError` | Login failed; `docker compose exec api python -m scripts.seed` restores the seed passwords |

## Afterwards

Rename the demo service so the runbook can run again. It stays as an
unpublished row, since rows are never deleted:

```bash
curl -s -X POST $B/services/$SID/unpublish -H "Authorization: Bearer $ADMIN"; echo   # 409 if already INACTIVE
curl -s -X PATCH $B/services/$SID -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d "{\"name\": \"Demo Walking Boot Fitting $(date +%s)\"}"; echo
```
