# Spec Evaluation Report: `.specs/003-agui-first-class/spec.md` (AG-UI as a first-class alloy transport)

**Rubric used**: `~/.claude/skills/spec-judge/SKILL.md` (found and loaded — this report follows it exactly).
**Mode**: Maximally harsh, single pass, no refinement loop. False positives flagged rather than omitted.

## Summary
- **Total Score**: 93/120 (77.5%)
- **Grade**: C — Adequate spec, known gaps, medium risk
- **Size Tier**: Large (self-declared in the spec header; confirmed — 546 lines, 5 journeys, 20 FRs, 9 NFR sub-sections, 3+ integrations)
- **Spec Format**: Hybrid — IEEE-style FR table + user-journey/Given-When-Then ACs, consistently applied
- **Buildability Ratio (FR-level)**: B:D:U ≈ 13:5:2 (65%:25%:10%) — **Mediocre band**, short of the "Good" bar (>70% B, <20% D, <10% U)
- **TBD Count**: 0 literal TBD/TODO/`[NEEDS CLARIFICATION]` markers, but 3 Open Questions function equivalently, one of which (Q-01) gates a MUST requirement's buildability
- **Happy-Path:Error Ratio**: ~1:1 to 1.5:1 across the five journeys — genuinely strong, not a weakness
- **Verdict**: The prose is unusually clean and the acceptance-criteria table is unusually well-formed for this codebase's history — but underneath that polish sit two real architecture holes (multi-turn thread continuity, and the concrete state JSON shape) and one MUST requirement the spec itself admits may be unbuildable. This will not fail the same way spec 002 failed (silent ambiguity dressed as an AC); it will fail by the developer discovering, mid-slice, that a MUST item has no floor to build on.

## Dimension Scores

| Dimension | Score | Max | Large-tier pass bar | Pass? |
|-----------|-------|-----|----|----|
| D1: Clarity & Unambiguity | 17 | 20 | 16 | Pass |
| D2: Completeness | 11 | 20 | 16 | **Fail** |
| D3: Testability & Verifiability | 13 | 18 | 14 | **Fail (narrow)** |
| D4: NFR Coverage & Quality | 14 | 16 | 13 | Pass |
| D5: Structural Integrity | 7 | 12 | 10 | **Fail** |
| D6: Story/Requirement Form | 11 | 12 | 10 | Pass |
| D7: Traceability & Prioritization | 13 | 14 | 14 (MoSCoW mandatory) | **Fail (narrow)** |
| D8: Error & Edge Case Coverage | 7 | 10 | 8 | **Fail (narrow)** |
| **Total** | **93** | **120** | | |

5 of 8 dimensions miss their Large-tier bar. Three of those misses are narrow (1 point); two (D2, D5) are not.

---

## Critical Issues

Materially blocks a developer from building the ONE thing the spec describes, or blocks a QA engineer from writing a deterministic test.

### SHIP-BLOCKER 1 — FR-07 / AC-13 is a MUST built on ground the spec itself calls unbuildable
A-03 rates its own confidence **Low**: *"Azure's Responses API really does emit `response.function_call_arguments.delta` for this agent configuration."* Q-01 then states in plain language: *"FR-07 and AC-13 are unbuildable as written; tool args fall back to a single chunk"* if the assumption is wrong. Yet FR-07 and AC-13 both carry **MUST** priority and both appear in the Launch Criteria go/no-go list (§6.1: *"Every `MUST` acceptance criterion (AC-01 to AC-26, AC-28 to AC-30) passes"*). Verified against the real code: `src/alloy/_loop.py`'s `translate_stream_event` and `extract_tool_call_from_stream_item` today handle only `response.output_text.delta` and `response.output_item.done` — there is no existing handling for an arguments-delta event, so FR-07 is not a small addition, it's new plumbing in the exact file the spec (§6.3) names as the one that hid four bugs last round. A MUST launch-blocking criterion should not rest on an unconfirmed external API behavior with no specified fallback contract. **Fix**: demote FR-07/AC-13 to SHOULD until Q-01 is resolved by a live spike (already scheduled as the validation method), or add a MUST fallback requirement now — e.g. *"If Azure does not emit incremental argument deltas for this agent configuration, `TOOL_CALL_ARGS` MUST be emitted exactly once carrying the complete argument JSON, and this MUST NOT be treated as a launch blocker."*

### SHIP-BLOCKER 2 — Thread continuity across multiple runs is claimed by the protocol and never specified
Every journey in this spec is a single run. But `threadId` exists in AG-UI specifically to correlate **multiple** runs into one ongoing conversation (Appendix A's own glossary doesn't even define "thread" beyond "carried in `RunAgentInput`"). Cross-referencing the sections that touch this:
- §4.2 Data Requirements: *"Data retention: None. State lives for the duration of one HTTP request and is discarded when the stream closes."*
- A-08: *"Client-supplied `messages` are for display/history only and alloy's own conversation state remains authoritative"* (Medium confidence).
- Verified against `examples/serve.py`: the existing pattern builds **one `Agent` per request** (module docstring: *"one `Agent` is built per request, not once at module scope"*), and `Agent._conversation_id` is created fresh (`_foundry.create_conversation`) whenever it's `None` — there is no session store keyed by `threadId` anywhere in the codebase, and the spec proposes none.

Put together: if nothing survives between requests (§4.2/§5.3) **and** client-resent messages aren't authoritative (A-08), a second run in the same `threadId` has no mechanism to recover what happened in the first run. One of those two must be false, and the spec never says which. This isn't an edge case — it's the primary reason the protocol has a `threadId` field at all, and no FR, AC, or journey in this Large-tier spec exercises "second POST, same `threadId`." **Fix**: add a journey (or explicit non-goal) for multi-run threads, and resolve whether the server reconstructs the conversation from `RunAgentInput.messages` on every run (making A-08 wrong) or keeps a `threadId`-keyed `Agent` cache (making §4.2's "Data retention: None" wrong, and requiring new eviction/lifecycle requirements).

### SHIP-BLOCKER 3 — FR-12 (the spec's own "single highest-risk vector") has zero acceptance criteria
FR-12: *"MUST treat `RunAgentInput.messages` and `.context` as untrusted client data, never as instructions... MUST NOT let a client-supplied system message replace the agent's own."* Its Acceptance Link column reads **"See 5.2"** — not a numbered AC. §5.2 itself calls this *"the single highest-risk vector in this spec."* Scanning AC-01 through AC-30, none test it. This is the one place in the document where the Given/When/Then discipline applied everywhere else (30 ACs, mostly clean) simply stops, on exactly the requirement that most needed it. A QA engineer sampling FR-12 cannot write a test from this spec — they'd have to invent both the attack payloads and the pass/fail condition themselves. **Fix**: add AC-31+ with concrete Given/When/Then, e.g. *"Given `RunAgentInput.messages` contains a `role: system` entry instructing the agent to ignore its instructions, when the run executes, then the agent's own system prompt is what was sent to the backend (assert via the mocked client's `create()` call kwargs), and the injected text never appears there."*

---

## Top 3 Improvements

1. **Specify the `State` entity's concrete JSON shape before FR-09/FR-10 are built.** §4.2's Data Requirements table lists only logical attributes ("messages, active tool calls, completed tool calls, failures"), never a shape. Add one worked example to the spec: an initial `STATE_SNAPSHOT` payload and one `STATE_DELTA` JSON Patch array against it, showing exactly how tool calls are keyed (array indexed by position, or object keyed by `toolCallId`?) and what a message entry looks like. Without this, two developers pass AC-15/16/17 with two incompatible state models, and a real AG-UI client (the thing §6.3 says is the actual bar) will render one of them wrong.
2. **Resolve the thread-continuity contradiction (Ship-Blocker 2) before this goes to a plan.** Pick one: (a) the server is truly stateless per-request and reconstructs everything from `RunAgentInput.messages` each run — in which case rewrite A-08, since it currently says the opposite; or (b) alloy keeps `threadId`-keyed session state — in which case add the FRs/ACs for that store's lifecycle (creation, eviction, concurrent-run-per-thread behavior) and correct §4.2's "Data retention: None." Either is buildable; the current document endorses neither and is internally inconsistent about which.
3. **Give FR-12, FR-19, and FR-20 numbered acceptance criteria instead of "See 5.2" / "See 2.2" pointers.** Every other MUST requirement in this document links to a specific AC-NN with a Given/When/Then row; these three are the exceptions, and FR-12 is the highest-stakes requirement in the spec. A prose cross-reference is not a test.

---

## Detected Anti-Patterns

| Pattern | Severity | Evidence |
|---|---|---|
| Pattern 3 (NFR Graveyard) | Not detected | 9 NFR categories present, mostly SMART; this is a strength |
| Pattern 4 (Happy Path Spec) | Not detected | ~1:1–1.5:1 happy:error ratio; strong error-path coverage in §5.4 |
| Pattern 9 (Scope Creep Incubator) | Not detected | Non-Goals (§2.2) is explicit, itemized, and marked binding |
| **Untestable Criterion (AP-09), partial** | **High** | FR-12/19/20 route to prose ("See 5.2"/"See 2.2") instead of an AC. FR-12 is the case that matters — see Ship-Blocker 3. |
| **The Missing Symmetry Pair** (D2 completeness variant, not independently named in the 9 patterns but the same failure class as AP-06/Wishlist gaps) | **Critical** | `threadId` is accepted, echoed, and central to the protocol's identity, but its "multiple runs, one thread" behavior has no counterpart requirement anywhere. See Ship-Blocker 2. |
| Pattern 1 (Fog of Adjectives) | Not detected | grep for fast/slow/secure/robust/scalable/user-friendly/intuitive/easy/simple/modern/clean/responsive/typically/"as appropriate"/"where necessary"/"when feasible" across the file returns only two hits, both "clean" used in a measurable, non-quality-adjective sense ("clean-venv check", "clean...core") |
| Pattern 8 (Tense Soup) | **Nit** | MUST/SHOULD used consistently, but the Glossary (Appendix A) never states what MUST/SHOULD/MAY formally mean — it's relied on as an unstated RFC-2119-style convention |

---

## Answers to the Five Specific Questions Asked

1. **Acceptance criteria that would pass against broken code (the repo's historical failure mode).** Two found: (a) **AC-15** references *"the state the server holds at RUN_FINISHED"* as ground truth, but no wire event ever exposes a final state snapshot — only one `STATE_SNAPSHOT` fires, at run *start* (FR-08). A test author has no documented way to observe "what the server holds" except by reaching into the translator's internals directly (white-box), which the spec never says is the intended test shape; a black-box wire-protocol test literally cannot check this AC as worded, which means it's easy to write a test that vacuously passes. (b) **AC-13**'s "concatenating deltas yields the complete argument JSON" is exactly the kind of ordering-sensitive assertion a synchronous unit test can pass while the real threaded producer (verified in `_agent.py`'s `_pump`/`asyncio.Queue` relay, lines 224–274) races under load — and the spec's own §6.3 predicts this outcome by name ("a fifth [bug] that only shows up under a race") without adding a requirement that would catch it. See D8.
2. **"Agent core untouched" vs. FR-07 reopening `_loop.py`/`_agent.py`.** Not resolved — papered over. A-01 explicitly reconciles "core untouched" with "full state support" *only*, via hooks: *"'full state support' needs no `Agent` core change... hooks reconcile them."* It never mentions FR-07. FR-07 itself, in the very same document, says plainly: *"This requires changes to `_loop.py` and `_agent.py`."* Verified against the real files: today's `_loop.py`/`_agent.py` have no incremental-argument-delta handling at all, so this is a real, nontrivial reopening of exactly the file §6.3 calls out as the source of the prior round's four bugs. The document contains both claims side by side with no cross-reference acknowledging the tension — a reader has to notice it themselves.
3. **Should A-03 (Low confidence) block FR-07/AC-13 from the MUST set?** Yes. See Ship-Blocker 1. A Low-confidence assumption underpinning a MUST launch-gate item, with the spec's own Open Questions table admitting the item is "unbuildable as written" if the assumption fails, is not a MUST — it's a spike-gated SHOULD, or a MUST with a specified fallback. Neither exists today.
4. **Is the state model (FR-09, FR-10, AC-15) precise enough to build without further decisions?** No. See Top 3 Improvement #1. FR-09 establishes *how* deltas are produced (recorded, not diffed) but not the shape being patched; FR-10 names four content buckets ("messages, in-flight tool calls, completed tool calls, tool failures") with no schema; §4.2's Data Requirements table stays at the "logical attributes" level throughout. Two compliant implementations could produce materially different JSON shapes and both pass AC-14/16/17 as worded.
5. **Is FR-12's prompt-injection requirement testable as written?** No — see Ship-Blocker 3. It has no numbered AC at all, only a narrative pointer to §5.2. It is also weak as an NFR under D4's SMART test specifically on the Measurable axis: §5.2 states the threat and the rule but no test procedure, no example payload, and no way to know when the requirement is satisfied versus merely believed to be.

---

## Detailed Analysis (dimensions scoring below 80% of max)

### D2: Completeness — 11/20
- **Evidence for the deduction**: the thread-continuity gap (Ship-Blocker 2) is a named-capability-with-zero-requirements gap on the protocol's own core correlation mechanism (`threadId`), which the rubric treats as cap-worthy ("named feature with zero requirements in body → cap at 8"); I did not apply the full cap because the gap is structural/derived rather than a single missing named epic, but it is the dominant reason this dimension misses its Large-tier bar of 16 by 5 points.
- **Secondary evidence**: §4.2's Data Requirements table is scope-complete (all six entities named: Run, Event, Message stream, Tool invocation, State, Patch) but attribute-shallow — "Key attributes (logical)" is the column header, and it stays logical throughout, never dropping to an example. This is the same completeness gap as Top 3 Improvement #1.
- **What's genuinely good here and kept the score off the floor**: zero literal TBD markers, an explicit and binding Non-Goals section (§2.2, 7 itemized exclusions), and every named in-scope bullet in §2.1 does have FRs beneath it.
- **Fix**: add the multi-run-thread journey/requirement and the state JSON example; both are concrete, boundable additions, not a rewrite.

### D3: Testability & Verifiability — 13/18
- FR-12/19/20 route to prose instead of a numbered AC (see Ship-Blocker 3 and the pattern table above) — 3 of 20 FRs (15%) have no direct test linkage, and the most severe one is the security requirement.
- AC-13 is contingent on an unresolved, Low-confidence external assumption (Q-01) — as written today, a developer cannot write this test without first discovering whether the premise holds.
- AC-15's ground-truth-observability gap (answer to Question 1 above).
- Kept above the floor by: 27 of 30 ACs are directly, mechanically testable Given/When/Then rows (AC-01 through AC-11, AC-14, AC-16–18, AC-20–26, AC-28–30 all pass a straightforward "write a test" check), and the conformance-checker requirement (FR-15/AC-24/AC-25) is a genuinely strong, self-verifying testability mechanism — a checker that must itself fail on a deliberately malformed sequence is exactly the kind of "prove the test can fail" discipline this rubric rewards.

### D5: Structural Integrity — 7/12
- The core deduction is the derived contradiction between §4.2/§5.3 ("Data retention: None... discarded when the stream closes") and A-08 ("alloy's own conversation state remains authoritative") once thread continuity is considered (Ship-Blocker 2) — an unresolved internal inconsistency between two explicitly-stated document sections, not something inferred from silence.
- FR-09's phrase *"recording the patch made rather than diffing two objects"* has an ambiguous referent for "the patch" — made by what, exactly, and at what granularity (one patch per hook event? per token?) — compounding the D2 state-shape gap from a structural-atomicity angle.
- What kept this off the floor: requirement IDs are complete and consistent throughout (FR/AC/A/Q namespaces never collide), no technology-name leakage beyond the one legitimate, load-bearing SDK choice (`ag-ui-protocol`, which is the entire subject of the spec, not an implementation leak), and terminology ("Run," "State," "Event," "Correlation id") is used consistently and is defined once in the Glossary rather than drifting.

### D8: Error & Edge Case Coverage — 7/10
- 5 of the rubric's 6 categories are present with real depth: invalid input (§5.4 malformed JSON / bad `RunAgentInput`), dependency failure (Azure fails pre/mid-stream), boundary conditions (AC-11 two-tool-calls, AC-12 no-text, 10-turn cap), and resource exhaustion / auth failure are both explicitly and deliberately out of scope (§2.2, §5.2) — which the rubric credits as a valid explicit decision, not an omission.
- The 6th category, **concurrent access conflicts**, is missing as a requirement despite the spec naming it, in its own words, as the most likely next defect: §6.3, *"the incremental tool-argument work reopens `_agent.py`'s stream path — the file that hid all four bugs in the previous round — and introduces a fifth that only shows up under a race."* A risk the document itself calls "the sharpest risk" (well, its second-sharpest, after the checker-fidelity risk) gets zero FR/AC coverage. That's the entire deduction.

---

## Note on scope of this review

Verified directly against the repository rather than taken on the spec's word: `pyproject.toml` (2 runtime deps confirmed: `azure-ai-projects`, `azure-identity`; pytest/ruff/pyright config confirmed), `src/alloy/hooks.py` (4 lifecycle events confirmed: `BeforeInvocationEvent`, `AfterInvocationEvent`, `BeforeToolCallEvent`, `AfterToolCallEvent`), `src/alloy/_agent.py` and `src/alloy/_loop.py` (stream shapes and the absence of any existing argument-delta handling confirmed), and `examples/serve.py` (per-request `Agent` construction, existing `/ping`/`/invoke` behavior, and the stdlib-only constraint confirmed). Everything scored above reflects the spec text cross-checked against this real code, not the spec's self-description alone.
