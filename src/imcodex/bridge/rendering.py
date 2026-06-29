from __future__ import annotations

import json

from ..appserver import AppServerError


class BridgeRenderingMixin:
    def _render_config(self, payload: dict, key_path: object | None) -> str:
        config = payload.get("config")
        if key_path is not None and isinstance(config, dict):
            return self._render_json(self._lookup_config_value(config, str(key_path)))
        return self._render_json(config if config is not None else payload)

    def _render_goal(self, payload: dict) -> str:
        goal = payload.get("goal") if isinstance(payload, dict) else None
        if not isinstance(goal, dict):
            return "No goal currently set."
        status = self._goal_status_label(str(goal.get("status") or ""))
        lines = [f"Goal {status}".strip()]
        objective = str(goal.get("objective") or "").strip()
        if objective:
            lines.append(f"Objective: {objective}")
        time_used = self._int_or_none(goal.get("timeUsedSeconds"))
        if time_used and time_used > 0:
            lines.append(f"Time: {self._format_goal_elapsed_seconds(time_used)}")
        token_budget = self._int_or_none(goal.get("tokenBudget"))
        tokens_used = self._int_or_none(goal.get("tokensUsed")) or 0
        if token_budget is not None:
            lines.append(
                f"Tokens: {self._format_compact_number(tokens_used)}/{self._format_compact_number(token_budget)}"
            )
        return "\n".join(lines)

    def _goal_status_label(self, status: str) -> str:
        labels = {
            "active": "active",
            "paused": "paused",
            "blocked": "blocked",
            "usageLimited": "usage limited",
            "budgetLimited": "limited by budget",
            "complete": "complete",
        }
        return labels.get(status, status or "updated")

    def _format_goal_elapsed_seconds(self, seconds: int) -> str:
        seconds = max(0, seconds)
        if seconds < 60:
            return f"{seconds}s"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        remaining_minutes = minutes % 60
        if hours >= 24:
            days = hours // 24
            remaining_hours = hours % 24
            return f"{days}d {remaining_hours}h {remaining_minutes}m"
        if remaining_minutes == 0:
            return f"{hours}h"
        return f"{hours}h {remaining_minutes}m"

    def _format_compact_number(self, value: int) -> str:
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
        if abs(value) >= 1_000:
            return f"{value / 1_000:.1f}K".replace(".0K", "K")
        return str(value)

    def _int_or_none(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _lookup_config_value(self, payload: dict, key_path: str) -> object:
        current: object = payload
        for part in key_path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _render_json(self, payload: object) -> str:
        return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)

    def _safe_appserver_error(self, error: AppServerError) -> str:
        return self._safe_exception_text(error)

    def _safe_exception_text(self, error: Exception) -> str:
        text = " ".join(str(error).split())
        lowered = text.lower()
        if not text:
            return "unexpected upstream error"
        if any(marker in lowered for marker in ("<html", "<!doctype", "</html", "separator is not found", "chunk exceed the limit")):
            return "unexpected upstream error"
        if len(text) > 180:
            return "unexpected upstream error"
        return text
