from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Type, TypeVar

from marketpulse_runtime import resolve_state_dir


class RecordStatus(StrEnum):
    NEW = "new"
    WATCHING = "watching"
    PROVEN = "proven"
    REJECTED = "rejected"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(slots=True)
class OutcomeEvent:
    bot_id: str
    bot_type: str
    category: str
    action_taken: str
    event_id: str = field(default_factory=lambda: _new_id("evt"))
    timestamp: str = field(default_factory=_utc_now)
    symbol: str = ""
    regime: str = ""
    position_context: dict[str, Any] = field(default_factory=dict)
    pnl_impact: float = 0.0
    runtime_impact: str = ""
    source_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BeliefSnapshot:
    event_id: str
    direction: str
    score: float
    confidence: float
    belief_id: str = field(default_factory=lambda: _new_id("belief"))
    market_context: dict[str, Any] = field(default_factory=dict)
    risk_context: dict[str, Any] = field(default_factory=dict)
    signal_components: list[dict[str, Any]] = field(default_factory=list)
    reason_text: str = ""
    timestamp: str = field(default_factory=_utc_now)


@dataclass(slots=True)
class LearningRecord:
    event_id: str
    learning_type: str
    claim: str
    suggested_change: str
    confidence: float
    scope: str
    learning_id: str = field(default_factory=lambda: _new_id("learn"))
    created_at: str = field(default_factory=_utc_now)
    status: str = RecordStatus.NEW.value


@dataclass(slots=True)
class ProofRecord:
    learning_id: str
    validation_window: str
    cases_seen: int
    wins_after_change: int
    losses_after_change: int
    false_positives_after_change: int
    net_effect: float
    proof_status: str
    proof_id: str = field(default_factory=lambda: _new_id("proof"))
    notes: str = ""
    created_at: str = field(default_factory=_utc_now)


T = TypeVar("T", OutcomeEvent, BeliefSnapshot, LearningRecord, ProofRecord)


class BotIntelligenceStore:
    def __init__(self, state_dir: str | Path | None = None):
        self.state_dir = resolve_state_dir(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._files = {
            OutcomeEvent: self.state_dir / "intelligence_outcomes.jsonl",
            BeliefSnapshot: self.state_dir / "intelligence_beliefs.jsonl",
            LearningRecord: self.state_dir / "intelligence_learning.jsonl",
            ProofRecord: self.state_dir / "intelligence_proofs.jsonl",
        }

    def append_outcome(self, record: OutcomeEvent) -> OutcomeEvent:
        self._append(record)
        return record

    def append_belief(self, record: BeliefSnapshot) -> BeliefSnapshot:
        self._append(record)
        return record

    def append_learning(self, record: LearningRecord) -> LearningRecord:
        self._append(record)
        return record

    def append_proof(self, record: ProofRecord) -> ProofRecord:
        self._append(record)
        return record

    def list_outcomes(self, bot_id: str | None = None) -> list[OutcomeEvent]:
        rows = self._read(OutcomeEvent)
        return [row for row in rows if bot_id is None or row.bot_id == bot_id]

    def list_beliefs(self, event_id: str | None = None) -> list[BeliefSnapshot]:
        rows = self._read(BeliefSnapshot)
        return [row for row in rows if event_id is None or row.event_id == event_id]

    def list_learning(self, status: str | None = None) -> list[LearningRecord]:
        rows = self._read(LearningRecord)
        return [row for row in rows if status is None or row.status == status]

    def list_proofs(self, learning_id: str | None = None) -> list[ProofRecord]:
        rows = self._read(ProofRecord)
        return [row for row in rows if learning_id is None or row.learning_id == learning_id]

    def _append(self, record: T) -> None:
        path = self._files[type(record)]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), sort_keys=True, default=str) + "\n")

    def _read(self, record_type: Type[T]) -> list[T]:
        path = self._files[record_type]
        if not path.exists():
            return []
        items = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                items.append(record_type(**json.loads(line)))
        return items
