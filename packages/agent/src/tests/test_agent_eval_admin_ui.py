from pathlib import Path


ADMIN_HTML = Path(__file__).parents[1] / "templates" / "admin.html"
STATIC_DIR = Path(__file__).parents[1] / "static"


def test_management_pages_do_not_retain_legacy_shared_management_authentication():
    pages = [ADMIN_HTML, *STATIC_DIR.glob("*.html")]
    forbidden_fragments = ("X-" + "Admin-Token", "admin" + "_token", "url" + "Token")

    for page in pages:
        content = page.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            assert fragment not in content, f"{page.name} still contains {fragment}"


def test_browser_management_pages_do_not_store_auth_tokens_in_local_storage():
    pages = [ADMIN_HTML, *STATIC_DIR.glob("*.html")]
    forbidden = ("securenexus_user_token", "USER_TOKEN_KEY", "localStorage.setItem('securenexus_user_token'")
    for page in pages:
        content = page.read_text(encoding="utf-8")
        for fragment in forbidden:
            assert fragment not in content, f"{page.name} still stores browser auth token: {fragment}"


def test_workspace_admin_exposes_sensitive_access_audit_view():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        'onclick="showAuditLogs(',
        "function showAuditLogs(tenantId, workspaceName)",
        "/api/admin/audit-logs?",
        "管理员审计复核",
    ):
        assert fragment in html, fragment


def test_workspace_admin_exposes_tenant_scoped_industry_profile_controls():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        "网络安全行业扩展",
        'id="industryProfileTenant"',
        'id="industryProfileList"',
        "function loadIndustryProfiles()",
        "function toggleIndustryProfile(profile, enabled)",
        "/api/admin/governance/industry-profiles",
        "访问控制始终优先于 Profile 过滤",
    ):
        assert fragment in html, fragment


def test_agent_eval_admin_exposes_judge_replay_and_calibration_controls():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    required_fragments = (
        'id="agentEvalJudgeLimit"',
        'id="btnRerunAgentJudge"',
        "function rerunAgentJudge()",
        "function saveAgentEvalHumanReview",
        "function showAgentEvalCalibration",
        "/api/agent-eval/runs/' + encodeURIComponent(currentAgentEvalRunId) + '/judge",
        "/reviews",
        "/calibration",
        "function checkReleaseReadiness()",
        "/api/agent-eval/release-readiness",
        "发布就绪检查",
    )
    for fragment in required_fragments:
        assert fragment in html, fragment


def test_admin_test_pages_explain_their_purpose():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        '本测试的目的：</strong>验证系统能否从知识库召回正确的网络安全资料',
        '本测试的目的：</strong>从用户问题、检索上下文到最终回答进行端到端检查',
        '本测试的目的：</strong>验证 Agent 的路由、检索、回答、安全防护和多轮记忆',
        '本工作台的目的：</strong>查看、修改和管理 System Prompt 版本',
    ):
        assert fragment in html, fragment


def test_prompt_admin_exposes_offline_ab_comparison_controls():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        'id="promptViewAb"',
        'id="promptAbVersionA"',
        'id="promptAbVersionB"',
        "function runPromptAbTest()",
        "/api/prompt/ab/run",
        "运行不会切换线上版本",
    ):
        assert fragment in html, fragment


def test_admin_navigation_is_grouped_by_operational_workflow():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    groups = ("operations", "knowledge", "configuration", "quality")
    positions = [html.index(f'data-group="{group}"') for group in groups]
    assert positions == sorted(positions)
    assert 'class="primary-nav-item active"' in html
    assert "function switchPrimaryGroup(group)" in html
    assert "function syncPrimaryGroup(group)" in html
    assert "const primaryGroupTabs = {" in html
    assert 'data-tab="documents"' in html
    assert 'data-tab="agentEval"' in html
    assert 'aria-selected' in html
    assert 'const tabContext = {' in html


def test_key_pages_expose_keyboard_focus_and_escape_interactions():
    admin = ADMIN_HTML.read_text(encoding="utf-8")
    index = (ADMIN_HTML.parent.parent / "templates" / "index.html").read_text(encoding="utf-8")
    chat = (ADMIN_HTML.parent.parent / "templates" / "chat.html").read_text(encoding="utf-8")
    assert ':focus-visible' in admin
    assert 'closeAccessibleModal' in admin
    assert 'for="chatInput"' in index
    assert 'event.key !== \'Escape\'' in index
    assert 'for="input"' in chat
    assert 'event.key === \'Escape\'' in chat


def test_operations_overview_exposes_unified_actionable_queue():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        'id="operationsOverview"',
        "function loadOperationsOverview()",
        "function loadOperationsTasks()",
        "/api/admin/governance/tasks",
        "运营待办",
        "/api/admin/operations/overview?tenant_id=",
        "待审批账号",
        "待处理监控预案",
        "待审 Skill/MCP",
        "const targets = {",
        "onclick=\"switchTab('${targets[key]}')\"",
        'id="globalAdminSearchInput"',
        "function runGlobalAdminSearch()",
        "/api/admin/search?q=",
        "function saveGlobalAdminSearch()",
        "function loadGlobalAdminSavedSearches()",
        "/api/admin/governance/saved-searches",
        "仅搜索管理元数据",
    ):
        assert fragment in html, fragment


def test_enterprise_integration_status_endpoint_is_exposed_in_admin_api():
    from auth import is_admin_route
    assert is_admin_route('/api/admin/integrations/status') is True


def test_workflow_editor_exposes_node_specific_controls_with_advanced_fallback():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        "function workflowNodeConfigForm(node, index)",
        'data-node-config-top-k',
        'min="1" max="50" step="1"',
        '检索结果数量必须是 1 到 50 的整数',
        '高级节点参数 JSON',
        '不能通过参数绕过人工审核',
            '首次运行会在人工审批节点暂停；批准后才执行指定的已审核、已授权 Skill/MCP',
    ):
        assert fragment in html, fragment


def test_workflow_editor_exposes_persisted_drag_canvas():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        'id="workflowEditorCanvas"',
        "function renderWorkflowCanvas(modal, nodes, edges)",
        "function startWorkflowCanvasDrag(event, modal, item)",
        "function drawWorkflowCanvasEdges(modal, edges)",
        "modal.dataset.workflowCanvasLayout",
        "node.layout && typeof node.layout === 'object'",
        "function resetWorkflowCanvas()",
    ):
        assert fragment in html, fragment


def test_knowledge_graph_admin_exposes_review_only_conflict_scan():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        'onclick="scanKnowledgeGraphConflicts()"',
        'id="graphConflictTable"',
        "/api/admin/knowledge-graph/conflicts",
        "结果仅供人工复核",
        "待人工复核",
    ):
        assert fragment in html, fragment


def test_knowledge_graph_admin_exposes_governed_semantic_candidate_action():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        'onclick="extractSemanticKnowledgeGraph()"',
        "/api/admin/knowledge-graph/semantic-extract",
        "待审核语义关系候选",
        "无证据或越界候选",
    ):
        assert fragment in html, fragment


def test_monitoring_admin_exposes_approved_change_handoff_action():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        "executeMonitoringChange('${item.id}')",
        "/execute",
        "当前默认适配器不会修改平台",
        "不会自动执行平台变更",
    ):
        assert fragment in html, fragment


def test_monitoring_admin_exposes_adapter_verify_and_explicit_rollback_actions():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        "runMonitoringAdapterAction('${item.id}','verify')",
        "runMonitoringAdapterAction('${item.id}','rollback')",
        "/adapter/${encodeURIComponent(action)}",
        "确认${labels[action] || action}",
    ):
        assert fragment in html, fragment


def test_graph_relation_prompt_is_operable_from_prompt_governance_page():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        "['reflection', 'graph_relation_extraction'].includes(item.slot)",
        "runPromptAssetGoldenTest('${escHtml(item.slot)}')",
        "slot !== 'reflection'",
        "该测试只校验输出契约",
    ):
        assert fragment in html, fragment


def test_admin_language_switch_translates_secondary_tabs_and_context():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    assert "const adminTabLabelsEn" in html
    assert "const adminTabContextEn" in html
    assert "let adminLanguage =" in html
    assert "adminLanguage = value" in html
    assert "if (activeTab) switchTab(activeTab)" in html


def test_admin_conversation_exposes_ai_revision_draft_review_flow():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        "AI 起草修订",
        "function draftAnswerRevision(convId, messageId)",
        "/revision-drafts",
        "decision:'approve'",
    ):
        assert fragment in html


def test_langfuse_configuration_page_is_reachable_from_configuration_group():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    page = (STATIC_DIR / "langfuse_config.html").read_text(encoding="utf-8")
    for fragment in (
        'data-tab="langfuseconfig"',
        "configuration: ['modelconfig', 'costrouting', 'reflection', 'prompt', 'langfuseconfig', 'externalretrieval', 'externalapi', 'webhooks', 'sso', 'emailnotifications']",
        'id="tabLangfuseconfig"',
        'src="/admin/langfuse-config"',
    ):
        assert fragment in html, fragment
    for fragment in (
        "Langfuse 观测配置",
        "/api/agent-eval/langfuse/config",
        "/api/agent-eval/langfuse/test",
        "/api/agent-eval/langfuse/dataset-sync",
        "/api/documents/profile-registry",
        'id=\"profile\"',
        'label="行业扩展"',
        "cyber-agent-eval-${String(profile",
        "允许导出脱敏正文",
    ):
        assert fragment in page, fragment


def test_email_notification_configuration_page_is_reachable_from_configuration_group():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    page = (STATIC_DIR / "email_notifications.html").read_text(encoding="utf-8")
    for fragment in (
        'data-tab="emailnotifications"',
        "emailnotifications",
        'id="tabEmailnotifications"',
        'src="/admin/email-notifications"',
        "/api/admin/email-notifications/config",
        "/api/admin/email-notifications/test",
        "/api/admin/email-notifications/deliveries",
        "/api/admin/governance/notification-policies",
        "function savePolicy()",
        "function previewPolicy()",
    ):
        assert fragment in html or fragment in page, fragment


def test_reflection_rules_are_configurable_and_observable_in_admin():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    for fragment in (
        'data-tab="reflection"',
        'id="tabReflection"',
        'id="reflectionRuleName"',
        'id="reflectionRunTable"',
        "function saveReflectionRule()",
        "function loadReflectionRules()",
        "/api/admin/reflection-rules",
        "/api/admin/reflection-summary",
    ):
        assert fragment in html, fragment
