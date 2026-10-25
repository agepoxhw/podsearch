# Podcast semantic search (work in progress)

Phase 9 will turn this into the full README. Sections written so far:

## Search quality: hub chunks, and what I would do next

**What happened.** Searching the first three episodes, the same 10-second chunk came out on top for every query,
even nonsense. It is audio with no words (music or a jingle). SONAR's speech encoder gives such audio a
generic vector that sits close to every text query, so it matches everything. I measured it: two chunks
averaged about 0.40 cosine against 40 unrelated queries, while a normal chunk averages 0.03.

**What I did (the hub filter).** Each chunk gets a hub score: its average similarity to 40 unrelated queries
("banana bread", "chess", ...). Normal speech is about something, so it scores low; a hub scores high. The
average over 40 queries is one dot product with their mean vector, so that mean is computed once (`make probes`),
committed as a 10 KB file, and used by the speech workers to score every chunk while it is embedded. The API
hides chunks above 0.17 (mean + 3 standard deviations, calibrated on the first 540 chunks). The score depends only
on the chunk, never on other audio, so nothing is rescanned or rewritten when new audio arrives. No vector
is changed, so scores stay true cosine. `docs/hubs-explained.html` explains this with charts.
The hub filter returns a subset of the hits that pass 0.20: hubs are hidden, and `below_threshold=true` (a debug
opt-in) shows everything including them.

**At 1000x the audio.** Hub filtering scales because it is per-chunk and stateless: one 1024-dim dot product per
chunk, computed inside the Ray Data embedding stage and stored with the vector. My first design (a corpus-wide
z-score recomputed after every ingest, scrolling all vectors into one driver and rewriting every point) would
need about 20 GB of driver memory, take tens of minutes, block ingest, and starve under continuous arrivals,
leaving new hubs unfiltered. A periodic sample-based job to watch the score distribution (not to rewrite it)
is the follow-up.

**Caveat: the cut-off is calibrated on three episodes.** The hub score of a chunk never depends on other audio
(it uses 40 fixed probe queries), but the cut-off 0.17 was chosen from the first 540 chunks. Hubs score about 0.40
and normal speech about 0.03, so the gap is wide and the cut-off is not fragile, but I have only seen it on three
episodes. With other shows, languages or recording quality the distribution may shift. Retuning is cheap:
`HUB_MAX` is an API setting applied at query time, so it needs no re-ingest, and `make hubness stats` prints the
current distribution. What is missing is a periodic job that watches that distribution and alerts on drift.
The filter also cannot remove speech that is merely near a topic: the recurring wrong hits in `results/` score
below the cut-off.

**What I would do with more time (speech detection).** The root cause is that non-speech audio gets indexed at
all. A voice-activity detector in the ingest step would drop chunks without speech before they are embedded, so
they never reach the index. It needs a detector in the speech image and a re-ingest of all audio, which is why
it is not in this version. The hub filter would stay as a safety net for odd chunks the detector misses.

**Result quality.** Over the four required queries 40 of 55 hits are correct (73%). Putin 90%, Iran 81%,
Ukraine 58%, Middle East 29%. The wrong hits are systematic: five general-geopolitics chunks cause 13 of the 15
errors, and none of them mention the query word. The one change I would make is averaging scores over neighbouring
chunks (a hit needs support from the 20-30 s around it); it is untested. Details: `results/analysis.md`.

**Related finding.** On this model the 0.20 cosine threshold alone does not reject unrelated queries: before
the filter, most nonsense queries still returned hits above 0.20.
