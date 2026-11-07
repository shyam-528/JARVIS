from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field


@dataclass
class ConversationThread:
    topic: str
    entities: list[str] = field(default_factory=list)  # Key nouns: names, projects, files
    start_turn: int = 0
    last_turn: int = 0
    summary: str = ""


@dataclass
class DialogueState:
    """Tracks conversation state for context continuity."""

    threads: list[ConversationThread] = field(default_factory=list)
    current_thread: ConversationThread | None = None
    pending_questions: list[str] = field(default_factory=list)
    turn_count: int = 0
    last_user_intent: str = ""
    last_assistant_action: str = ""


class DialogueManager:
    """Manages dialogue state, topic tracking, and reference resolution."""

    # Patterns for detecting entity references
    ENTITY_PATTERNS = [
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b",  # Capitalized names
        r"\b(?:project|repo|repository|codebase|app|service)\s+(\w+)",  # project X
        r"\b(?:file|script|module)\s+(\w+(?:\.\w+)?)",  # file X
        r"\b(?:function|method|class)\s+(\w+)",  # function X
    ]

    # Topic shift indicators
    TOPIC_SHIFT_PATTERNS = [
        r"^(?:by the way|btw|anyway|speaking of|on another note)",
        r"^(?:new topic|different subject|change of subject)",
        r"^(?:so|okay|right|well),?\s+(?:about|regarding|concerning)",
    ]

    def __init__(self, max_threads: int = 5, max_history: int = 100):
        self.state = DialogueState()
        self.max_threads = max_threads
        self.max_history = max_history
        self.recent_user_messages: deque[str] = deque(maxlen=max_history)
        self.recent_assistant_messages: deque[str] = deque(maxlen=max_history)

    def process_user_message(self, message: str) -> dict:
        """Process incoming user message, update state, return context hints."""
        self.state.turn_count += 1
        self.recent_user_messages.append(message)

        # Detect topic shift
        if self._detect_topic_shift(message):
            self._start_new_thread(message)

        # Extract entities
        entities = self._extract_entities(message)
        if self.state.current_thread:
            for entity in entities:
                if entity not in self.state.current_thread.entities:
                    self.state.current_thread.entities.append(entity)
            self.state.current_thread.last_turn = self.state.turn_count

        # Detect pending questions
        if "?" in message and not message.strip().startswith("?"):
            self.state.pending_questions.append(message.strip())

        # Detect intent (simple keyword-based)
        intent = self._classify_intent(message)
        self.state.last_user_intent = intent

        return {
            "entities": entities,
            "intent": intent,
            "topic": self.state.current_thread.topic if self.state.current_thread else "",
            "pending_questions": list(self.state.pending_questions)[-3:],
        }

    def process_assistant_message(self, message: str, tool_calls: list = None):
        """Process assistant response, track actions taken."""
        self.recent_assistant_messages.append(message)

        if tool_calls:
            for tc in tool_calls:
                # Handle both tuple format (cid, name, args) and dict format
                if isinstance(tc, tuple):
                    tool_name = tc[1] if len(tc) > 1 else ""
                else:
                    tool_name = tc.get("function", {}).get("name", "")
                if tool_name:
                    self.state.last_assistant_action = tool_name

    def _detect_topic_shift(self, message: str) -> bool:
        """Check if message indicates a topic change."""
        lower = message.lower().strip()
        for pattern in self.TOPIC_SHIFT_PATTERNS:
            if re.search(pattern, lower, re.IGNORECASE):
                return True
        return False

    def _start_new_thread(self, message: str):
        """Start a new conversation thread."""
        # Summarize previous thread if it had content
        if self.state.current_thread and self.state.current_thread.entities:
            self.state.current_thread.summary = self._summarize_thread(self.state.current_thread)
            self.state.threads.append(self.state.current_thread)
            # Trim old threads
            if len(self.state.threads) > self.max_threads:
                self.state.threads = self.state.threads[-self.max_threads :]

        # Create new thread
        topic = self._extract_topic(message) or f"Topic {len(self.state.threads) + 1}"
        self.state.current_thread = ConversationThread(
            topic=topic,
            start_turn=self.state.turn_count,
            last_turn=self.state.turn_count,
        )

    def _extract_topic(self, message: str) -> str | None:
        """Extract a topic label from the message."""
        # Simple heuristic: first noun phrase after common starters
        starters = [
            "about ",
            "regarding ",
            "concerning ",
            "on ",
            "for ",
            "how to ",
            "what is ",
            "what are ",
            "tell me ",
        ]
        lower = message.lower()
        for starter in starters:
            idx = lower.find(starter)
            if idx >= 0:
                rest = message[idx + len(starter) :].strip()
                # Take first 5 words
                words = rest.split()[:5]
                if words:
                    return " ".join(words).rstrip("?.,!")
        # Fallback: first noun phrase
        match = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", message)
        if match:
            return match.group(1)
        return None

    def _extract_entities(self, message: str) -> list[str]:
        """Extract key entities (names, projects, files, etc.) from message."""
        entities = []
        for pattern in self.ENTITY_PATTERNS:
            matches = re.findall(pattern, message)
            entities.extend(matches)
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for e in entities:
            e_clean = e.strip()
            if e_clean and e_clean.lower() not in seen:
                seen.add(e_clean.lower())
                unique.append(e_clean)
        return unique[:10]  # Limit

    def _classify_intent(self, message: str) -> str:
        """Simple intent classification."""
        lower = message.lower()
        if any(w in lower for w in ["code", "program", "function", "debug", "implement", "script"]):
            return "coding"
        if any(w in lower for w in ["analyze", "compare", "evaluate", "assess", "review"]):
            return "analysis"
        if any(w in lower for w in ["research", "find", "search", "look up", "investigate"]):
            return "research"
        if any(w in lower for w in ["create", "make", "build", "generate", "write"]):
            return "creation"
        if any(w in lower for w in ["explain", "how", "why", "what", "define"]):
            return "explanation"
        if any(w in lower for w in ["plan", "schedule", "organize", "arrange"]):
            return "planning"
        return "general"

    def _summarize_thread(self, thread: ConversationThread) -> str:
        """Create a brief summary of a thread."""
        parts = [thread.topic]
        if thread.entities:
            parts.append(f"Entities: {', '.join(thread.entities[:5])}")
        return " | ".join(parts)

    def get_context_for_prompt(self) -> str:
        """Generate context string to inject into system prompt."""
        parts = []
        if self.state.current_thread:
            parts.append(f"Current topic: {self.state.current_thread.topic}")
            if self.state.current_thread.entities:
                parts.append(
                    f"Active entities: {', '.join(self.state.current_thread.entities[:5])}"
                )
        if self.state.pending_questions:
            parts.append(f"Pending questions: {len(self.state.pending_questions)}")
        if self.state.last_user_intent:
            parts.append(f"User intent: {self.state.last_user_intent}")
        return " | ".join(parts) if parts else ""

    def resolve_reference(self, reference: str) -> str | None:
        """Resolve vague references like 'that file', 'the project', 'it'."""
        lower = reference.lower().strip()

        # Direct entity match in current thread
        if self.state.current_thread:
            for entity in self.state.current_thread.entities:
                if entity.lower() in lower or lower in entity.lower():
                    return entity

        # Common reference patterns
        if lower in ("that file", "the file", "it") and self.state.current_thread:
            for entity in self.state.current_thread.entities:
                if entity.endswith((".py", ".js", ".ts", ".json", ".md", ".txt")):
                    return entity

        if lower in ("that project", "the project", "my project") and self.state.current_thread:
            for entity in self.state.current_thread.entities:
                if not entity.endswith((".py", ".js", ".ts", ".json", ".md", ".txt")):
                    return entity

        return None

    def clear_pending_question(self, question: str):
        """Mark a pending question as addressed."""
        if question in self.state.pending_questions:
            self.state.pending_questions.remove(question)
