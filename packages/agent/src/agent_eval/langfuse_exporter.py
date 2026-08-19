"""Optional, fail-open Langfuse exporter for Agent Evaluation.

The local SQLite result is authoritative. Langfuse is only an observability
sink and is never imported or contacted unless explicitly enabled.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from agent_eval.evaluation_evidence import redact_evidence, redact_text


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class LangfuseExporter:
    def __init__(self, client: Any = None, export_content: bool = False):
        self.client = client
        self.export_content = export_content
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _safe_text(self, value: Any) -> Any:
        text = str(value or "")
        if self.export_content:
            return redact_text(text)
        return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "length": len(text)}

    def export_run(self, run: dict[str, Any]) -> bool:
        if not self.client:
            return False
        try:
            summary = run.get("summary") or {}
            with self.client.start_as_current_observation(
                as_type="agent",
                name="agent-evaluation-run",
                input={"run_id": run.get("run_id", "")},
                metadata={
                    "profile_counts": summary.get("profile_counts", {}),
                    "case_type_counts": summary.get("case_type_counts", {}),
                },
            ) as root:
                for result in run.get("results", []):
                    metrics = result.get("metrics") or {}
                    case_metadata = {
                        "case_key": result.get("case_key", ""),
                        "profile": result.get("profile") or "general",
                        "case_type": result.get("case_type") or "answer_quality",
                        "status": result.get("status", ""),
                    }
                    with root.start_as_current_observation(
                        as_type="span",
                        name="agent-evaluation-case",
                        input={"query": self._safe_text(result.get("query", ""))},
                        metadata=case_metadata,
                    ) as observation:
                        observation.update(
                            output={
                                "answer": self._safe_text(result.get("answer", "")),
                                "trace": redact_evidence(result.get("trace") or {}),
                                "elapsed_ms": result.get("elapsed_ms", 0),
                                "cost_estimate": (metrics.get("runtime") or {}).get("cost_estimate") or {},
                            }
                        )
                        for name, value in metrics.items():
                            if isinstance(value, bool):
                                observation.score(name=name, value=1.0 if value else 0.0)
                            elif isinstance(value, (int, float)):
                                observation.score(name=name, value=float(value))
                        for name, value in (metrics.get("judge") or {}).items():
                            if isinstance(value, bool):
                                observation.score(name=f"judge.{name}", value=1.0 if value else 0.0)
                            elif isinstance(value, (int, float)):
                                observation.score(name=f"judge.{name}", value=float(value))
            self.client.flush()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            try:
                self.client.flush()
            except Exception:
                pass
            return False


def build_langfuse_exporter() -> LangfuseExporter:
    """Build an exporter from env without making Langfuse a hard dependency."""
    if not _enabled(os.environ.get("LANGFUSE_ENABLED")):
        return LangfuseExporter()
    try:
        from langfuse import get_client

        return LangfuseExporter(
            client=get_client(),
            export_content=_enabled(os.environ.get("LANGFUSE_EXPORT_CONTENT")),
        )
    except Exception as exc:
        exporter = LangfuseExporter()
        exporter.last_error = str(exc)
        return exporter
