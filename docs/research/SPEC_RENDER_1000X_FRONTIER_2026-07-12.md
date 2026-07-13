# Spec render 1000x frontier — final closure

> **Superseded on 2026-07-13.** This historical closure predates the
> descriptor-retained cache rerun, restart-safe one-render finalization, closed
> PNG transport receipt, and whole-project hardening audit. Do not cite its
> 9444.805x / 212.992x / 352.885x figures as current. The authoritative current
> ledger is [`SPEC_ENGINE_WHOLE_PROJECT_PASS_2026-07-13.md`](SPEC_ENGINE_WHOLE_PROJECT_PASS_2026-07-13.md).

Updated 2026-07-13.

## Outcome

The campaign crossed 1000x for exact-request artifact transport. It did not cross 1000x for a fresh, unique, independently gated render.

- Final exact transport: **0.011900375 s median, 0.012294225 s type-7 p95, and 0.012490875 s slowest**.
- Against the pinned 112.396726 s frame-9 reference, that is **9444.805x median and 8998.307x slowest**. All 9/9 trials exceeded 1000x.
- Every delivery contained the exact same 8,310,590 bytes with SHA-256 `785044e407ca68c12fd25a874492be8ceb4eb27d7d0631909c4cb100075fad36`.
- The fastest independently gated fresh-render experiment is Spatial75 at **0.550618666 s median / 212.992x**. All 7/7 timing trials exceeded 200x; the predeclared trial-zero quality proof and its current independent-verifier replay pass.
- Removing the second independent render and reference-free agreement gate improves the measurement-only upper bound to **0.332339667 s / 352.885x**, but it remains 2.834 times over the 1000x latency budget and is not authorized for publication or production.
- Two cross-frame temporal arms have quality-passing composed estimates above 1000x. They are post-hoc, local-unattested, non-integrated estimates and are explicitly unauthorized.

The honest frontier is therefore:

1. **9444.805x** for exact byte transport of an exact request, with source eligibility inherited unchanged;
2. **212.992x** for a fresh, independently gated preview render; and
3. **352.885x** as a deliberately ungated one-render measurement ceiling.

No result upgrades the cached or rendered artifact to production-ready, product-verified, or billable eligibility.

## Authoritative method matrix

| Method | Denominator | Result | QA result | Valid claim |
|---|---:|---:|---|---|
| Spatial75, two-render gated | 117.277528 s frame 11 | 0.550618666 s median; 0.555222384 s p95; 212.992x median | Predeclared timing trial 0 passes quality-v3 and current independent-verifier replay | 7/7 timing trials exceed 200x; fastest fresh, independently gated preview experiment; local-unattested |
| Spatial75, one render | 117.277528 s frame 11 | 0.332339667 s median; 0.349205434 s p95; 352.885x median | 7/7 quality-v3 and verifier replays pass | Measurement-only upper bound; no second render or gate; publication false |
| One-render OIDN reconstruction | 112.396726 s frame 9 | 1.847133584 s selected observation; 60.849x | Quality-v3 passes selected blend | Same-render confidence only; no independent verification receipt |
| Resident full-resolution Cycles, 4 SPP | 112.396726 s frame 9 | 1.655698917 s selected observation; 67.885x | Quality-v3 evaluator passes | Experimental measurement; no independent verifier receipt |
| Resident EEVEE, 4 SPP | 112.396726 s frame 9 | 0.798478791 s median; 140.764x | Quality-v3 fails | No quality-preserving claim |
| Resident Workbench | 112.396726 s frame 9 | 0.717367 s median; 156.680x | Quality-v3 fails | No quality-preserving claim |
| Narrow 50%-resolution, 1-SPP render-and-write floor | 112.396726 s frame 9 | 0.099163042 s median total for the measured low-resolution path; arithmetic 1133.454x | Quality-v3 fails | Incomplete floor only; full-resolution reconstruction/final encoding, validation, and durable publication are not included |
| Temporal direct prior | 112.396726 s frame 9 | 0.082803418 s composed estimate; 1357.392x | Quality-v3 and current verifier pass | Unauthorized cross-frame audit; not an integrated wall measurement |
| Temporal linear extrapolation | 112.396726 s frame 9 | 0.092781626 s composed estimate; 1211.411x | Quality-v3 and current verifier pass | Unauthorized cross-frame audit; not an integrated wall measurement |
| Hardened exact transport | 112.396726 s frame 9 | 0.011900375 s median; 0.012294225 s p95; 9444.805x median | Exact SHA-256 byte identity in 9/9 trials | Exact-request transport only; inherited preview/non-production eligibility |

The OIDN and resident Cycles entries are selected observations, not repeated-trial medians. The 0.099163042 s mutation result is intentionally not combined with an unrelated post-processing observation: it is only a low-resolution render-and-write floor, and its reconstructed output fails quality-v3.

## Measurement and statistical limits

The 112.396726 s denominator is a local-unattested, fixed-order single reference trial with no variance estimate, measured resident steady-state after an uncharged warmup. Each Spatial frame-11 denominator is likewise one local-unattested reference trial. Candidate and transport summaries use the trial counts recorded in their receipts.

- Exact transport uses 9 trials and reports median, type-7 p95, minimum, maximum, and population variance. Its measured interval begins before strict index lookup and ends after durable artifact and bound-sidecar publication; aggregate receipt publication is excluded.
- Two-render Spatial75 uses 7 candidate trials. Candidate wall time includes both render endpoints, manifests, retained-snapshot conversion, the reference-free pair gate, reconstruction, validation, and PNG0 publication. The fresh 4096-SPP quality reference, quality-v3 evaluation, and independent verification are measurement-only and excluded from the headline wall.
- One-render Spatial75 uses 7 trials and excludes the second render and pair gate by design. Quality evaluation and verification are excluded from the wall and can invalidate, but cannot authorize, the result.
- Temporal estimates use 9 resident trials. The headline is the sum of one-time product-input validation and the resident median. It is explicitly **not** an integrated wall median. Timing is local self-reported evidence without an external signature; validators bind code, artifacts, arithmetic, reconstruction, and quality, but do not independently attest the clock.

These limits prevent a benchmark statistic from being presented as a production latency SLA or a generalized workload result.

## Why fresh unique rendering stopped below 1000x

The frame-11 1000x budget is 0.117277528 s.

- The one-render endpoint and manifest alone have a 0.262702041 s median. The complete one-render path is 0.332339667 s, or 2.833788 times the budget.
- Holding its other median components constant would leave only about 0.04734 s for the render endpoint. The measured endpoint therefore needs roughly another 5.55x reduction before the complete one-render path can reach 1000x.
- The 0.099163042 s low-resolution mutation path fits under the raw frame-9 cutoff only because it omits required full-resolution reconstruction/final encoding, validation, and durable publication; its reconstructed output also fails quality-v3.
- EEVEE and Workbench reduce some render work but remain slower than the cutoff and fail the quality contract.
- The selected OIDN branch spends 1.768806667 s before reconstruction, so that branch cannot approach the cutoff.
- The independently gated two-render lane necessarily pays for two disjoint renders and the reference-free agreement gate. Its 212.992x result is the defensible fresh-render frontier, not a path to a 1000x claim without a different renderer or representation.

In the tested branches, the measured value proposition has diminishing returns: further sample or resolution removal reaches the latency budget only by dropping required work or failing quality. The next meaningful fresh-render advance must attack the render endpoint and reconstruction/encode architecture, not tune another small Python-level stage.

## Exact transport closure

The cache key binds frame, manifest, serialized scene bundle, complete render recipe and policy, renderer/backend/Blender identities, and the expected output contract. Source eligibility is part of that binding.

The measured transaction includes:

1. strict cache-index lookup;
2. full cached-source SHA-256 validation;
3. descriptor-based copy into a staged file, copied-byte hashing, and file `fsync`;
4. no-clobber publication;
5. destination inode, size, and full-SHA validation plus destination `fsync`;
6. parent-directory `fsync`;
7. durable publication of the bound sidecar receipt; and
8. rollback with directory `fsync` if sidecar publication fails.

Cache population, post-hoc cross-frame quality evaluation, fresh 4096-SPP target references, independent proof recomputation, and aggregate screen-receipt publication are disclosed but excluded from hit latency.

The exact transport is authorized only for the exact request identity. The source remains preview-only, non-production-ready, unverified, and non-billable. Frame-9 reuse against frames 10 and 11 passed the retained quality audits, but those comparisons are post-hoc and do not authorize general cross-frame reuse.

For a simple median expected-latency model in which a miss costs the full 112.396726 s reference and a hit costs 0.011900375 s, at least **99.910578%** of requests must be exact hits to sustain 1000x aggregate speed:

`(112.396726 - 0.112396726) / (112.396726 - 0.011900375)`

This is a modeled coverage threshold, not a measured workload hit rate or tail-latency SLA.

### Storage trust boundary

The receipt explicitly trusts same-UID storage writers not to mutate files during the transaction or after return, and every consumption performs full SHA-256 validation. The final audit found one additional hardening opportunity inside that excluded threat model: keep the delivery destination descriptor open and recheck it after parent-directory `fsync`. The current implementation validates and `fsync`s the destination before closing it, then `fsync`s the directory. This is not a release blocker under the declared same-UID boundary, and changing it would require a new cache evidence cycle without changing the measured product claim.

## Temporal frontier interpretation

The direct-prior resident median is 0.039654042 s, with 0.040888275 s resident p95 and 0.041133458 s resident slowest. Adding 0.043149376 s of one-time frame-9/frame-10 product-input validation produces the 0.082803418 s composed estimate.

The linear arm has a 0.049632250 s resident median, 0.051989000 s resident p95, and 0.052779834 s resident slowest. The same input-validation charge produces 0.092781626 s.

Both arms pass quality-v3 and current independent-verifier replay against the pinned fresh frame-11 audit reference. Neither arm is authorized: the target was audited post-hoc, the product decision was not reference-free, the estimates are not integrated wall measurements, and the timings are local-unattested. The unavailable optical-flow arm was not silently substituted; it is recorded as unavailable because `cv2` was absent.

## Release evidence ledger

Only the following four closure artifacts are authoritative:

| Evidence | Path | SHA-256 |
|---|---|---|
| Exact transport | `/Users/scammermike/.cache/cx-spec-lab/cache-arm/koro-static-cache-copy-generic-hardened-f9-v-f10-f11-20260712f/receipt.json` | `e68b3736266fa2c5b4ff3b30597af7fcd24a7d8bc64e79913832ab36931f6b1f` |
| Spatial75 two-render | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-spatial75-release-closed-f11-20260713/receipt.json` | `8419eba32c1602786640ac366c67eb9a9e2e98df519d5e47f5e94fc9b997716d` |
| Spatial75 one-render upper bound | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-spatial75-one-render-release-closed-v2-f11-20260713/spatial75-one-render-upper-bound.json` | `9ede62a385471f4bdc3abbb49bf096f9316cd693f1298f0f3ac7f9164bac481e` |
| Temporal prediction | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-temporal-prediction-f11-20260713-closed-v3/temporal-prediction-frontier.json` | `12f8be655933b5735ff2325125acb1507b1059b6c679735fe11d362d512e04f4` |

Supporting evidence:

| Branch | Path | SHA-256 |
|---|---|---|
| Frame-9 performance provenance | `/Users/scammermike/.cache/cx-spec-lab/cache-arm/koro-static-cache-copy-generic-hardened-f9-v-f10-f11-20260712f/evidence/frame-9-performance-receipt.json` | `b1ca40bc63288ad8736b76592c24a9baffe168f752c8309ac6925cc2c22f19c8` |
| OIDN selected sweep | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-one-render-oidn-810x1440-s4-f9-20260712/cpu-residual-sweep-v2/one-render-residual-sweep.json` | `b0171ad02647bc14f3e58ea0c41dc929ecae0ff44edfd5de8af25368a0e288f5` |
| OIDN source screen | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-one-render-oidn-810x1440-s4-f9-20260712/one-render-confidence-screen.json` | `8b6cfc4bf41059dd3620c9355a7af701ed64683002c419dc03a6ec48db318573` |
| Resident Cycles/endpoint screen | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-inmemory-endpoints-1080x1920-f9-20260712-v1/inmemory-endpoint-screen.json` | `de7475b45bf460a3d87cf63d13cc8356ef5a4a7849245fd7a9f15d2440a2e1ba` |
| Resident EEVEE/Workbench | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-resident-raster-followup-1080x1920-s4-f9-20260712-v1/resident-raster-followup.json` | `51f83f18d25b74c2b34468f70836f8b7adeb7dbd1f31d0f3bc056dc8b95736bc` |
| Resident mutation floor | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-resident-mutations-540x960-s1-f9-20260712/resident-mutation-screen.json` | `1342930ce102d2a5264e37869079772fdc061b660691e94cc4295e765c5567b5` |
| Mutation quality-fail proof | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-resident-mutations-540x960-s1-f9-20260712/one-render-bicubic-quality-v3.json` | `2496f1c95e6b452b2c3fbc849f41cda5ef860ed7029b049092c36c032404c002` |

The cache receipts ending in `20260712a` through `20260712e`, earlier Spatial75 receipts including SHAs beginning `099530` and `7b920`, the one-render report with SHA beginning `0e1e1dde`, and temporal v1/v2/pre-closure reports are superseded and must not be cited as current closure or headline evidence. Older receipts remain supporting provenance only where an authoritative closure artifact pins them immutably.

## Frozen implementation pins

Key current pins are:

- Cache screen `1dac36c2f732d61a3a1aa1985ac3c16e67abc666e2e859e0d65b1585e63212c6`; cache tests `ff595873b45b1626a5d104ddce32d0734a5b158bdb76815b74d548c5e648d25d`.
- Spatial harness `c5866a218c24471da6e2cc61359407f97a084f9d7d0f83b675e12152308f7fb2`; harness tests `22317973277694eeaa2af5039cd51bf864011dc3ada9471af65ecfc44c394ee0`.
- Spatial operator `555f20cafe0d7724c06f8f87a019109a7efe7333696c8408e6fb649aeacd5027`; operator tests `1825e1dc6dc1e51d155608823be9db2733ac799f23b29fe844327361fa3c4b16`.
- One-render screen `1643c7fa4a2ba722a8e75eb3086af71d12734fb2ce905644c4728c7e6fb83f44`; one-render tests `89f7e13871390ba416687671f0b5bec06837dd16c4b9ea64f229e6e65e8ca9b8`.
- Backend `ae1a9c2c07529c5d591177f99882e7532fa8e62e74f956b31e29064b26dfc08f`; driver `5d05da0214b9dab5446910a303691b2a5b342a6658a22e1d4aa0faf07abc1e6b`; core `c729c8b9e5d231f9c6566d1bc8c492af2ecad4fc2aade7cf2a188f35428d9a11`; adapter `fbd72047b0497a6ce886c22bb86ba4a4b374a5ebd05b86cc1eb4c2d91afd534e`.
- Quality-v3 module `819d5b3c2ba6da2b67e3e9feffece242e7b17d2b55f858d783962b0048c81c5e`; verifier `5cb8b25edae1638f61e1f2cd17770b6be0eba517680d0b089c65c3210ee788ed`; contract `6665e2931fed124108929bfd9cfc093c6db69407fcd7fc8a39644c4e39183b0b`.
- Temporal screen `06b780e73e4bad3809ebed97f19e2748accb99178f1f919f915dd5fe53c5f5f7`; tests `072f9255dfb0408293adcff017844f27e116dca6cbae5ae3b34838248edca292`; validator `5e15cbd17c9a107cb2966cd0b69e71cb023f080d4ed65c391116e7bbe66c6dd8`.

## Final QA and audit

The post-audit broad regression ran **264 tests in 34.103 s**. Every executed test passed; two deliberately opt-in integration smokes were skipped because their environment flags were not enabled:

- installed-Blender wire-protocol smoke: `CX_RUN_REAL_CYCLES_PREVIEW_SMOKE=1`;
- VideoToolbox mux smoke: `CX_RUN_REAL_CYCLES_SEQUENCE_MUX_SMOKE=1`.

`python3 -m compileall -q scripts/spec-lab` passed. All four authoritative closure artifacts then replayed successfully against the current code and files, and their SHA-256 values matched the ledger above.

The closing audit found no release blocker. Hardening added during this campaign includes exact current code and test pins, immutable timing evidence, strict arithmetic and policy replay, current quality-verifier replay, serialized scene and renderer identity rebinding, resolve-beneath/no-symlink checks, unique-inode artifact checks, fail-closed atomic publication, and adversarial mutation tests for proof, timing, artifact, path, pin, and authorization substitution.

## Next useful work

Further progress should be treated as a new campaign with three separate goals:

1. **Fresh-render latency:** replace or redesign the 0.2627 s one-render endpoint and 0.0586 s reconstruction/encode/validation stage while keeping the independent pair gate.
2. **Temporal authorization:** predeclare trials on unseen sequences, measure a truly integrated wall path, add external timing attestation, and require a reference-free product gate.
3. **Production cache economics:** measure real exact-hit distributions and tail latency using production-eligible, verified source artifacts. The modeled 99.910578% hit threshold is the first go/no-go boundary.

Small local tuning has reached diminishing returns. Those three changes can move the actual product boundary; another unintegrated microbenchmark cannot.
