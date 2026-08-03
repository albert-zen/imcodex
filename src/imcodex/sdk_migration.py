from __future__ import annotations

import copy
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

    if product_store.sdk_gateway_migration_completed():
        return 0
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
        desired_thread_ref = (
            ThreadRef(application_instance_id, legacy.thread_id)
            if legacy.thread_id
            else None
        )
        if current is None:
            if desired_thread_ref is not None:
                await gateway_state.put_projection_route(
                    ThreadProjectionRoute(
                        route_id=derive_projection_route_id(
                            desired_thread_ref,
                            conversation,
                        ),
                        thread_ref=desired_thread_ref,
                        conversation_ref=conversation,
                        updated_at=datetime.now(UTC),
                    )
                )
            current = await gateway_state.put(
                ConversationBinding(
                    conversation_ref=conversation,
                    application_ref=ApplicationRef(application_instance_id),
                    thread_ref=desired_thread_ref,
                    updated_at=datetime.now(UTC),
                )
            )
            imported += 1
        else:
            thread_ref = current.thread_ref
            if thread_ref is not None:
                await gateway_state.put_projection_route(
                    ThreadProjectionRoute(
                        route_id=derive_projection_route_id(thread_ref, conversation),
                        thread_ref=thread_ref,
                        conversation_ref=conversation,
                        updated_at=datetime.now(UTC),
                    )
                )
    await product_store.commit_sdk_gateway_migration_completed()
    return imported


async def recover_legacy_deliveries(*, product_store, delivery_service) -> int:
    """Converge the old durable outbox through SDK delivery, then retire entries."""

    completed = 0
    for pending in product_store.list_legacy_delivery_evidence():
        message_payload = copy.deepcopy(pending.message)
        metadata = message_payload.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["delivery_id"] = pending.delivery_id
        message_payload["metadata"] = metadata
        message = OutboundMessage(**message_payload)
        try:
            (
                _outbound,
                delivered,
                _durable,
            ) = await delivery_service.deliver_outbound_message(message)
        except ValueError as exc:
            raise RuntimeError(
                f"legacy delivery {pending.delivery_id} cannot migrate: {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"legacy delivery {pending.delivery_id} migration attempt failed: {exc}"
            ) from exc
        if not delivered:
            if not _durable:
                raise RuntimeError(
                    f"legacy delivery {pending.delivery_id} reached a permanent non-delivered outcome"
                )
            continue
        try:
            await product_store.consume_legacy_delivery_evidence(pending.delivery_id)
        except Exception as exc:
            raise RuntimeError(
                f"legacy delivery {pending.delivery_id} evidence commit failed: {exc}"
            ) from exc
        await delivery_service.discard_outbound_uploads(message.artifacts)
        completed += 1
    return completed
