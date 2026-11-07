"""ML proactive tools: record activity, fetch pattern-based suggestions,
anomaly checks, training and stats. Registered only with a live ML engine.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from core.ml_proactive import MLFeatureVector, MLProactiveEngine, SuggestionType
from core.tools import Tool, ToolBox


def register_ml_proactive(box: ToolBox, ml_engine: MLProactiveEngine):
    async def ml_record_action(
        action_type: str,
        tool_used: str,
        duration_ms: int = 0,
        success: bool = True,
        context: str = "{}",
    ) -> str:
        """Record an action for ML pattern learning."""
        try:
            ctx = json.loads(context) if context else {}
        except json.JSONDecodeError:
            ctx = {}
        ml_engine.record_action(action_type, tool_used, duration_ms, success, ctx)
        return f"Recorded action: {action_type} using {tool_used}"

    async def ml_get_suggestions(
        context: str = "{}",
        include_anomalies: bool = True,
        include_predictions: bool = True,
        include_patterns: bool = True,
    ) -> str:
        """Get ML-based proactive suggestions."""
        try:
            ctx = json.loads(context) if context else {}
        except json.JSONDecodeError:
            ctx = {}

        suggestions = ml_engine.get_suggestions(ctx)

        filtered = []
        for s in suggestions:
            if s.type == SuggestionType.ANOMALY and not include_anomalies:
                continue
            if s.type == SuggestionType.PREDICTION and not include_predictions:
                continue
            if s.type == SuggestionType.HABIT and not include_patterns:
                continue
            filtered.append(s)

        if not filtered:
            return "No suggestions at this time."

        lines = []
        for s in filtered:
            lines.append(
                f"- [{s.type.value.upper()}] {s.title}: {s.description} (confidence: {s.confidence:.0%})"
            )
        return "ML Proactive Suggestions:\n" + "\n".join(lines)

    async def ml_record_activity(
        action_type: str,
        tool: str,
        duration_ms: int = 0,
        success: bool = True,
    ) -> str:
        """Quick activity recording for pattern learning."""
        ml_engine.record_action(action_type, tool, duration_ms, success)
        return f"Recorded: {action_type} via {tool}"

    async def ml_get_stats() -> str:
        """Get ML engine statistics."""
        stats = ml_engine.get_stats()
        return "ML Engine Stats: " + ", ".join(f"{k}={v}" for k, v in stats.items())

    async def ml_analyze_anomaly(
        action_type: str,
        tool_used: str,
        duration_ms: int = 0,
        success: bool = True,
    ) -> str:
        """Check if an action is anomalous (real detector call)."""
        now = datetime.now()
        fv = MLFeatureVector(
            timestamp=time.time(),
            hour_of_day=now.hour,
            day_of_week=now.weekday(),
            action_type=action_type,
            tool_used=tool_used,
            duration_ms=duration_ms,
            success=success,
        )
        result = ml_engine.behavior_analyzer.detect_anomaly(fv)
        if result is None:
            ba = ml_engine.behavior_analyzer
            return (
                f"Anomaly detector not ready "
                f"({len(ba.action_history)}/{ba.min_samples_for_training} samples, or sklearn missing)."
            )
        verdict = "ANOMALOUS" if result.is_anomaly else "normal"
        detail = ", ".join(
            f"{k}={v:.2f}"
            for k, v in sorted(result.feature_contributions.items(), key=lambda kv: -abs(kv[1]))[:3]
        )
        return f"Action is {verdict} (score={result.score:.3f}). Top factors: {detail}"

    async def ml_train() -> str:
        """Trigger manual model retraining (real engine call)."""
        ba = ml_engine.behavior_analyzer
        trained_anomaly = await ba.train_anomaly_detector()
        trained_predictor = ml_engine.prediction_engine.train()
        return (
            f"Training on {len(ba.action_history)} samples: "
            f"anomaly detector {'trained' if trained_anomaly else 'not trained (insufficient samples or sklearn missing)'}; "
            f"predictor {'trained' if trained_predictor else 'not trained (insufficient labeled history)'}"
        )

    box.register(
        Tool(
            name="ml_record",
            description="Record an action for ML pattern learning",
            parameters={
                "type": "object",
                "properties": {
                    "action_type": {
                        "type": "string",
                        "description": "Type of action (e.g., 'search', 'code', 'write', 'read')",
                    },
                    "tool_used": {
                        "type": "string",
                        "description": "Tool used (e.g., 'web_search', 'screenshot', 'code_editor')",
                    },
                    "duration_ms": {
                        "type": "integer",
                        "default": 0,
                        "description": "Duration in milliseconds",
                    },
                    "success": {"type": "boolean", "default": True},
                    "context": {"type": "string", "default": "{}", "description": "JSON context"},
                },
                "required": ["action_type", "tool_used"],
            },
            handler=ml_record_action,
        )
    )
    box.register(
        Tool(
            name="ml_suggestions",
            description="Get ML-based proactive suggestions",
            parameters={
                "type": "object",
                "properties": {
                    "context": {"type": "string", "default": "{}", "description": "JSON context"},
                    "include_anomalies": {"type": "boolean", "default": True},
                    "include_predictions": {"type": "boolean", "default": True},
                    "include_patterns": {"type": "boolean", "default": True},
                },
                "required": [],
            },
            handler=ml_get_suggestions,
        )
    )
    box.register(
        Tool(
            name="ml_record_activity",
            description="Quick activity recording for pattern learning",
            parameters={
                "type": "object",
                "properties": {
                    "action_type": {"type": "string"},
                    "tool": {"type": "string"},
                    "duration_ms": {"type": "integer", "default": 0},
                    "success": {"type": "boolean", "default": True},
                },
                "required": ["action_type", "tool"],
            },
            handler=ml_record_activity,
        )
    )
    box.register(
        Tool(
            name="ml_anomaly_check",
            description="Check if an action is anomalous",
            parameters={
                "type": "object",
                "properties": {
                    "action_type": {"type": "string"},
                    "tool_used": {"type": "string"},
                    "duration_ms": {"type": "integer", "default": 0},
                    "success": {"type": "boolean", "default": True},
                },
                "required": ["action_type", "tool_used"],
            },
            handler=ml_analyze_anomaly,
        )
    )
    box.register(
        Tool(
            name="ml_train",
            description="Trigger manual model retraining",
            parameters={"type": "object", "properties": {}},
            handler=ml_train,
        )
    )
    box.register(
        Tool(
            name="ml_stats",
            description="Get ML engine statistics",
            parameters={"type": "object", "properties": {}},
            handler=ml_get_stats,
        )
    )
