"""Azure adapter: hops the sync client onto a worker thread for async callers.

Owns the only `asyncio.to_thread` call in the package (see contract). A non-streaming
call is a single hop; a streaming call runs the whole blocking iteration on one thread and
relays events back to the event loop through an `asyncio.Queue`, so the caller gets each
event as it arrives instead of waiting for the full response.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator
from typing import Any

from .contracts import AlloyError, StreamingUnsupportedError

_DONE = object()


async def create_completion(client: Any, **create_kwargs: Any) -> Any:
    """Run one non-streaming `client.chat.completions.create` call off the event loop."""
    return await asyncio.to_thread(client.chat.completions.create, **create_kwargs)


async def stream_completion(client: Any, **create_kwargs: Any) -> AsyncGenerator[Any, None]:
    """Run one streaming `client.chat.completions.create(stream=True)` call on a worker thread.

    Yields each raw SDK event as it arrives. A client whose `create` does not accept
    `stream` raises `StreamingUnsupportedError`
    (see B19). An SSE `error` frame can arrive either as an event with `type == "error"`
    or as a raised exception from inside the SDK's own stream iteration (the dual error
    path called out in the streaming reference doc) — both are forwarded to the caller.
    Abandoning iteration (breaking, or closing the generator) stops the worker thread
    before cleanup completes (see B26).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    stop_requested = threading.Event()

    def _pump() -> None:
        try:
            raw_stream = client.chat.completions.create(**create_kwargs, stream=True)
        except TypeError as exc:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                StreamingUnsupportedError(f"backend does not support streaming: {exc}"),
            )
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)
            return
        try:
            for raw_event in raw_stream:
                if stop_requested.is_set():
                    return
                if getattr(raw_event, "type", None) == "error":
                    message = getattr(raw_event, "message", "stream error")
                    loop.call_soon_threadsafe(queue.put_nowait, AlloyError(message))
                    return
                loop.call_soon_threadsafe(queue.put_nowait, raw_event)
        except Exception as exc:  # the dual error path: an SSE error raised as an exception
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    pump_task = asyncio.ensure_future(asyncio.to_thread(_pump))
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop_requested.set()
        await pump_task
