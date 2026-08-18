from agent_eval.langfuse_exporter import LangfuseExporter, build_langfuse_exporter


class FakeObservation:
    def __init__(self):
        self.scores = []
        self.updated = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def start_as_current_observation(self, **_kwargs):
        return FakeObservation()

    def update(self, **kwargs):
        self.updated.append(kwargs)

    def score(self, **kwargs):
        self.scores.append(kwargs)


class FakeClient:
    def __init__(self):
        self.root = FakeObservation()
        self.flushed = False

    def start_as_current_observation(self, **_kwargs):
        return self.root

    def flush(self):
        self.flushed = True


def test_langfuse_exporter_is_fail_open_and_redacts_content():
    client = FakeClient()
    exporter = LangfuseExporter(client=client)
    ok = exporter.export_run({
        "run_id": "run-1", "summary": {"profile_counts": {"general": 1}},
        "results": [{
            "case_key": "GEN-1", "profile": "general", "case_type": "router",
            "query": "敏感问题", "answer": "敏感回答", "trace": {}, "elapsed_ms": 10,
            "status": "passed", "metrics": {"task_success": True, "judge": {"faithfulness": 0.8}},
        }],
    })

    assert ok is True
    assert client.flushed is True
    assert client.root.start_as_current_observation


def test_langfuse_exporter_disabled_without_flag(monkeypatch):
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)
    exporter = build_langfuse_exporter()
    assert exporter.enabled is False
