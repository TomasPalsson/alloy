"""Slice 7: a conversation survives past one message.

An AG-UI `threadId` and an Azure Foundry conversation are the same concept — server-held
history addressed by an id the client supplies — so alloy maps one onto the other rather
than storing transcripts itself. Behaviours B30-B34 and B48.
"""

from __future__ import annotations

import pytest

from alloy import Agent
from alloy.agui import ThreadStore


def test_b30_unseen_thread_id_has_no_conversation_yet() -> None:
    store = ThreadStore()
    assert store.resolve("thread-unseen") is None


def test_b30_remember_then_resolve_returns_the_conversation_id() -> None:
    store = ThreadStore()
    store.remember("thread-a", "conv-a")
    assert store.resolve("thread-a") == "conv-a"


def test_b31_second_run_on_a_thread_resolves_to_the_same_conversation() -> None:
    store = ThreadStore()
    store.remember("thread-a", "conv-a")
    # A second run resolves rather than minting, which is the whole point: without this an
    # agent starts a fresh conversation every message and forgets everything.
    assert store.resolve("thread-a") == "conv-a"
    assert store.resolve("thread-a") == "conv-a"


def test_b32_two_threads_never_read_each_others_conversations() -> None:
    store = ThreadStore()
    store.remember("thread-a", "conv-a")
    store.remember("thread-b", "conv-b")
    assert store.resolve("thread-a") == "conv-a"
    assert store.resolve("thread-b") == "conv-b"
    assert store.resolve("thread-a") != store.resolve("thread-b")


def test_b33_store_evicts_least_recently_used_past_its_cap() -> None:
    store = ThreadStore(max_threads=3)
    store.remember("t1", "c1")
    store.remember("t2", "c2")
    store.remember("t3", "c3")
    # Touch t1 so t2 becomes the least recently used.
    assert store.resolve("t1") == "c1"
    store.remember("t4", "c4")

    assert store.resolve("t2") is None, "t2 was least recently used and should be evicted"
    assert store.resolve("t1") == "c1"
    assert store.resolve("t3") == "c3"
    assert store.resolve("t4") == "c4"


def test_b33_eviction_surfaces_no_error_and_the_new_thread_works() -> None:
    store = ThreadStore(max_threads=1)
    store.remember("t1", "c1")
    store.remember("t2", "c2")  # must not raise
    assert store.resolve("t1") is None
    assert store.resolve("t2") == "c2"


def test_b34_forgetting_a_stale_mapping_leaves_the_thread_resolvable_again() -> None:
    store = ThreadStore()
    store.remember("thread-a", "conv-stale")
    store.forget("thread-a")
    assert store.resolve("thread-a") is None
    # The caller then mints a fresh conversation and records it; the run completes rather
    # than failing, which is AC-40.
    store.remember("thread-a", "conv-fresh")
    assert store.resolve("thread-a") == "conv-fresh"


def test_b34_forgetting_an_unknown_thread_is_silent() -> None:
    store = ThreadStore()
    store.forget("never-seen")  # must not raise


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_b48_blank_thread_id_is_rejected_rather_than_shared(blank: str) -> None:
    # Taken from the operator's own production catalogue: a session key that collapsed to a
    # constant put every user in one shared session. A blank thread id would do the same,
    # so it is a rejection rather than a default.
    store = ThreadStore()
    with pytest.raises(ValueError, match="thread id must not be blank"):
        store.remember(blank, "conv-a")
    with pytest.raises(ValueError, match="thread id must not be blank"):
        store.resolve(blank)


def test_b48_two_blank_thread_ids_cannot_collapse_into_one_conversation() -> None:
    store = ThreadStore()
    with pytest.raises(ValueError, match="thread id must not be blank"):
        store.remember("", "conv-one")
    with pytest.raises(ValueError, match="thread id must not be blank"):
        store.remember("", "conv-two")
    assert len(store) == 0


def test_agent_accepts_a_conversation_id_at_construction() -> None:
    # FR-22: without this an Agent always mints a fresh conversation and a thread can never
    # be resumed. Keyword-only, defaulting to today's behaviour.
    agent = Agent(model="gpt-5-mini", conversation_id="conv-existing", client=object())
    assert agent.conversation_id == "conv-existing"


def test_agent_without_a_conversation_id_has_none_until_it_runs() -> None:
    agent = Agent(model="gpt-5-mini", client=object())
    assert agent.conversation_id is None


def test_conversation_id_is_keyword_only_so_call_sites_stay_readable() -> None:
    with pytest.raises(TypeError):
        Agent("gpt-5-mini", "conv-x")  # type: ignore[misc]


def test_store_reports_its_size_for_eviction_tests_and_operators() -> None:
    store = ThreadStore()
    assert len(store) == 0
    store.remember("t1", "c1")
    store.remember("t2", "c2")
    assert len(store) == 2


def test_remembering_a_thread_twice_replaces_rather_than_duplicates() -> None:
    store = ThreadStore()
    store.remember("t1", "c1")
    store.remember("t1", "c2")
    assert store.resolve("t1") == "c2"
    assert len(store) == 1
