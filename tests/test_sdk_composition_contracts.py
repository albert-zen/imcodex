from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from imagent import Gateway, SQLiteGatewayStore
from imagent.applications.capabilities import ProjectMode
from imagent.gateway.delivery import (
    DeliveryIntent,
    DeliveryPrincipal,
    ScopedDeliveryAuthorizer,
    ThreadRouteDeliveryTarget,
)
from imagent.gateway.outcomes import Succeeded
from imagent.interaction.messages import ConversationRef, InboundMessage, TextContent
from imagent.testing import FakeAgentApplicationAdapter, FakeChannelAdapter

from imcodex.sdk_composition import SDK_PROJECTION_POLICY


async def test_sqlite_composition_restores_public_conversation_binding(tmp_path) -> None:
    database = tmp_path / "gateway.sqlite3"
    application = FakeAgentApplicationAdapter(project_mode=ProjectMode.FLAT)
    conversation = ConversationRef("telegram", "conversation-1")
    thread_ref = None

    first = Gateway(
        gateway_id="imcodex-contract",
        channels=[FakeChannelAdapter("telegram")],
        applications=[application],
        store=SQLiteGatewayStore(database),
        projection_policy=SDK_PROJECTION_POLICY,
    )
    async with first:
        actions = first.actions(conversation, actor="user-1")
        created = await actions.create_and_bind_thread(
            application.default_project_ref,
            action_id="contract:create-thread",
        )
        assert isinstance(created, Succeeded)
        thread_ref = created.value.ref
        observed = await actions.observe_thread(
            thread_ref,
            action_id="contract:observe-thread",
            reply_to_message_id="inbound-1",
        )
        assert isinstance(observed, Succeeded)

    restarted = Gateway(
        gateway_id="imcodex-contract",
        channels=[FakeChannelAdapter("telegram")],
        applications=[application],
        store=SQLiteGatewayStore(database),
        projection_policy=SDK_PROJECTION_POLICY,
    )
    async with restarted:
        binding = await restarted.actions(conversation, actor="user-1").get_binding()

    assert binding is not None
    assert binding.thread_ref == thread_ref
    assert binding.project_ref == application.default_project_ref


async def test_remembered_thread_route_moves_to_last_selecting_conversation(tmp_path) -> None:
    application = FakeAgentApplicationAdapter(project_mode=ProjectMode.FLAT)
    channel = FakeChannelAdapter("telegram")
    authorizer = ScopedDeliveryAuthorizer()
    gateway = Gateway(
        gateway_id="imcodex-route-contract",
        channels=[channel],
        applications=[application],
        store=SQLiteGatewayStore(tmp_path / "routes.sqlite3"),
        projection_policy=SDK_PROJECTION_POLICY,
        delivery_authorizer=authorizer,
    )
    first = ConversationRef("telegram", "first")
    second = ConversationRef("telegram", "second")

    async with gateway:
        first_actions = gateway.actions(first, actor="user-1")
        created = await first_actions.create_and_bind_thread(
            application.default_project_ref,
            action_id="route:create",
        )
        assert isinstance(created, Succeeded)
        thread_ref = created.value.ref
        assert isinstance(
            await first_actions.observe_thread(
                thread_ref,
                action_id="route:observe-first",
                reply_to_message_id="first-message",
            ),
            Succeeded,
        )

        second_actions = gateway.actions(second, actor="user-2")
        assert isinstance(
            await second_actions.bind_thread(thread_ref, action_id="route:bind-second"),
            Succeeded,
        )
        assert isinstance(
            await second_actions.observe_thread(
                thread_ref,
                action_id="route:observe-second",
                reply_to_message_id="second-message",
            ),
            Succeeded,
        )
        routed_at = len(channel.sent)
        await channel.emit_message(
            InboundMessage(
                message_id="second-message",
                conversation_ref=second,
                sender="user-2",
                content=(TextContent("select route"),),
                created_at=datetime.now(UTC),
            )
        )
        async with asyncio.timeout(2):
            while len(channel.sent) == routed_at:
                await asyncio.sleep(0)
        before = len(channel.sent)
        credential = await authorizer.issue(
            DeliveryPrincipal(
                principal_id="route-contract",
                allowed_threads=(thread_ref,),
            ),
            credential="route-contract-credential",
        )
        await gateway.deliver_proactively(
            DeliveryIntent(
                delivery_id="route:proactive",
                target=ThreadRouteDeliveryTarget(thread_ref),
                content=(TextContent("background route check"),),
                created_at=datetime.now(UTC),
            ),
            credential=credential,
        )
        async with asyncio.timeout(2):
            while len(channel.sent) == before:
                await asyncio.sleep(0)

    projected = channel.sent[before:]
    assert projected
    assert {message.conversation_ref for message in projected} == {second}
