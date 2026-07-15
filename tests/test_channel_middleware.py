from __future__ import annotations

import asyncio
from dataclasses import asdict
from threading import Event, Timer
import time

import pytest

from imcodex.models import InboundAttachment, InboundMessage, OutboundMessage
from imcodex.store import ConversationStore


class StubService:
    def __init__(
        self,
        outbound: list[OutboundMessage] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.outbound = outbound or []
        self.error = error
        self.seen: list[InboundMessage] = []

    async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
        self.seen.append(message)
        if self.error is not None:
            raise self.error
        return list(self.outbound)


class CapturingAdapter:
    channel_id = "qq"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send_message(self, message: OutboundMessage) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_channel_middleware_dispatches_to_service_and_sets_reply_metadata() -> None:
    from imcodex.channels.middleware import (
        GENERIC_USER_ERROR_TEXT,
        UnifiedChannelMiddleware,
    )

    service = StubService(
        outbound=[
            OutboundMessage(
                channel_id="qq",
                conversation_id="group:group-1",
                message_type="turn_result",
                text="Done",
            )
        ]
    )
    adapter = CapturingAdapter()
    middleware = UnifiedChannelMiddleware(service=service)

    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="group:group-1",
        user_id="user-1",
        message_id="msg-1",
        text="inspect repo",
    )

    await middleware.handle_inbound(adapter, inbound, reply_to_message_id="msg-1")

    assert service.seen == [inbound]
    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "Done"
    assert adapter.sent[0].metadata["reply_to_message_id"] == "msg-1"
    assert adapter.sent[0].text != GENERIC_USER_ERROR_TEXT


@pytest.mark.asyncio
async def test_channel_middleware_emits_correlated_message_trace_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imcodex.channels.middleware import UnifiedChannelMiddleware

    observed_events: list[dict] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    monkeypatch.setattr("imcodex.channels.middleware.emit_event", capture_event)

    service = StubService(
        outbound=[
            OutboundMessage(
                channel_id="qq",
                conversation_id="group:group-1",
                message_type="turn_result",
                text="Done",
            )
        ]
    )
    adapter = CapturingAdapter()
    middleware = UnifiedChannelMiddleware(service=service)
    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="group:group-1",
        user_id="user-1",
        message_id="msg-1",
        text="inspect repo",
    )

    await middleware.handle_inbound(adapter, inbound, reply_to_message_id="msg-1")

    assert inbound.trace_id is not None
    assert adapter.sent[0].metadata["trace_id"] == inbound.trace_id
    assert [event["event"] for event in observed_events] == [
        "message.inbound.received",
        "message.outbound.sending",
        "message.outbound.sent",
    ]
    assert all(event["trace_id"] == inbound.trace_id for event in observed_events)
    assert observed_events[0]["data"]["text_preview"] == "inspect repo"
    assert observed_events[1]["data"]["text_preview"] == "Done"


@pytest.mark.asyncio
async def test_channel_middleware_traces_attachment_metadata_without_local_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imcodex.channels.middleware import UnifiedChannelMiddleware

    observed_events: list[dict] = []
    monkeypatch.setattr(
        "imcodex.channels.middleware.emit_event",
        lambda **payload: observed_events.append(payload),
    )
    service = StubService()
    adapter = CapturingAdapter()
    middleware = UnifiedChannelMiddleware(service=service)
    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="c2c:user-1",
        user_id="user-1",
        message_id="msg-1",
        text="",
        attachments=(
            InboundAttachment(
                kind="image",
                content_type="image/png",
                local_path="/private/secret/inbound.png",
                size_bytes=123,
            ),
        ),
    )

    await middleware.handle_inbound(adapter, inbound)

    data = observed_events[0]["data"]
    assert data["attachment_count"] == 1
    assert data["attachments"] == [
        {"kind": "image", "content_type": "image/png", "size_bytes": 123}
    ]
    assert "content_sha256" in data
    assert "/private/secret" not in repr(observed_events)
    changed_size = InboundMessage(
        channel_id="qq",
        conversation_id="c2c:user-1",
        user_id="user-1",
        message_id="msg-2",
        text="",
        attachments=(InboundAttachment("image", "image/png", "/another/path.png", 124),),
    )
    assert middleware._inbound_content_sha256(inbound) != middleware._inbound_content_sha256(changed_size)


@pytest.mark.asyncio
async def test_channel_middleware_hides_raw_exception_details_from_user(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from imcodex.channels.middleware import (
        GENERIC_USER_ERROR_TEXT,
        UnifiedChannelMiddleware,
    )

    local_path = "/private/.imcodex/channels/qq/inbound-media/abc123.png"
    service = StubService(error=RuntimeError(f"<html>{local_path}" + ("x" * 500)))
    adapter = CapturingAdapter()
    middleware = UnifiedChannelMiddleware(service=service)

    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="c2c:user-1",
        user_id="user-1",
        message_id="msg-1",
        text="hello",
    )

    await middleware.handle_inbound(adapter, inbound, reply_to_message_id="msg-1")

    assert len(adapter.sent) == 1
    assert adapter.sent[0].message_type == "error"
    assert adapter.sent[0].text == GENERIC_USER_ERROR_TEXT
    assert "<html>" not in adapter.sent[0].text
    assert adapter.sent[0].metadata["reply_to_message_id"] == "msg-1"
    assert local_path not in caplog.text


@pytest.mark.asyncio
async def test_channel_middleware_replays_cached_reply_without_reexecuting_service(
    tmp_path,
) -> None:
    from imcodex.channels.middleware import UnifiedChannelMiddleware

    service = StubService(error=RuntimeError("native unavailable"))
    service.store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)

    class FlakyAdapter(CapturingAdapter):
        fail = True

        async def send_message(self, message: OutboundMessage) -> None:
            if self.fail:
                raise OSError("platform unavailable")
            await super().send_message(message)

    adapter = FlakyAdapter()
    middleware = UnifiedChannelMiddleware(service=service)
    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="c2c:user-1",
        user_id="user-1",
        message_id="msg-1",
        text="hello",
    )

    with pytest.raises(OSError, match="platform unavailable"):
        await middleware.handle_inbound(adapter, inbound)

    adapter.fail = False
    await middleware.handle_inbound(adapter, inbound)

    assert len(service.seen) == 1
    assert [message.message_type for message in adapter.sent] == ["error"]
    assert adapter.sent[0].metadata["delivery_id"].startswith("imcodex:")
    binding = service.store.get_binding("qq", "c2c:user-1")
    assert binding.reply_context["last_inbound_user_id"] == "user-1"


@pytest.mark.asyncio
async def test_channel_middleware_materializes_lazily_after_dedup_and_not_on_replay(
    tmp_path,
) -> None:
    from imcodex.channels.middleware import UnifiedChannelMiddleware

    state_path = tmp_path / "state.json"
    service = StubService(
        outbound=[
            OutboundMessage(
                channel_id="qq",
                conversation_id="c2c:user-1",
                message_type="turn_result",
                text="Done",
            )
        ]
    )
    service.store = ConversationStore(state_path=state_path, clock=lambda: 1.0)
    adapter = CapturingAdapter()
    middleware = UnifiedChannelMiddleware(service=service)
    prepare_calls = 0

    async def prepare(inbound: InboundMessage) -> InboundMessage:
        nonlocal prepare_calls
        prepare_calls += 1
        inbound.attachments = (
            InboundAttachment("image", "image/png", "/private/media/image.png", 123),
        )
        return inbound

    def inbound() -> InboundMessage:
        return InboundMessage(
            channel_id="qq",
            conversation_id="c2c:user-1",
            user_id="user-1",
            message_id="msg-image-1",
            text="describe this",
        )

    await middleware.handle_inbound(
        adapter,
        inbound(),
        prepare_inbound=prepare,
        pending_attachment_count=1,
    )
    await middleware.handle_inbound(
        adapter,
        inbound(),
        prepare_inbound=prepare,
        pending_attachment_count=1,
    )

    assert prepare_calls == 1
    assert len(service.seen) == 1
    assert len(adapter.sent) == 2

    restarted_service = StubService()
    restarted_service.store = ConversationStore(state_path=state_path, clock=lambda: 1.0)
    restarted_adapter = CapturingAdapter()
    await UnifiedChannelMiddleware(service=restarted_service).handle_inbound(
        restarted_adapter,
        inbound(),
        prepare_inbound=prepare,
        pending_attachment_count=1,
    )

    assert prepare_calls == 1
    assert restarted_service.seen == []
    assert [message.text for message in restarted_adapter.sent] == ["Done"]


@pytest.mark.asyncio
async def test_channel_middleware_finalizes_duplicate_resources_before_delivery(
    tmp_path,
) -> None:
    from imcodex.channels.middleware import UnifiedChannelMiddleware

    service = StubService(
        outbound=[
            OutboundMessage(
                channel_id="qq",
                conversation_id="c2c:user-1",
                message_type="turn_result",
                text="Done",
            )
        ]
    )
    service.store = ConversationStore(
        state_path=tmp_path / "state.json",
        clock=lambda: 1.0,
    )
    middleware = UnifiedChannelMiddleware(service=service)

    def inbound() -> InboundMessage:
        return InboundMessage(
            channel_id="qq",
            conversation_id="c2c:user-1",
            user_id="user-1",
            message_id="msg-image-1",
            text="describe this",
        )

    async def prepare(message: InboundMessage) -> InboundMessage:
        message.attachments = (
            InboundAttachment("image", "image/png", "/private/image.png", 123),
        )
        return message

    await middleware.handle_inbound(
        CapturingAdapter(),
        inbound(),
        prepare_inbound=prepare,
        pending_attachment_count=1,
    )

    class BlockingAdapter(CapturingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.delivery_started = asyncio.Event()
            self.release = asyncio.Event()

        async def send_message(self, message: OutboundMessage) -> None:
            self.delivery_started.set()
            await self.release.wait()
            await super().send_message(message)

    adapter = BlockingAdapter()
    finalized = asyncio.Event()

    async def finalize() -> None:
        finalized.set()

    task = asyncio.create_task(
        middleware.handle_inbound(
            adapter,
            inbound(),
            prepare_inbound=prepare,
            finalize_inbound=finalize,
            pending_attachment_count=1,
        )
    )
    await asyncio.wait_for(adapter.delivery_started.wait(), timeout=1)

    assert finalized.is_set()
    adapter.release.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_channel_middleware_preflight_rejection_skips_lazy_materialization() -> None:
    from imcodex.channels.middleware import UnifiedChannelMiddleware

    class PreflightService(StubService):
        def __init__(self) -> None:
            super().__init__()
            self.store = ConversationStore(clock=lambda: 1.0)

        def preflight_inbound_attachments(
            self,
            inbound: InboundMessage,
        ) -> list[OutboundMessage]:
            return [
                OutboundMessage(
                    channel_id=inbound.channel_id,
                    conversation_id=inbound.conversation_id,
                    message_type="error",
                    text="Local image paths are unavailable.",
                )
            ]

    prepare_calls = 0

    async def prepare(inbound: InboundMessage) -> InboundMessage:
        nonlocal prepare_calls
        prepare_calls += 1
        return inbound

    service = PreflightService()
    adapter = CapturingAdapter()
    await UnifiedChannelMiddleware(service=service).handle_inbound(
        adapter,
        InboundMessage(
            channel_id="qq",
            conversation_id="c2c:user-1",
            user_id="user-1",
            message_id="msg-image-1",
            text="",
        ),
        prepare_inbound=prepare,
        pending_attachment_count=1,
    )

    assert prepare_calls == 0
    assert service.seen == []
    assert [message.text for message in adapter.sent] == [
        "Local image paths are unavailable."
    ]


@pytest.mark.asyncio
async def test_channel_middleware_retries_dirty_commit_without_reexecuting_service(
    tmp_path,
) -> None:
    from imcodex.channels.middleware import UnifiedChannelMiddleware

    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    original_write = store._write_serialized_state
    write_attempts = 0

    def fail_first_write(serialized: str, revision: int) -> None:
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            raise OSError("disk unavailable")
        original_write(serialized, revision)

    store._write_serialized_state = fail_first_write  # type: ignore[method-assign]
    service = StubService(
        outbound=[
            OutboundMessage(
                channel_id="qq",
                conversation_id="c2c:user-1",
                message_type="turn_result",
                text="Done",
            )
        ]
    )
    service.store = store
    middleware = UnifiedChannelMiddleware(service=service)
    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="c2c:user-1",
        user_id="user-1",
        message_id="msg-1",
        text="/new",
    )
    adapter = CapturingAdapter()

    with pytest.raises(OSError, match="disk unavailable"):
        await middleware.handle_inbound(adapter, inbound)

    await middleware.handle_inbound(adapter, inbound)

    assert len(service.seen) == 1
    assert [message.text for message in adapter.sent] == ["Done"]
    delivery_id = adapter.sent[0].metadata["delivery_id"]

    reloaded = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    restarted_service = StubService()
    restarted_service.store = reloaded
    restarted_adapter = CapturingAdapter()
    await UnifiedChannelMiddleware(service=restarted_service).handle_inbound(
        restarted_adapter,
        inbound,
    )

    assert restarted_service.seen == []
    assert restarted_adapter.sent[0].metadata["delivery_id"] == delivery_id


@pytest.mark.asyncio
async def test_channel_middleware_sanitizes_cached_metadata_before_persisting(
    tmp_path,
) -> None:
    from imcodex.channels.middleware import UnifiedChannelMiddleware

    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    service = StubService(
        outbound=[
            OutboundMessage(
                channel_id="qq",
                conversation_id="c2c:user-1",
                message_type="turn_result",
                text="Done",
                metadata={"unsupported": object(), "not_finite": float("nan")},
            )
        ]
    )
    service.store = store
    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="c2c:user-1",
        user_id="user-1",
        message_id="msg-1",
        text="inspect",
    )
    adapter = CapturingAdapter()
    middleware = UnifiedChannelMiddleware(service=service)

    await middleware.handle_inbound(adapter, inbound)
    await middleware.handle_inbound(adapter, inbound)

    assert len(service.seen) == 1
    assert len(adapter.sent) == 2
    assert adapter.sent[0].metadata["unsupported"] is None
    assert adapter.sent[0].metadata["not_finite"] is None
    assert adapter.sent[1].metadata == adapter.sent[0].metadata


@pytest.mark.asyncio
async def test_channel_middleware_explicitly_reports_evicted_cached_reply() -> None:
    from imcodex.channels.middleware import (
        EXPIRED_REPLAY_TEXT,
        UnifiedChannelMiddleware,
    )

    store = ConversationStore(clock=lambda: 1.0)
    for index in range(store.RECENT_INBOUND_RESPONSE_LIMIT + 1):
        store.mark_inbound_message_processed(
            channel_id="qq",
            conversation_id="c2c:user-1",
            user_id="user-1",
            message_id=f"msg-{index}",
            text_fingerprint=f"fingerprint-{index}",
            response_payload=[
                asdict(
                    OutboundMessage(
                        channel_id="qq",
                        conversation_id="c2c:user-1",
                        message_type="turn_result",
                        text=f"result-{index}",
                    )
                )
            ],
        )
    service = StubService()
    service.store = store
    adapter = CapturingAdapter()

    middleware = UnifiedChannelMiddleware(service=service)
    old_inbound = InboundMessage(
        channel_id="qq",
        conversation_id="c2c:user-1",
        user_id="user-1",
        message_id="msg-0",
        text="old request",
    )
    await middleware.handle_inbound(
        adapter,
        old_inbound,
    )

    assert service.seen == []
    assert adapter.sent[0].message_type == "error"
    assert adapter.sent[0].text == EXPIRED_REPLAY_TEXT
    assert adapter.sent[0].metadata["cached_response_expired"] is True
    assert adapter.sent[0].metadata["delivery_id"] != middleware._delivery_id(old_inbound, 0)


@pytest.mark.asyncio
async def test_channel_middleware_executes_concurrent_same_id_only_once() -> None:
    class BlockingService:
        def __init__(self) -> None:
            self.store = ConversationStore(clock=lambda: 1.0)
            self.calls: list[str] = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
            self.calls.append(message.message_id)
            self.started.set()
            await self.release.wait()
            return []

    service = BlockingService()
    middleware = __import__(
        "imcodex.channels.middleware",
        fromlist=["UnifiedChannelMiddleware"],
    ).UnifiedChannelMiddleware(service=service)
    adapter = CapturingAdapter()
    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="c2c:user-1",
        user_id="user-1",
        message_id="msg-1",
        text="hello",
    )

    first = asyncio.create_task(middleware.handle_inbound(adapter, inbound))
    await service.started.wait()
    second = asyncio.create_task(middleware.handle_inbound(adapter, inbound))
    await asyncio.sleep(0)
    assert service.calls == ["msg-1"]

    service.release.set()
    await asyncio.gather(first, second)

    assert service.calls == ["msg-1"]


@pytest.mark.asyncio
async def test_channel_middleware_serializes_distinct_messages_per_conversation() -> None:
    class BlockingService:
        def __init__(self) -> None:
            self.store = ConversationStore(clock=lambda: 1.0)
            self.calls: list[str] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def handle_inbound(self, message: InboundMessage) -> list[OutboundMessage]:
            self.calls.append(message.message_id)
            if message.message_id == "msg-1":
                self.first_started.set()
                await self.release_first.wait()
            return []

    service = BlockingService()
    middleware = __import__(
        "imcodex.channels.middleware",
        fromlist=["UnifiedChannelMiddleware"],
    ).UnifiedChannelMiddleware(service=service)
    adapter = CapturingAdapter()

    def inbound(message_id: str) -> InboundMessage:
        return InboundMessage(
            channel_id="qq",
            conversation_id="c2c:user-1",
            user_id="user-1",
            message_id=message_id,
            text=message_id,
        )

    first = asyncio.create_task(middleware.handle_inbound(adapter, inbound("msg-1")))
    await service.first_started.wait()
    second = asyncio.create_task(middleware.handle_inbound(adapter, inbound("msg-2")))
    await asyncio.sleep(0)
    assert service.calls == ["msg-1"]

    service.release_first.set()
    await asyncio.gather(first, second)

    assert service.calls == ["msg-1", "msg-2"]


@pytest.mark.asyncio
async def test_durable_dedupe_write_does_not_block_event_loop(tmp_path) -> None:
    writer_started = Event()
    release_writer = Event()
    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    original_write = store._write_serialized_state

    def slow_write(serialized: str, revision: int) -> None:
        writer_started.set()
        release_writer.wait(timeout=1)
        original_write(serialized, revision)

    store._write_serialized_state = slow_write  # type: ignore[method-assign]
    service = StubService()
    service.store = store
    middleware = __import__(
        "imcodex.channels.middleware",
        fromlist=["UnifiedChannelMiddleware"],
    ).UnifiedChannelMiddleware(service=service)
    inbound = InboundMessage(
        channel_id="qq",
        conversation_id="c2c:user-1",
        user_id="user-1",
        message_id="msg-1",
        text="hello",
    )

    task = asyncio.create_task(middleware.handle_inbound(CapturingAdapter(), inbound))
    assert await asyncio.to_thread(writer_started.wait, 1)
    await asyncio.sleep(0)

    assert not task.done()
    release_writer.set()
    await task


@pytest.mark.asyncio
async def test_async_dedupe_fsync_does_not_block_normal_state_save(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_started = Event()
    release_writer = Event()
    original_fsync = __import__("os").fsync
    fsync_calls = 0

    def block_first_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            writer_started.set()
            release_writer.wait(timeout=2)
        original_fsync(fd)

    monkeypatch.setattr("imcodex.store.os.fsync", block_first_fsync)
    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    response = asdict(
        OutboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            message_type="turn_result",
            text="Done",
        )
    )
    commit = asyncio.create_task(
        store.commit_inbound_message_processed(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="user-1",
            message_id="msg-1",
            text_fingerprint="fingerprint",
            response_payload=[response],
        )
    )
    assert await asyncio.to_thread(writer_started.wait, 1)
    watchdog = Timer(1.0, release_writer.set)
    watchdog.start()
    started_at = time.monotonic()
    try:
        store.set_bootstrap_cwd("qq", "conv-2", "/work")
    finally:
        elapsed = time.monotonic() - started_at
        release_writer.set()
        watchdog.cancel()
    await commit
    await store.flush_pending_writes()

    assert elapsed < 0.5
    reloaded = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    assert reloaded.current_cwd("qq", "conv-2") == "/work"
    assert reloaded.should_drop_duplicate_inbound_message(
        channel_id="qq",
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        text_fingerprint="fingerprint",
    )


@pytest.mark.asyncio
async def test_normal_state_save_moves_slow_fsync_off_event_loop(tmp_path) -> None:
    writer_started = Event()
    release_writer = Event()
    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    original_write = store._write_serialized_state

    def slow_write(serialized: str, revision: int) -> None:
        writer_started.set()
        release_writer.wait(timeout=1)
        original_write(serialized, revision)

    store._write_serialized_state = slow_write  # type: ignore[method-assign]
    ticker_fired = asyncio.Event()

    async def tick() -> None:
        await asyncio.sleep(0.01)
        ticker_fired.set()

    ticker = asyncio.create_task(tick())
    store.set_bootstrap_cwd("qq", "conv-1", "/work")
    assert await asyncio.to_thread(writer_started.wait, 1)
    await asyncio.wait_for(ticker_fired.wait(), timeout=0.2)

    release_writer.set()
    await store.flush_pending_writes()
    await ticker

    reloaded = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    assert reloaded.current_cwd("qq", "conv-1") == "/work"


@pytest.mark.asyncio
async def test_cancelled_failed_dedupe_write_remains_retryable(tmp_path) -> None:
    writer_started = Event()
    release_writer = Event()
    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    original_write = store._write_serialized_state

    def blocked_failure(_serialized: str, _revision: int) -> None:
        writer_started.set()
        release_writer.wait(timeout=1)
        raise OSError("disk unavailable")

    store._write_serialized_state = blocked_failure  # type: ignore[method-assign]
    response = asdict(
        OutboundMessage(
            channel_id="qq",
            conversation_id="conv-1",
            message_type="turn_result",
            text="Done",
        )
    )
    commit = asyncio.create_task(
        store.commit_inbound_message_processed(
            channel_id="qq",
            conversation_id="conv-1",
            user_id="user-1",
            message_id="msg-1",
            text_fingerprint="fingerprint",
            response_payload=[response],
        )
    )
    assert await asyncio.to_thread(writer_started.wait, 1)
    commit.cancel()
    release_writer.set()

    with pytest.raises(OSError, match="disk unavailable"):
        await commit

    assert store.should_drop_duplicate_inbound_message(
        channel_id="qq",
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        text_fingerprint="fingerprint",
    )
    store._write_serialized_state = original_write  # type: ignore[method-assign]
    await store.ensure_inbound_message_durable("qq", "conv-1", "msg-1")

    reloaded = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    assert reloaded.get_processed_inbound_response("qq", "conv-1", "msg-1") == [response]
