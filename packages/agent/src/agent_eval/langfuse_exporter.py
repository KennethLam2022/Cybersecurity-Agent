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
    def __init__(self, client: Any = None, export_content: bool = False, annotation_queue: str = ""):
        self.client = client
        self.export_content = export_content
        self.annotation_queue = annotation_queue
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _safe_text(self, value: Any) -> Any:
        text = str(value or "")
        if self.export_content:
            return redact_text(text)
        return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "length": len(text)}

    def verify_connection(self) -> dict[str, Any]:
        """Perform the SDK's credential check without exporting evaluation data."""
        if not self.client:
            return {"ok": False, "error": self.last_error or "Langfuse 未启用或 SDK 未安装"}
        if not hasattr(self.client, "auth_check"):
            return {"ok": False, "error": "当前 Langfuse SDK 不支持凭证校验"}
        try:
            if self.client.auth_check() is not True:
                return {"ok": False, "error": "Langfuse 未通过凭证校验"}
            return {"ok": True, "message": "已验证 Langfuse 网络连接与项目凭证"}
        except Exception as exc:
            self.last_error = str(exc)
            return {"ok": False, "error": f"Langfuse 连接或凭证校验失败：{self.last_error}"}

    def export_cases_to_dataset(self, cases: list[dict[str, Any]], dataset_name: str) -> dict[str, Any]:
        """Best-effort Dataset projection; SQLite remains the canonical test set."""
        if not self.client:
            return {"ok": False, "exported": 0, "error": self.last_error or "Langfuse 未启用"}
        if not hasattr(self.client, "create_dataset_item"):
            return {"ok": False, "exported": 0, "error": "当前 Langfuse SDK 不支持 Dataset API"}
        exported = 0
        try:
            for case in cases:
                query = case.get("query") or {}
                self.client.create_dataset_item(
                    dataset_name=dataset_name,
                    input=redact_evidence(query),
                    expected_output=redact_evidence(case.get("expected") or {}),
                    metadata={
                        "local_case_id": case.get("id"),
                        "case_key": case.get("case_key") or "",
                        "profile": case.get("profile") or "general",
                        "profile_version": case.get("profile_version") or "unknown",
                        "case_type": case.get("case_type") or "answer_quality",
                        "domain": case.get("domain") or "",
                    },
                )
                exported += 1
            self.client.flush()
            return {"ok": True, "exported": exported, "dataset_name": dataset_name}
        except Exception as exc:
            self.last_error = str(exc)
            try:
                self.client.flush()
            except Exception:
                pass
            return {"ok": False, "exported": exported, "error": self.last_error}

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
                    "annotation_queue_candidate": bool(self.annotation_queue),
                    "annotation_queue": self.annotation_queue,
                },
            ) as root:
                for result in run.get("results", []):
                    metrics = result.get("metrics") or {}
                    case_metadata = {
                        "case_key": result.get("case_key", ""),
                        "profile": result.get("profile") or "general",
                        "case_type": result.get("case_type") or "answer_quality",
                        "status": result.get("status", ""),
                        "annotation_queue_candidate": bool(self.annotation_queue),
                        "annotation_queue": self.annotation_queue,
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


def build_langfuse_exporter(config: dict | None = None) -> LangfuseExporter:
    """Build an exporter from env without making Langfuse a hard dependency."""
    config = config or {}
    enabled = config.get("enabled") if config else _enabled(os.environ.get("LANGFUSE_ENABLED"))
    if not enabled:
        return LangfuseExporter()
    try:
        from langfuse import Langfuse
        client = Langfuse(
            public_key=config.get("public_key") or os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
            secret_key=config.get("secret_key") or os.environ.get("LANGFUSE_SECRET_KEY", ""),
            base_url=config.get("host") or os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        return LangfuseExporter(
            client=client,
            export_content=bool(config.get("export_content")) if config else _enabled(os.environ.get("LANGFUSE_EXPORT_CONTENT")),
            annotation_queue=str(config.get("annotation_queue") or os.environ.get("LANGFUSE_ANNOTATION_QUEUE", "")).strip(),
        )
    except Exception as exc:
        exporter = LangfuseExporter()
        exporter.last_error = str(exc)
        return exporter
