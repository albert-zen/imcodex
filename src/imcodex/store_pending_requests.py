from __future__ import annotations

from .models import PendingNativeRequestRoute


class PendingRequestStoreMixin:
    def upsert_pending_request(
        self,
        *,
        request_id: str,
        request_handle: str | None = None,
        channel_id: str,
        conversation_id: str,
        thread_id: str | None,
        turn_id: str | None,
        kind: str,
        request_method: str | None,
        transport_request_id: str | int | None = None,
        connection_epoch: int = 0,
        payload: dict | None = None,
    ) -> PendingNativeRequestRoute:
        route = PendingNativeRequestRoute(
            request_id=request_id,
            request_handle=request_handle or request_id[:8],
            channel_id=channel_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            kind=kind,
            request_method=request_method,
            transport_request_id=transport_request_id,
            connection_epoch=connection_epoch,
            payload=dict(payload or {}),
        )
        self._pending_requests[request_id] = route
        return route

    def list_pending_requests(
        self,
        channel_id: str,
        conversation_id: str,
        *,
        kind: str | None = None,
    ) -> list[PendingNativeRequestRoute]:
        routes = [
            route
            for route in self._pending_requests.values()
            if route.channel_id == channel_id and route.conversation_id == conversation_id
        ]
        if kind is not None:
            routes = [route for route in routes if route.kind == kind]
        return routes

    def get_pending_request(self, request_id: str) -> PendingNativeRequestRoute | None:
        return self._pending_requests.get(request_id)

    def select_pending_requests(
        self,
        channel_id: str,
        conversation_id: str,
        token: str | None = None,
        *,
        kind: str | None = None,
    ) -> list[PendingNativeRequestRoute]:
        candidates = self.list_pending_requests(channel_id, conversation_id, kind=kind)
        if not candidates:
            return []
        if token is None or not token.strip():
            return candidates
        token = token.strip()
        exact = [
            route
            for route in candidates
            if token == route.request_id or (route.request_handle is not None and token == route.request_handle)
        ]
        if exact:
            return exact[:1]
        prefix_matches = [
            route
            for route in candidates
            if route.request_id.startswith(token)
            or (route.request_handle is not None and route.request_handle.startswith(token))
        ]
        if len(prefix_matches) > 1:
            raise ValueError(f"Ambiguous request id prefix: {token}")
        if len(prefix_matches) == 1:
            return prefix_matches
        return []

    def match_pending_request(
        self,
        channel_id: str,
        conversation_id: str,
        token: str | None = None,
        *,
        kind: str | None = None,
    ) -> PendingNativeRequestRoute | None:
        candidates = self.select_pending_requests(
            channel_id,
            conversation_id,
            token,
            kind=kind,
        )
        if token is None or not str(token).strip():
            if len(candidates) == 1:
                return candidates[0]
            return None
        return candidates[0] if candidates else None

    def remove_pending_request(self, request_id: str) -> PendingNativeRequestRoute | None:
        route = self._pending_requests.pop(request_id, None)
        return route

    def remove_pending_requests_for_turn(self, thread_id: str, turn_id: str) -> None:
        removed = [
            request_id
            for request_id, route in self._pending_requests.items()
            if route.thread_id == thread_id and route.turn_id == turn_id
        ]
        if not removed:
            return
        for request_id in removed:
            self._pending_requests.pop(request_id, None)

    def invalidate_pending_requests_for_connection(self, connection_epoch: int) -> list[PendingNativeRequestRoute]:
        removed = [route for route in self._pending_requests.values() if route.connection_epoch == connection_epoch]
        if not removed:
            return []
        for route in removed:
            self._pending_requests.pop(route.request_id, None)
        return removed
