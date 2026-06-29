from __future__ import annotations

from .client import AppServerError


class CodexBackendErrorMixin:
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
        return self._is_stale_thread_error(error) or any(
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
