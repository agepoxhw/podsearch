# Analysis of the four required queries

55 hits at or above cosine 0.20, every one listened to and labelled by hand (`labels.json`). Numbers below are
computed from `labels.json` and `raw.json`. The corpus is 3 episodes (540 chunks of 10 s). The hits come from
`make eval` on commit 7f4fd59, before the hub filter change; the later hub-at-ingest commit hid the same chunks.

## Precision per query

| Query | Hits | Correct | Precision |
|---|---|---|---|
| Putin | 20 | 18 | 90% |
| Ukraine | 12 | 7 | 58% |
| Middle East | 7 | 2 | 29% |
| Iran | 16 | 13 | 81% |
| All | 55 | 40 | 73% |

Where it works: names that are said often and clearly (Putin, Iran) with one episode focused on them. Where it
fails: a region word that is rarely said, or said in passing (Middle East). All 15 wrong hits have the same reason:
the word is not mentioned in the chunk.

## The wrong hits are systematic, not random

15 wrong hits, but only 7 distinct chunks, and 5 of those chunks cause 13 of the 15:

| Chunk | Times wrong | Also correct for another query |
|---|---|---|
| ep289-kotkin 21:20 | 4 | no |
| ep310-bustamante 29:00 | 3 | yes (Ukraine) |
| ep334-amanat 28:20 | 2 | yes (Iran) |
| ep289-kotkin 14:30 | 2 | yes (Middle East) |
| ep334-amanat 13:00 | 2 | yes (Iran) |

These are chunks of general geopolitical talk (war, regions, leaders) without the query word. SONAR places that kind
of speech near every geopolitical query, so the same chunks come back again and again. Random errors would be
spread over many chunks. The same chunk is right for one query and wrong for another, so the chunk is not broken:
the match is topical, not about the word.

## Threshold sweep (finding only: the spec fixes 0.20)

| Threshold | Hits | Correct | Precision |
|---|---|---|---|
| 0.20 | 55 | 40 | 73% |
| 0.22 | 44 | 34 | 77% |
| 0.25 | 30 | 26 | 87% |
| 0.28 | 17 | 14 | 82% |
| 0.30 | 11 | 9 | 82% |

A higher cut-off helps a little (73% to 87% at 0.25) but drops 25 hits, 14 of them correct, and several wrong hits
score above 0.28 anyway (Putin 0.350, Ukraine 0.283). Average score: correct 0.273, wrong 0.243. The gap is small, so
the score alone cannot separate them. Recall was not measured: I labelled hits, not every chunk in the episodes.

## The one change that would help most (my answer)

Average scores over neighbouring chunks, so a hit needs support from the 20-30 s around it. A chunk that is merely
on the topic of geopolitics tends to sit alone between chunks about something else. A real mention of Iran
is usually discussed for more than 10 s, so its neighbours score high too. The wrong hits above are the isolated
kind. This is an untested hypothesis: I did not run it. It needs only the stored vectors plus the chunk order, no
re-ingest.

Rejected for this version: longer queries (changes what the user types), speech detection (fixes hub chunks, not
these wrong hits; see the README).
