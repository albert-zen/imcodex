from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

import pytest
from imagent.contracts import (
    ApplicationRef,
    ConversationBinding,
    ConversationRef,
    ThreadProjectionRoute,
    ThreadRef,
)
from imagent.projections import derive_projection_route_id
from imagent.storage import SQLiteGatewayState

from imcodex.models import OutboundMessage as ProductOutboundMessage
from imcodex.sdk_migration import (
    migrate_legacy_gateway_state,
    recover_legacy_deliveries,
)
from imcodex.store import ConversationStore
from imcodex.webhook_namespace import encode_webhook_conversation


def _legacy_delivery_store(tmp_path, *, delivery_id: str, message) -> ConversationStore:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "bindings": [],
                "pending_terminal_deliveries": [
                    {
                        "delivery_id": delivery_id,
                        "thread_id": "thread-1",
                        "turn_id": "turn-1",
                        "message": asdict(message),
                        "created_at": 99.0,
                        "sequence": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return ConversationStore(clock=lambda: 100.0, state_path=state_path)


@pytest.mark.asyncio
async def test_legacy_binding_and_route_are_imported_once(tmp_path) -> None:
    product = ConversationStore(clock=lambda: 100.0, state_path=tmp_path / "state.json")
    product.set_bootstrap_cwd("telegram", "chat-1", "/repo")
    product.bind_thread("telegram", "chat-1", "thread-1")
    state = SQLiteGatewayState(tmp_path / "gateway.sqlite3")
    try:
        first = await migrate_legacy_gateway_state(
            product_store=product,
            gateway_state=state,
            application_instance_id="codex-main",
            native_channel_ids=frozenset({"telegram"}),
        )
        second = await migrate_legacy_gateway_state(
            product_store=product,
            gateway_state=state,
            application_instance_id="codex-main",
            native_channel_ids=frozenset({"telegram"}),
        )

        binding = await state.get(ConversationRef("telegram", "chat-1"))
        routes = await state.list_projection_routes()
        assert first == 1
        assert second == 0
        assert binding is not None
        assert binding.thread_ref.native_thread_id == "thread-1"
        assert len(routes) == 1
        assert ConversationStore(
            clock=lambda: 101.0,
            state_path=tmp_path / "state.json",
        ).sdk_gateway_migration_completed()
    finally:
        await state.close()


@pytest.mark.asyncio
async def test_legacy_generic_webhook_binding_uses_multiplexed_sdk_namespace(
    tmp_path,
) -> None:
    product = ConversationStore(clock=lambda: 100.0, state_path=tmp_path / "state.json")
    product.set_bootstrap_cwd("custom-a", "room/1", "/repo")
    product.bind_thread("custom-a", "room/1", "thread-1")
    state = SQLiteGatewayState(tmp_path / "gateway.sqlite3")
    try:
        await migrate_legacy_gateway_state(
            product_store=product,
            gateway_state=state,
            application_instance_id="codex-main",
            native_channel_ids=frozenset({"telegram", "qq", "feishu", "weixin"}),
        )

        conversation = ConversationRef(
            "webhook",
            encode_webhook_conversation("custom-a", "room/1"),
        )
        binding = await state.get(conversation)
        routes = await state.list_projection_routes()
        assert binding is not None
        assert binding.thread_ref.native_thread_id == "thread-1"
        assert routes[0].conversation_ref == conversation
    finally:
        await state.close()


@pytest.mark.asyncio
async def test_existing_sdk_binding_rebuilds_its_foreground_route(tmp_path) -> None:
    product = ConversationStore(clock=lambda: 100.0, state_path=tmp_path / "state.json")
    product.set_bootstrap_cwd("telegram", "chat-1", "/repo")
    product.bind_thread("telegram", "chat-1", "thread-1")
    state = SQLiteGatewayState(tmp_path / "gateway.sqlite3")
    conversation = ConversationRef("telegram", "chat-1")
    await state.put(
        ConversationBinding(
            conversation_ref=conversation,
            application_ref=ApplicationRef("codex-main"),
            thread_ref=ThreadRef("codex-main", "thread-1"),
        )
    )
    try:
        imported = await migrate_legacy_gateway_state(
            product_store=product,
            gateway_state=state,
            application_instance_id="codex-main",
            native_channel_ids=frozenset({"telegram"}),
        )

        assert imported == 0
        routes = await state.list_projection_routes()
        assert [route.conversation_ref for route in routes] == [conversation]
    finally:
        await state.close()


@pytest.mark.asyncio
async def test_existing_sdk_binding_and_remembered_route_win_over_product_cache(
    tmp_path,
) -> None:
    product = ConversationStore(clock=lambda: 100.0, state_path=tmp_path / "state.json")
    product.bind_thread("telegram", "chat-1", "thread-product")
    state = SQLiteGatewayState(tmp_path / "gateway.sqlite3")
    conversation = ConversationRef("telegram", "chat-1")
    await state.put(
        ConversationBinding(
            conversation_ref=conversation,
            application_ref=ApplicationRef("codex-main"),
            thread_ref=ThreadRef("codex-main", "thread-sdk-old"),
        )
    )
    stale_product_thread = ThreadRef("codex-main", "thread-product")
    await state.put_projection_route(
        ThreadProjectionRoute(
            route_id=derive_projection_route_id(stale_product_thread, conversation),
            thread_ref=stale_product_thread,
            conversation_ref=conversation,
            updated_at=datetime.now(UTC),
        )
    )
    try:
        await migrate_legacy_gateway_state(
            product_store=product,
            gateway_state=state,
            application_instance_id="codex-main",
            native_channel_ids=frozenset({"telegram"}),
        )

        binding = await state.get(conversation)
        assert binding is not None
        assert binding.thread_ref == ThreadRef("codex-main", "thread-sdk-old")
        routes = await state.list_projection_routes()
        assert {route.thread_ref for route in routes} == {
            ThreadRef("codex-main", "thread-product"),
            ThreadRef("codex-main", "thread-sdk-old"),
        }
    finally:
        await state.close()


@pytest.mark.asyncio
async def test_completed_handoff_never_promotes_new_product_cache(tmp_path) -> None:
    product = ConversationStore(clock=lambda: 100.0, state_path=tmp_path / "state.json")
    state = SQLiteGatewayState(tmp_path / "gateway.sqlite3")
    try:
        assert (
            await migrate_legacy_gateway_state(
                product_store=product,
                gateway_state=state,
                application_instance_id="codex-main",
                native_channel_ids=frozenset({"telegram"}),
            )
            == 0
        )
        product.bind_thread("telegram", "post-cutover", "thread-uncommitted")

        assert (
            await migrate_legacy_gateway_state(
                product_store=product,
                gateway_state=state,
                application_instance_id="codex-main",
                native_channel_ids=frozenset({"telegram"}),
            )
            == 0
        )
        assert await state.get(ConversationRef("telegram", "post-cutover")) is None
    finally:
        await state.close()


@pytest.mark.asyncio
async def test_import_adds_another_foreground_subscriber_without_replacing_existing(
    tmp_path,
) -> None:
    product = ConversationStore(clock=lambda: 100.0, state_path=tmp_path / "state.json")
    product.bind_thread("telegram", "conversation-a", "thread-1")
    state = SQLiteGatewayState(tmp_path / "gateway.sqlite3")
    thread = ThreadRef("codex-main", "thread-1")
    conversation_a = ConversationRef("telegram", "conversation-a")
    conversation_b = ConversationRef("telegram", "conversation-b")
    await state.replace_thread_projection_routes(
        ThreadProjectionRoute(
            route_id=derive_projection_route_id(thread, conversation_b),
            thread_ref=thread,
            conversation_ref=conversation_b,
            updated_at=datetime.now(UTC),
        )
    )
    try:
        imported = await migrate_legacy_gateway_state(
            product_store=product,
            gateway_state=state,
            application_instance_id="codex-main",
            native_channel_ids=frozenset({"telegram"}),
        )

        assert imported == 1
        binding = await state.get(conversation_a)
        assert binding is not None
        assert binding.thread_ref == thread
        routes = await state.list_projection_routes(thread)
        assert {route.conversation_ref for route in routes} == {
            conversation_a,
            conversation_b,
        }
    finally:
        await state.close()


@pytest.mark.asyncio
async def test_legacy_pending_delivery_completes_only_after_sdk_acceptance(
    tmp_path,
) -> None:
    message = ProductOutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="result",
        text="Done",
        metadata={"delivery_id": "legacy-delivery-1"},
    )
    product = _legacy_delivery_store(
        tmp_path,
        delivery_id="legacy-delivery-1",
        message=message,
    )
    discarded = []

    class Delivery:
        async def deliver_outbound_message(self, outbound):
            assert outbound.text == "Done"
            return [outbound], True, True

        async def discard_outbound_uploads(self, artifacts):
            discarded.append(tuple(artifacts))

    completed = await recover_legacy_deliveries(
        product_store=product,
        delivery_service=Delivery(),
    )

    assert completed == 1
    assert product.list_legacy_delivery_evidence() == []
    assert discarded == [()]


@pytest.mark.parametrize("legacy_metadata", ({}, {"delivery_id": "wrong-id"}))
@pytest.mark.asyncio
async def test_legacy_delivery_uses_evidence_identity_for_sdk_replay(
    tmp_path,
    legacy_metadata,
) -> None:
    message = ProductOutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="result",
        text="Done",
        metadata=legacy_metadata,
    )
    product = _legacy_delivery_store(
        tmp_path,
        delivery_id="legacy-stable-id",
        message=message,
    )

    class Delivery:
        async def deliver_outbound_message(self, outbound):
            assert outbound.metadata["delivery_id"] == "legacy-stable-id"
            return [outbound], True, True

        async def discard_outbound_uploads(self, artifacts):
            assert artifacts == []

    assert (
        await recover_legacy_deliveries(
            product_store=product,
            delivery_service=Delivery(),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_legacy_pending_delivery_stays_durable_until_sdk_acceptance(
    tmp_path,
) -> None:
    message = ProductOutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="result",
        text="Done",
        metadata={"delivery_id": "legacy-delivery-1"},
    )
    product = _legacy_delivery_store(
        tmp_path,
        delivery_id="legacy-delivery-1",
        message=message,
    )

    class Delivery:
        async def deliver_outbound_message(self, outbound):
            return [outbound], False, True

        async def discard_outbound_uploads(self, artifacts):
            raise AssertionError("pending artifacts must remain referenced")

    completed = await recover_legacy_deliveries(
        product_store=product,
        delivery_service=Delivery(),
    )

    assert completed == 0
    assert len(product.list_legacy_delivery_evidence()) == 1


@pytest.mark.asyncio
async def test_legacy_permanent_delivery_failure_is_explicit(tmp_path) -> None:
    message = ProductOutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="result",
        text="Done",
        metadata={"delivery_id": "legacy-delivery-blocked"},
    )
    product = _legacy_delivery_store(
        tmp_path,
        delivery_id="legacy-delivery-blocked",
        message=message,
    )

    class Delivery:
        async def deliver_outbound_message(self, outbound):
            del outbound
            return [], False, False

        async def discard_outbound_uploads(self, artifacts):
            raise AssertionError("permanent failures keep legacy evidence")

    with pytest.raises(RuntimeError, match="legacy-delivery-blocked.*permanent"):
        await recover_legacy_deliveries(
            product_store=product,
            delivery_service=Delivery(),
        )

    assert len(product.list_legacy_delivery_evidence()) == 1


@pytest.mark.asyncio
async def test_legacy_unclassified_failure_degrades_maintenance(tmp_path) -> None:
    message = ProductOutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="result",
        text="Done",
        metadata={"delivery_id": "legacy-delivery-retry"},
    )
    product = _legacy_delivery_store(
        tmp_path,
        delivery_id="legacy-delivery-retry",
        message=message,
    )

    class Delivery:
        async def deliver_outbound_message(self, outbound):
            del outbound
            raise OSError("artifact ledger write failed")

        async def discard_outbound_uploads(self, artifacts):
            raise AssertionError("failed migration keeps legacy evidence")

    with pytest.raises(
        RuntimeError,
        match="legacy-delivery-retry.*artifact ledger write failed",
    ):
        await recover_legacy_deliveries(
            product_store=product,
            delivery_service=Delivery(),
        )

    assert len(product.list_legacy_delivery_evidence()) == 1


@pytest.mark.asyncio
async def test_legacy_artifacts_are_not_discarded_before_evidence_commit(
    tmp_path,
    monkeypatch,
) -> None:
    message = ProductOutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="result",
        text="Done",
        metadata={"delivery_id": "legacy-delivery-commit"},
    )
    product = _legacy_delivery_store(
        tmp_path,
        delivery_id="legacy-delivery-commit",
        message=message,
    )

    async def fail_write(_serialized, _revision) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(product, "_write_state_async", fail_write)

    class Delivery:
        async def deliver_outbound_message(self, outbound):
            return [outbound], True, True

        async def discard_outbound_uploads(self, artifacts):
            raise AssertionError("artifacts must survive an evidence commit failure")

    with pytest.raises(
        RuntimeError,
        match="legacy-delivery-commit.*evidence commit failed",
    ):
        await recover_legacy_deliveries(
            product_store=product,
            delivery_service=Delivery(),
        )

    assert [
        item.delivery_id for item in product.list_legacy_delivery_evidence()
    ] == ["legacy-delivery-commit"]
