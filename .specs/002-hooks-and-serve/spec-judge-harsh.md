# Spec Evaluation Report: `alloy` 002 — Lifecycle hooks and a runnable `/invoke` server

## Summary
- **Total Score**: 91/120 (76%)
- **Grade**: C — Adequate spec, known gaps, medium risk
- **Size Tier**: Medium (self-declared in header, confirmed: 174 lines, 1 role, 2 integration surfaces — fits the 100–250 line target)
- **Spec Format**: Hybrid — Given/When/Then acceptance criteria (AC-NN), bug-report-style fix descriptions (FR-BN), declarative NFR statements (NFR-NN). Consistently applied — not a defect per the Hybrid-format note.
- **Buildability Ratio**: B:D:U = 32:4:0 (89%:11%:0%) — looks "Good" by the raw ratio, but the 4 [D] items are the spec's highest-leverage, highest-risk requirements (cross-path hook parity, tool cancellation, invocation-boundary timing, the "unmodified tests" guarantee), not filler. The ratio flatters this spec's real risk.
- **TBD Count**: 0
- **Happy-Path:Error Ratio**: ~4:1 to 5:1 (borderline-acceptable)
- **Verdict**: Individually, every acceptance criterion is crisp and testable — this is a well-written spec at the sentence level. But it asserts three specific, hard engineering claims (AC-17 cross-path event parity, AC-12/13 tool cancellation, AC-05 "unmodified" test parity) without giving the implementer the one design fact each actually depends on, and it is silent on a real circular-import hazard and on the server's security posture. A developer can start building from this immediately; three separate places in the build will require a decision the spec should have made, and at least one of those (AC-17) is a trap that looks solved but isn't unless the implementer independently notices the seam.

## Dimension Scores

| Dimension | Score | Max | Notes |
|-----------|-------|-----|-------|
| D1: Clarity & Unambiguity | 17 | 20 | No vague adjectives, no "and/or", consistent Given/When/Then. Docked for "byte-for-byte" (AC-05) as a loose metaphor and two unspecified output/comparison shapes (AC-12, AC-17). |
| D2: Completeness | 17 | 20 | Strong explicit Non-Goals section, zero TBDs, both features fully covered by ACs. Minor: no statement on hook removal/unregistration. |
| D3: Testability & Verifiability | 17 | 18 | Every AC is Given/When/Then observable-behavior. Docked for AC-12's unspecified wire format and AC-17's unspecified comparison granularity. |
| D4: NFR Coverage & Quality | 6 | 16 | Zero-score trigger applies: a network-facing server that accepts user prompts with no security NFR/trust-boundary statement → capped at 6. See Detailed Analysis. |
| D5: Structural Integrity | 9 | 12 | Atomic, IDs present, no textual contradictions. Docked for the AC-05/FR-B1/FR-B2 unresolved tension and the run_calls signature-change gap it implies. |
| D6: Story/Requirement Form | 11 | 12 | Consistent Given/When/Then form throughout, actors mostly clear. |
| D7: Traceability & Prioritization | 7 | 14 | IDs present; goals (§1) traceable to ACs. No priority markers, no MoSCoW, no MVP cut line across 29 ACs — everything reads as equally mandatory. |
| D8: Error & Edge Case Coverage | 7 | 10 | Invalid-input and server-concurrency well covered. Missing: backend/dependency-failure contract through the new server, multi-hook mutation ordering, empty-prompt edge case. |
| **Total** | **91** | **120** | |

## Critical Issues

These materially block a developer from building the ONE thing the spec describes, or block a QA engineer from writing one deterministic test.

1. **AC-17 (cross-path hook parity) is achievable only via one unstated implementation choice, and the natural reading of AC-08 points at the wrong one.** See Detailed Analysis §Risk-1.
2. **AC-12/AC-13 (tool cancellation) has no seam in the existing `run_calls`, and the spec doesn't specify the new signature, the cancel/unknown-tool precedence, or the cancelled `ToolResult.output` wire format.** See Detailed Analysis §Risk-3.
3. **AC-05's "all 49 existing tests still pass, unmodified" is asserted without checking it against the spec's own FR-B1/FR-B2 fixes or the `run_calls` signature change AC-08/09 require.** See Detailed Analysis §Risk-6.
4. **D4 zero-score trigger fires**: the `/invoke` server accepts arbitrary user-submitted prompts over a real network socket with zero authentication (an explicit Non-Goal), and the spec never states the resulting operational constraint (what interface it binds, and that it must not be exposed beyond a trusted network). "We chose not to build auth" and "we told the user not to expose this unauthenticated" are two different sentences; only the first exists.

## Top 3 Improvements

1. **State explicitly where hook firing happens in `stream_async`.** Add one sentence to §4 or §8 (Assumptions): "`BeforeToolCallEvent`/`AfterToolCallEvent` fire from inside (or immediately around) the shared `run_calls` call — the same call site used by all three paths — never at the point `stream_async` discovers a tool call mid-stream via `response.output_item.done`." Without this, AC-08's literal wording ("before the tool runs") combined with `stream_async`'s existing `yield {"current_tool_use": call}` at discovery time (`_agent.py:269-272`) is the natural place a developer reaches for first — and it produces a *different* event ordering (all Befores batched at stream-discovery time, Afters batched later at execution time) than the sync paths produce (Before/After alternating per call, at execution time). AC-17's own comparison test is what would catch this, but only after the wrong thing is built.

2. **Give `run_calls` its new signature and precedence rules, in the spec, not just in the code.** State: (a) the new parameters (`agent`, `hooks: HookRegistry`, presumably keyword-only with defaults so existing direct callers of `run_calls` in the test suite keep compiling — this is also what AC-05 needs); (b) where in the per-call loop the cancel-check sits relative to the existing unknown-tool lookup and argument-decode/validate steps; (c) what happens when a hook cancels a call for an unknown tool (does `AC-12`'s cancellation or `AC-11`'s `UnknownToolError` win?); (d) the exact `ToolResult.output` shape for a cancellation — a bare reason string, or `json.dumps({"cancelled": reason})` to match the existing `_failure()` convention of `json.dumps({"error": str(error)})`. Four separate implementers would produce four different, individually-defensible answers to these from the current text.

3. **Verify AC-05 against the changes the spec itself introduces, and say so.** One sentence each: "None of the 49 existing tests exercise `@tool(name=...)` with a distinct override, so FR-B1's fix changes no existing assertion" and "None of the 49 existing tests exercise a non-JSON-serializable tool return value, so FR-B2's fix changes no existing assertion" — or, if either is untrue, resolve the conflict now rather than at test-run time. Also state that `run_calls`'s new parameters must be optional so any test that calls `run_calls(calls, tools)` directly (2 positional args) keeps working unmodified.

## Detected Anti-Patterns

| Pattern | Severity | Evidence |
|---|---|---|
| AP-06 The NFR Graveyard | Medium | §5's 5 NFRs cover no-op-hook cost, `pyright`/`ruff` gates, and two design-rule invariants — none is a Security or general Reliability NFR, and the spec ships a new network-facing HTTP server. Not a graveyard (the section is substantive), but a real gap in the *categories* it covers. |
| AP-02 Implementation Leak | Not present | §3's tech exclusions ("Not FastAPI, not uvicorn, not starlette") are stated as constraints justifying a no-new-dependency policy, not accidental leaks — the rubric's own exception for legitimate constraints applies. |
| AP-13 Scope Creep Incubator | Not present | Non-Goals section (§3) is thorough, explicit, and placed early. |
| AP-01/03/04/05/09 | Not present | No vague adjectives, no unprioritized wishlist, no happy-path-only journeys, no passive-voice actor loss, no intent-only ACs. |

## Hostile-Risk Findings (as specifically requested)

### Risk 1 — AC-17 cross-path event parity: ACHIEVABLE, but only via an unstated seam
`run_calls` (`_loop.py:43`) is already the single function all three call paths (`__call__` at `_agent.py:138`, `invoke_async` at `_agent.py:179`, `stream_async` at `_agent.py:286`) route their tool execution through, receiving the SAME already-assembled list of `ToolCall`s regardless of path. If hook-firing for `BeforeToolCallEvent`/`AfterToolCallEvent` is added *inside* (or immediately wrapping) that one shared call, cross-path identical ordering follows by construction — free, no divergence possible.

The trap: `stream_async` discovers each tool call *during* the SSE stream, one at a time, via `extract_tool_call_from_stream_item` on `response.output_item.done` (`_loop.py:97-104`), and immediately does `pending_calls.append(call); yield {"current_tool_use": call}` (`_agent.py:270-272`) — *before* the turn's stream has finished and *before* `run_calls` is ever invoked for that turn. A developer reading AC-08 ("`BeforeToolCallEvent` fires once per tool call, before the tool runs") in isolation has no reason not to fire the event right there, at discovery. That produces `Before(call1), Before(call2), ... [stream ends] ... After(call1), After(call2), ...` — batched Befores, batched Afters — which is a *different* sequence of event *types* than the sync paths' `Before(call1), After(call1), Before(call2), After(call2)` (since `run_calls`'s `for call in calls:` loop executes and would naturally bracket each call individually). AC-17's own comparison test is the only thing that would catch this, and only after the wrong architecture is built.

**Verdict: achievable, not handled as written.** The spec should name the seam (§4 or §8), not leave it to be inferred from the shared-function structure.

### Risk 2 — NFR-04 (exactly two `async def`s): HANDLED
`test_b30_exactly_two_async_defs_in_package` (`test_design_rules.py:60-67`) scans only `src/alloy/*.py`. §3 explicitly keeps the HTTP server out of `src/alloy/` ("The HTTP server does **not** enter `src/alloy/`"), and hook callbacks are explicitly sync ("No async hook callbacks... on all three paths," §3). `examples/serve.py` is free to add its own `async def` bridge (needed to drain `agent.stream_async`'s async generator from a sync `http.server` handler thread) without touching the count. Nothing in this spec forces a third `async def` inside the package. This is the one hostile risk the spec gets unambiguously right, and it's right specifically because of a decision (server stays out of `src/alloy/`) the spec made deliberately — worth crediting.

### Risk 3 — AC-12/AC-13 tool cancellation: the seam is ABSENT, must be built, and the spec underspecifies it
`run_calls`'s current signature is `run_calls(calls: Sequence[ToolCall], tools: Mapping[str, ToolSpec]) -> list[ToolResult]` — no `agent`, no `hooks` parameter. To fire `BeforeToolCallEvent` (which needs `agent`, per AC-01/06/08) and to honor a cancellation, this signature must change, and a new early-exit branch must be added to the per-call loop, ahead of the existing unknown-tool lookup (`tools.get(call.name)`), argument-decode (`decode_arguments`), and required-argument-check — since AC-11 requires `BeforeToolCallEvent` to fire even for unknown tools, the hook call is unconditional and comes first.

Three concrete gaps the spec leaves for the implementer to invent:
- **Signature**: does `run_calls` gain required or optional (defaulted) `agent`/`hooks` params? This directly determines whether AC-05's "unmodified" tests survive (see Risk 6).
- **Precedence**: if a hook sets `cancel_tool` on a call for a tool name that doesn't exist, does the `ToolResult` reflect the cancellation (`failure=None`) or `UnknownToolError` (`failure=exc`)? AC-11 and AC-12 both claim the event fires but don't say which outcome wins when both conditions are true on the same call.
- **Wire format**: AC-12 says the `ToolResult` "carries the reason string" — as `output` verbatim, or JSON-wrapped like the existing `_failure()` helper's `json.dumps({"error": str(error)})` (`_loop.py:79-82`)? The model on the other end sees whatever is chosen; the spec doesn't say which.

**Verdict: the seam is not present in the current code and is architecturally straightforward to add, but the spec leaves 3 implementation-choice gaps that materially affect what a QA engineer would assert in a test.**

### Risk 4 — Circular import (`agent: Agent` on hook events): UNACKNOWLEDGED
Every hook event needs an `agent: Agent` field (AC-01, 06-09). `_agent.py` will need to import event/registry types from the new `hooks.py` to construct and fire them; `hooks.py` needs the `Agent` type to annotate its event dataclasses. That's a textbook circular import. The whole package already uses `from __future__ import annotations` (confirmed at the top of `_agent.py`, `_loop.py`, `contracts.py`, `_schema.py`), so the standard fix — a `TYPE_CHECKING`-guarded import of `Agent` in `hooks.py`, with `pyright --strict` (NFR-02) still resolving it statically — is available and idiomatic for this codebase. But the spec never mentions it, in a document that otherwise cites exact line numbers for two other bugs (FR-B1 at `_schema.py:157`/`_agent.py:65`, FR-B2 at `_loop.py:74`). A developer who imports `Agent` at module level in `hooks.py` the same way every other module-level import in this codebase is written will hit `ImportError: cannot import name 'Agent' from partially initialized module` the first time `_agent.py` imports from `hooks.py`.

**Verdict: real, standard-fix-available, but genuinely unaddressed — a notable omission given the spec's demonstrated attention to this exact kind of codebase detail elsewhere.**

### Risk 5 — AC-26 ("tested without binding a socket"): ACHIEVABLE, and the spec phrases it correctly
`http.server.BaseHTTPRequestHandler` subclasses are normally constructed by the socket server machinery with a live socket, and `do_GET`/`do_POST` read directly from `self.rfile`/write to `self.wfile` — testing that in isolation without a real connection is awkward. But AC-26 doesn't mandate *how* — it states the *outcome* ("the handler logic is a plain function tested directly"), which correctly pushes the implementer toward extracting request-handling (parse JSON → route → build response) into a plain, socket-free function that `do_GET`/`do_POST` call as a thin adapter. This is a standard, well-known pattern and fully compatible with stdlib `http.server`.

**Verdict: satisfiable, and this AC is a good example of the spec specifying observable behavior instead of over-specifying implementation** — the one soft gap is that the spec never states that AC-19–25 (which need real HTTP semantics like `Content-Type: text/event-stream`) are presumably tested at a different level (a live, possibly-ephemeral-port integration test) than AC-26's unit-level test — inferable, but unstated. NIT, not a blocker.

### Risk 6 — AC-05 ("all 49 existing tests still pass, unmodified") vs FR-B1/FR-B2 and the `run_calls` signature change: TENSION, NOT RESOLVED
Two separate ways this guarantee could break, neither of which the spec rules out:
- **FR-B1/FR-B2 change observable behavior.** FR-B1 makes a renamed tool (`@tool(name="x")`) reachable under its *new* name instead of the original (currently-buggy) name; FR-B2 makes a tool returning a non-JSON-serializable value produce a `ToolResult.failure` instead of raising a bare `TypeError`. If any of the 49 existing tests currently asserts the *old* (buggy) behavior on either path, fixing it breaks that test — which the spec's own §7 language ("verified against source, not taken on report") suggests is unlikely but never confirms.
- **`run_calls`'s signature must change** to support hook firing (Risk 3). If any of the 49 existing tests calls `run_calls(calls, tools)` directly with exactly two positional arguments (plausible, since it's a deliberately pure, directly-testable function), adding required new parameters breaks that call site regardless of whether the *behavior* changed.

**Verdict: plausible-but-unverified tension.** The spec asserts a strong, falsifiable claim ("unmodified") about code it also modifies, without doing the one check (grep the 49 tests for `@tool(name=`, a non-serializable tool return, or a direct `run_calls(` call) that would confirm or refute it.

## Buildability Scan (First Pass)

| # | Item | Class | Note |
|---|---|---|---|
| AC-01–04, 07–11, 13–16 | Registry mechanics, event carriage, mutation (minus 12) | B | Clear, single interpretation each. |
| AC-05 | "49 existing tests pass, unmodified" | D | See Risk 6 — asserted, not verified against FR-B1/FR-B2/signature change. |
| AC-06 | `BeforeInvocationEvent` fires before any backend call | D | `__call__` (`_agent.py:95-155`) and `_prepare_call` (used by `invoke_async`/`stream_async`, `_agent.py:305-326`) are two **separately maintained** blocks of near-identical setup logic — the hook-firing point must be added to both, and the spec doesn't flag this existing duplication as a place a fix can silently miss one path. |
| AC-12 | Tool cancellation | D | See Risk 3 — signature, precedence, wire format all unspecified. |
| AC-17 | Cross-path event-sequence parity | D | See Risk 1 — achievable only via one unstated design choice. |
| AC-18–29 | Server, docs | B | Well-specified, behavior-first (see Risk 5). |
| FR-B1, FR-B2 | Bug fixes | B | Precise, line-cited, testable — but see Risk 6 for their interaction with AC-05. |
| NFR-01–05 | Non-functional | B | Concrete, mechanism-level criteria (not vague adjectives). |

**Ratio: 32 B : 4 D : 0 U (89% : 11% : 0%).**

## Detailed Analysis (dimensions below 80% of max)

### D4: NFR Coverage & Quality — 6/16
The minimum coverage set for Medium tier is Performance, Security, Reliability, Error handling. This spec's 5 NFRs map as: NFR-01 (perf-adjacent, narrow — cost of the unregistered-hooks path only, not the server's latency/throughput), NFR-02/03 (code-quality gates: `pyright`, `ruff` — not one of the 4 categories), NFR-04/05 (architectural invariants — design-rule tests). **Zero of the 5 is a Security NFR**, despite this spec adding a real HTTP server that accepts arbitrary user-submitted prompts on a real socket, explicitly with no auth (§3 Non-Goals). The rubric's own trigger — "System handles user data or auth but no security NFR → cap at 6" — applies: this system now handles externally-submitted user input over a network with a stated absence of auth, and nowhere does the spec add the corresponding NFR ("this example must bind to localhost only" / "must not be run without a reverse proxy" / any trust-boundary statement at all). Reliability is similarly thin: AC-23 covers one specific failure mode (malformed JSON) but there's no general statement about the server's behavior on backend/dependency failure (see Risk-adjacent gap below).
**Fix**: add one NFR stating the server's binding interface and the operational constraint that follows from having no auth (e.g., "NFR-06: the example binds to `127.0.0.1` only, or the README states in bold that it must not be exposed beyond a trusted network").

### D7: Traceability & Prioritization — 7/14
IDs are present throughout and every AC traceably serves one of the two stated goals in §1 ("Why"). But there is **no priority marker of any kind** — no MoSCoW, no P1/P2/P3, no MUST/SHOULD/MAY distinguishing ACs — across all 29 acceptance criteria, and no MVP cut line. Everything reads as equally mandatory. For a solo-maintainer (§6) Medium-tier spec this may be an intentional simplification (ship all 29 or don't ship), but the spec never says that — it's silent rather than deliberate-and-stated.
**Fix**: one line — "All 29 ACs are required for this spec to close; there is no partial-ship cut line" — turns an omission into a stated decision, which is all D7 actually needs at this tier.

### D8: Error & Edge Case Coverage — 7/10
Strong on invalid client input (AC-11, 23, 24) and server concurrency (AC-25). Three real gaps, none acknowledged:
1. **Backend/dependency failure through the new server** — if the Foundry backend errors mid-request (a `BackendAuthError`, a timeout, an outage), what does `/invoke` return? AC-23 sets a precedent (400 + JSON error body, no stack trace, server survives) for malformed *client* input but says nothing about backend-originated failures, which are arguably more likely in practice.
2. **Multi-hook mutation ordering** — AC-14/15 are phrased for "a hook" (singular) rewriting `event.tool_use`/`event.result`. With two or more hooks registered for the same event (which AC-02/03 explicitly support), does the second hook see the first hook's rewrite, or the original? Unaddressed.
3. **Empty-string prompt** — `{"prompt": ""}` is neither "no `prompt` key" (AC-24, → 400) nor a normal prompt (AC-20). Unaddressed.

## Note on scope of this review
This review evaluated buildability against the actual shipped code (`_agent.py`, `_loop.py`, `contracts.py`, `_schema.py`, `test_design_rules.py`) as instructed. It did not have access to the other 49 existing tests referenced by AC-05 (only `test_design_rules.py` was in scope), which is precisely the gap the Risk-6 finding is about — this reviewer cannot independently confirm or refute AC-05's claim either, for the same reason the spec doesn't.
