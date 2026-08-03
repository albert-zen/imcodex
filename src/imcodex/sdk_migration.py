from __future__ import annotations

from datetime import UTC, datetime

from imagent.contracts import (
    ApplicationRef,
    ConversationBinding,
    ConversationRef,
    ThreadProjectionRoute,
    ThreadRef,
)
from imagent.projections import derive_projection_route_id

from .models import OutboundMessage
from .webhook_namespace import (
    WEBHOOK_CHANNEL_INSTANCE_ID,
    encode_webhook_conversation,
)


async def migrate_legacy_gateway_state(
    *,
    product_store,
    gateway_state,
    application_instance_id: str,
    native_channel_ids: frozenset[str] = frozenset(),
) -> int:
    """Import legacy bindings/routes once without importing Agent state."""

    imported = 0
    for legacy in product_store.iter_bindings():
        conversation = (
            ConversationRef(legacy.channel_id, legacy.conversation_id)
            if legacy.channel_id in native_channel_ids
            else ConversationRef(
                WEBHOOK_CHANNEL_INSTANCE_ID,
                encode_webhook_conversation(
                    legacy.channel_id,
                    legacy.conversation_id,
                ),
            )
        )
        current = await gateway_state.get(conversation)
        if current is None:
            thread_ref = (
                ThreadRef(application_instance_id, legacy.thread_id)
                if legacy.thread_id
                else None
            )
            await gateway_state.put(
                ConversationBinding(
                    conversation_ref=conversation,
                    application_ref=ApplicationRef(application_instance_id),
                    thread_ref=thread_ref,
                    updated_at=datetime.now(UTC),
                )
            )
            imported += 1
        else:
            thread_ref = current.thread_ref
        if thread_ref is None:
            continue
        route = ThreadProjectionRoute(
            route_id=derive_projection_route_id(thread_ref, conversation),
            thread_ref=thread_ref,
            conversation_ref=conversation,
            updated_at=datetime.now(UTC),
        )
        await gateway_state.put_projection_route(route)
    return imported


async def recover_legacy_deliveries(*, product_store, delivery_service) -> int:
    """Converge the old durable outbox through SDK delivery, then retire entries."""

    completed = 0
    for pending in product_store.list_pending_terminal_deliveries():
        message = OutboundMessage(**pending.message)
        try:
            (
                _outbound,
                delivered,
                _durable,
            ) = await delivery_service.deliver_outbound_message(message)
        except Exception:
            continue
        if not delivered:
            continue
        product_store.complete_terminal_delivery(pending.delivery_id)
        await delivery_service.discard_outbound_uploads(message.artifacts)
        completed += 1
    if completed:
        await product_store.flush_pending_writes()
    return completed
