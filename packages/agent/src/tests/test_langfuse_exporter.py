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

    def auth_check(self):
        return True


class FakeDatasetClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.items = []

    def create_dataset_item(self, **kwargs):
        self.items.append(kwargs)


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


def test_langfuse_content_export_is_redacted():
    client = FakeClient()
    exporter = LangfuseExporter(client=client, export_content=True)

    assert exporter._safe_text("mail a@example.com token Bearer abcdefghijk") == (
        "mail [REDACTED_EMAIL] token Bearer [REDACTED_TOKEN]"
    )


def test_langfuse_connection_check_uses_sdk_auth_check():
    assert LangfuseExporter(client=FakeClient()).verify_connection() == {
        "ok": True, "message": "已验证 Langfuse 网络连接与项目凭证"
    }


def test_langfuse_connection_check_reports_remote_failure():
    class BrokenClient(FakeClient):
        def auth_check(self):
            raise RuntimeError("invalid credentials")

    result = LangfuseExporter(client=BrokenClient()).verify_connection()
    assert result["ok"] is False
    assert "invalid credentials" in result["error"]


def test_langfuse_dataset_sync_is_best_effort_and_redacted():
    client = FakeDatasetClient()
    result = LangfuseExporter(client=client, export_content=True).export_cases_to_dataset([
        {
            "id": 1, "case_key": "GEN-1", "profile": "general", "case_type": "safety",
            "query": {"text": "联系 a@example.com"},
            "expected": {"forbidden": ["Bearer abcdefghijk"]},
        }
    ], "cyber-agent-eval-general")

    assert result == {"ok": True, "exported": 1, "dataset_name": "cyber-agent-eval-general"}
    assert client.items[0]["input"]["text"] == "联系 [REDACTED_EMAIL]"
    assert client.items[0]["expected_output"]["forbidden"] == ["Bearer [REDACTED_TOKEN]"]
