from __future__ import annotations

from dataclasses import asdict

import pytest
from imagent.contracts import (
    ApplicationRef,
    ConversationBinding,
    ConversationRef,
    ThreadRef,
)
from imagent.storage import SQLiteGatewayState

from imcodex.models import OutboundMessage as ProductOutboundMessage
from imcodex.sdk_migration import (
    migrate_legacy_gateway_state,
    recover_legacy_deliveries,
)
from imcodex.store import ConversationStore
from imcodex.webhook_namespace import encode_webhook_conversation


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
async def test_binding_import_resumes_after_crash_before_route_write(tmp_path) -> None:
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
        assert len(await state.list_projection_routes()) == 1
    finally:
        await state.close()


@pytest.mark.asyncio
async def test_legacy_pending_delivery_completes_only_after_sdk_acceptance(
    tmp_path,
) -> None:
    product = ConversationStore(clock=lambda: 100.0, state_path=tmp_path / "state.json")
    message = ProductOutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="result",
        text="Done",
        metadata={"delivery_id": "legacy-delivery-1"},
    )
    product.stage_terminal_delivery(
        delivery_id="legacy-delivery-1",
        thread_id="thread-1",
        turn_id="turn-1",
        message=asdict(message),
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
    assert product.list_pending_terminal_deliveries() == []
    assert product.is_terminal_delivery_acknowledged("legacy-delivery-1") is True
    assert discarded == [()]


@pytest.mark.asyncio
async def test_legacy_pending_delivery_stays_durable_until_sdk_acceptance(
    tmp_path,
) -> None:
    product = ConversationStore(clock=lambda: 100.0, state_path=tmp_path / "state.json")
    message = ProductOutboundMessage(
        channel_id="telegram",
        conversation_id="chat-1",
        message_type="result",
        text="Done",
        metadata={"delivery_id": "legacy-delivery-1"},
    )
    product.stage_terminal_delivery(
        delivery_id="legacy-delivery-1",
        thread_id="thread-1",
        turn_id="turn-1",
        message=asdict(message),
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
    assert len(product.list_pending_terminal_deliveries()) == 1
