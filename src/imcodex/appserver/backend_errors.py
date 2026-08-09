from __future__ import annotations

import re

from .client import AppServerError


class CodexBackendErrorMixin:
    _ACTIVE_WRITER_CONFLICT = re.compile(
        r"thread (?P<thread_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) "
        r"already has an active writer"
    )

    def _is_native_active_writer_conflict(
        self,
        error: AppServerError,
        *,
        expected_thread_id: str,
    ) -> bool:
        """Whether native Codex says another process owns the rollout writer."""

        if getattr(error, "code", None) != -32600:
            return False
        match = self._ACTIVE_WRITER_CONFLICT.fullmatch(str(error).strip().lower())
        return match is not None and match.group("thread_id") == expected_thread_id.lower()

    def _is_native_permission_profile_unsupported(self, error: AppServerError) -> bool:
        return self._is_unsupported_method_error(error)

    def _is_unsupported_method_error(self, error: AppServerError) -> bool:
        if getattr(error, "code", None) == -32601:
            return True
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "method not found",
                "unknown method",
                "not implemented",
                "unsupported method",
                "requires experimentalapi",
                "experimentalapi capability",
                "no handler",
            )
        )

    def _is_stale_thread_error(self, error: AppServerError) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "invalid request",
                "not found",
                "unknown thread",
                "no such thread",
                "no rollout found",
            )
        )

    def _requires_thread_resume(self, error: AppServerError) -> bool:
        message = str(error).lower()
        return self._is_stale_turn_error(error) or any(
            marker in message
            for marker in (
                "not loaded",
                "must resume",
                "thread closed",
            )
        )

    def _is_stale_turn_error(self, error: AppServerError) -> bool:
        message = str(error).lower()
        return self._is_stale_thread_error(error) or any(
            marker in message
            for marker in (
                "no active turn",
                "unknown turn",
                "no such turn",
                "turn not found",
                "expected turn",
            )
        )
