# Podcast semantic search (work in progress)

Phase 9 will turn this into the full README. Sections written so far:

## Search quality: hub chunks, and what I would do next

**What happened.** Searching the first three episodes, the same 10-second chunk came out on top for every query,
even nonsense. It is audio with no words (music or a jingle). SONAR's speech encoder gives such audio a
generic vector that sits close to every text query, so it matches everything. I measured it: two chunks
averaged about 0.40 cosine against 40 unrelated queries, while a normal chunk averages 0.03.

**What I did (the hub filter).** After ingest, `make hubness` scores each chunk by how similar it is to those 40
unrelated queries and stores the score on the chunk. The API hides chunks with a score above 3 standard
deviations. This is cheap (no re-embedding, no extra work per search) and keeps scores as true cosine.
Limits: new episodes are not scored until `make hubness` is run again, and the cut-off of 3 was picked by
looking at three episodes.

**What I would do with more time (speech detection).** The root cause is that non-speech audio gets indexed at
all. A voice-activity detector in the ingest step would drop chunks without speech before they are embedded, so
they never reach the index. It needs a detector in the speech image and a re-ingest of all audio, which is why
it is not in this version. The hub filter would stay as a safety net for odd chunks the detector misses.

**Related finding.** On this model the 0.20 cosine threshold alone does not reject unrelated queries: before
the filter, most nonsense queries still returned hits above 0.20.
