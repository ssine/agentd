from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .models import AgentSession
from .runners import AgentRunControl


@dataclass
class ActiveRun:
    run_id: int
    session: AgentSession
    control: AgentRunControl
    done: threading.Event = field(default_factory=threading.Event)
    handoff_child_session_id: int | None = None
