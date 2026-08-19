# Proposed cleanup — approval required

Nothing in this list was deleted as part of the ByteSize UI/storage work.

1. `templates/landing.html` and its landing-only CSS are now orphaned because
   `/` renders the real Compress screen.
2. `db._scope`, `db.analytics_stats`, `db.analytics_by_extension`, and
   `db.analytics_timeseries` have no callers after the explicitly requested
   analytics-route removal. The old database columns should remain for
   backward compatibility even if these helpers are removed.
3. `config.ASSUMED_LINK_MBPS` is used only by the removed analytics view.
4. `app._reference_sizes` and `config.REFERENCE_CODEC_MAX_BYTES` now perform
   upload-time gzip/plain-Huffman reference work that no surviving UI reads.
   Removing this computation would save processing time; legacy history
   columns can remain.
5. `/compare` and `templates/compare.html` are tested and documented but have
   no navigation link. Either expose the comparison from Files or remove it.
6. `/dashboard` is no longer in the primary navigation now that `/` is the
   actual Compress interface. It can remain as a post-login summary or be
   retired after deciding the desired login destination.
7. `/api/history`, `/api/stats`, and `/api/presets` have no current browser
   caller. Keep them unless API compatibility is intentionally dropped.
8. Compress-tab switching is registered in both `queue.js` and `compress.js`.
   It works, but the duplicate handler can be consolidated in a focused JS
   cleanup.
9. `AFC_WebApp.html` is still referenced by the browser/WASM build workflow,
   so it should be removed only if that standalone build is formally retired.
