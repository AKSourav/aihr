from datetime import datetime, timezone

from livekit.agents import AgentSession
from livekit.agents.llm import ChatMessage
from livekit.agents.voice.events import ConversationItemAddedEvent

from core.schemas import Turn

ROLE_TO_SPEAKER = {"assistant": "agent", "user": "candidate"}


class TranscriptCollector:
    def __init__(self) -> None:
        self.turns: list[Turn] = []

    def attach(self, session: AgentSession) -> None:
        session.on("conversation_item_added", self._on_item_added)

    def _on_item_added(self, event: ConversationItemAddedEvent) -> None:
        item = event.item
        if not isinstance(item, ChatMessage):
            return
        speaker = ROLE_TO_SPEAKER.get(item.role)
        if speaker is None:
            return
        text = item.text_content
        if not text:
            return
        self.turns.append(
            Turn(
                speaker=speaker,
                text=text,
                started_at=datetime.fromtimestamp(event.created_at, tz=timezone.utc),
            )
        )
