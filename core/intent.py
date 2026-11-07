from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class IntentResult:
    task_type: str
    confidence: float
    keywords_matched: list[str]
    suggested_models: list[str] = None


class IntentClassifier:
    """Classifies user input into task types for model routing."""

    # Task type definitions with keywords and preferred model tags
    TASK_DEFINITIONS = {
        "coding": {
            "keywords": [
                "code",
                "program",
                "function",
                "class",
                "debug",
                "refactor",
                "implement",
                "script",
                "api",
                "endpoint",
                "database",
                "sql",
                "python",
                "javascript",
                "typescript",
                "rust",
                "go",
                "java",
                "html",
                "css",
                "react",
                "vue",
                "node",
                "docker",
                "kubernetes",
                "git",
                "github",
                "gitlab",
                "ci/cd",
                "test",
                "pytest",
                "jest",
                "variable",
                "loop",
                "condition",
                "algorithm",
                "data structure",
                "library",
                "framework",
                "package",
                "module",
                "import",
                "error",
                "exception",
                "traceback",
                "bug",
                "fix",
                "patch",
            ],
            "model_tags": ["coding", "reasoning"],
            "preferred_providers": [
                "xkiro-qwen38-max",
                "xkiro-qwen-coder-plus",
                "xkiro-mistral-large",
                "xkiro-qwen3-max",
            ],
        },
        "reasoning": {
            "keywords": [
                "solve",
                "calculate",
                "math",
                "equation",
                "proof",
                "logic",
                "reason",
                "think step by step",
                "analyze",
                "compare",
                "evaluate",
                "optimize",
                "algorithm",
                "complex",
                "deduce",
                "infer",
                "conclude",
                "derive",
                "theorem",
                "hypothesis",
            ],
            "model_tags": ["reasoning", "thinking"],
            "preferred_providers": [
                "xkiro-qwen38-max",
                "xkiro-qwen3-max",
                "xkiro-mistral-large",
                "gemini-3.5-flash",
                "xkiro-qwen35-397b",
            ],
        },
        "vision": {
            "keywords": [
                "screenshot",
                "screen",
                "image",
                "photo",
                "picture",
                "camera",
                "vision",
                "ocr",
                "read screen",
                "what's on",
                "look at",
                "analyze image",
                "describe image",
                "vision",
                "visual",
                "diagram",
                "chart",
                "graph",
                "ui",
                "interface",
                "mockup",
            ],
            "model_tags": ["vision", "multimodal"],
            "preferred_providers": [
                "gemini-3.5-flash",
                "gemini-3.6-flash",
            ],
        },
        "research": {
            "keywords": [
                "research",
                "find",
                "search",
                "look up",
                "investigate",
                "explore",
                "survey",
                "literature",
                "paper",
                "study",
                "compare options",
                "alternatives",
                "best practices",
                "state of the art",
                "latest",
                "recent",
                "trends",
            ],
            "model_tags": ["general", "reasoning"],
            "preferred_providers": [
                "gemini-3.5-flash",
                "xkiro-qwen38-max",
                "xkiro-qwen36-plus",
                "gemini-3.6-flash",
            ],
        },
        "analysis": {
            "keywords": [
                "analyze",
                "analysis",
                "examine",
                "inspect",
                "review",
                "audit",
                "assess",
                "evaluate",
                "critique",
                "breakdown",
                "patterns",
                "trends",
                "anomalies",
                "outliers",
                "correlation",
                "statistics",
                "metrics",
                "performance",
                "bottleneck",
            ],
            "model_tags": ["reasoning", "general"],
            "preferred_providers": [
                "xkiro-qwen3-max",
                "xkiro-qwen38-max",
                "xkiro-mistral-large",
                "gemini-3.5-flash",
            ],
        },
        "creative": {
            "keywords": [
                "brainstorm",
                "ideate",
                "creative",
                "design",
                "invent",
                "imagine",
                "concept",
                "prototype",
                "mockup",
                "sketch",
                "story",
                "narrative",
                "write",
                "compose",
                "poem",
                "marketing",
                "copy",
                "slogan",
                "branding",
            ],
            "model_tags": ["creative", "general"],
            "preferred_providers": [
                "xkiro-qwen37-plus",
                "gemini-3.5-flash",
                "xkiro-qwen38-max",
                "xkiro-mistral-medium",
            ],
        },
        "planning": {
            "keywords": [
                "plan",
                "schedule",
                "organize",
                "arrange",
                "structure",
                "roadmap",
                "timeline",
                "milestone",
                "task",
                "step",
                "break down",
                "decompose",
                "prioritize",
                "strategy",
            ],
            "model_tags": ["reasoning", "general"],
            "preferred_providers": [
                "xkiro-mistral-large",
                "xkiro-qwen38-max",
                "xkiro-qwen3-max",
                "gemini-3.6-flash",
            ],
        },
        "explanation": {
            "keywords": [
                "explain",
                "how does",
                "why does",
                "what is",
                "what are",
                "define",
                "describe",
                "clarify",
                "elaborate",
                "teach",
                "tutorial",
                "guide",
                "walkthrough",
                "overview",
            ],
            "model_tags": ["general", "reasoning"],
            "preferred_providers": [
                "gemini-3.5-flash",
                "xkiro-qwen38-max",
                "xkiro-qwen36-plus",
                "xkiro-qwen-coder-plus",
            ],
        },
        "fast": {
            "keywords": [
                "quick",
                "fast",
                "simple",
                "short",
                "brief",
                "summary",
                "tl;dr",
                "one line",
                "yes/no",
                "true/false",
                "just",
            ],
            "model_tags": ["fast", "fastest", "lightweight"],
            "preferred_providers": [
                "xkiro-qwen37-flash",
                "xkiro-qwen-coder-plus",
                "xkiro-minimax-m27-highspeed",
                "xkiro-minimax-m21-highspeed",
            ],
        },
    }

    DEFAULT_TASK = "general"
    GENERAL_PROVIDERS = [
        "xkiro-minimax-m27-highspeed",
        "xkiro-qwen38-max",
        "gemini-3.5-flash",
        "xkiro-qwen36-plus",
    ]

    def __init__(self):
        # Compile keyword patterns for faster matching
        self._compiled_patterns = {}
        for task_type, defn in self.TASK_DEFINITIONS.items():
            pattern = r"\b(" + "|".join(re.escape(kw) for kw in defn["keywords"]) + r")\b"
            self._compiled_patterns[task_type] = re.compile(pattern, re.IGNORECASE)

    def classify(self, text: str, context: dict = None) -> IntentResult:
        """Classify the intent of the user input."""
        if not text:
            return IntentResult(
                task_type=self.DEFAULT_TASK,
                confidence=0.5,
                keywords_matched=[],
                suggested_models=self.GENERAL_PROVIDERS,
            )

        lower = text.lower()
        scores = {}
        all_matched = {}

        # Score each task type
        for task_type, compiled in self._compiled_patterns.items():
            matches = compiled.findall(lower)
            if matches:
                # Count unique matches
                unique_matches = {m.lower() for m in matches}
                scores[task_type] = len(unique_matches)
                all_matched[task_type] = list(unique_matches)

        # Context boost
        if context:
            prev_task = context.get("previous_task_type")
            if prev_task and prev_task in scores:
                scores[prev_task] = scores.get(prev_task, 0) + 1  # Continuity bonus

            # Dialogue manager hints
            if "intent" in context and context["intent"] in scores:
                scores[context["intent"]] = scores.get(context["intent"], 0) + 1

        # Determine best match
        if scores:
            best_task = max(scores, key=scores.get)
            max_score = scores[best_task]
            total_matches = sum(scores.values())
            confidence = min(0.95, max_score / max(total_matches, 1) + 0.3)
            keywords = all_matched.get(best_task, [])
            suggested = self.TASK_DEFINITIONS[best_task]["preferred_providers"]
        else:
            best_task = self.DEFAULT_TASK
            confidence = 0.5
            keywords = []
            suggested = self.GENERAL_PROVIDERS

        return IntentResult(
            task_type=best_task,
            confidence=confidence,
            keywords_matched=keywords,
            suggested_models=suggested,
        )

    def get_task_tags(self, task_type: str) -> list[str]:
        """Get model tags for a task type."""
        return self.TASK_DEFINITIONS.get(task_type, {}).get("model_tags", ["general"])

    def get_preferred_providers(self, task_type: str) -> list[str]:
        """Get preferred provider names for a task type."""
        return self.TASK_DEFINITIONS.get(task_type, {}).get(
            "preferred_providers", self.GENERAL_PROVIDERS
        )
