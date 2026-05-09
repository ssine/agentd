from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ChannelCapabilities:
    supports_threads: bool = False
    supports_child_threads: bool = False
    supports_card_actions: bool = False
    supports_message_update: bool = False
    supports_markdown: bool = True
    delivery_modes: tuple[str, ...] = ('text',)


@dataclass(frozen=True)
class ChannelEnvelope:
    channel: str
    conversation_ref: str
    message_ref: str
    text: str
    sender_ref: str = ''
    sender_name: str = ''
    thread_ref: str = ''
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ControlCommand:
    command_type: str
    channel: str
    conversation_ref: str
    message_ref: str
    text: str = ''
    thread_ref: str = ''
    sender_ref: str = ''
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryRequest:
    channel: str
    destination_ref: str
    kind: str
    payload: dict[str, Any]
    thread_ref: str = ''
    dedupe_key: str = ''


class ChannelAdapter(Protocol):
    kind: str
    capabilities: ChannelCapabilities

    def submit_message(self, envelope: ChannelEnvelope) -> ControlCommand:
        ...
