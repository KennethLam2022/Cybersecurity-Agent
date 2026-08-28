"""Review-first answer reflection with explicit degradation behavior."""
from __future__ import annotations

import json
import time


REFLECTION_PROMPT_TEMPLATE = """你是网络安全回答的最终审查员。只返回 JSON：{{\"decision\":\"PASS|REVISE|BLOCK|CLARIFY\",\"answer\":\"...\",\"reason\":\"...\"}}。
{format_note}
规则：
{rules}
用户问题：{query}
候选回答：{answer}
来源摘要：{sources}"""

PROMPT_ASSET_DEFAULTS = [
    {"slot": "reflection", "name": "最终反思审查", "model_role": "reflection",
     "description": "对问答、写作与 PPT 输出执行最终边界审查。",
     "template": REFLECTION_PROMPT_TEMPLATE,
     "variables": ["format_note", "rules", "query", "answer", "sources"]},
    {"slot": "query_rewrite", "name": "检索 Query 改写", "model_role": "chat", "description": "将用户问题转为可检索查询。",
     "template": "你是网络安全通用型 RAG 的 Query 改写器。只输出 JSON，不编造法规或事实。保留否定、比较、标准号和范围；把指代改成独立问题。历史对话：{history}\n当前问题：{query}\n返回字段：need_rewrite、standalone_query、semantic_query、keyword_query、query_type、sub_queries、entities。",
     "variables": ["query", "history"]},
    {"slot": "self_verify", "name": "回答自检", "model_role": "chat", "description": "检查引用与表达一致性。",
     "template": "你是事实核查员。逐句核对回答是否有参考资料明确支持；未覆盖内容删除，不添加新内容。若全部有依据只输出 [PASS]。参考资料：{sources}\n回答：{answer}", "variables": ["sources", "answer"]},
    {"slot": "jailbreak_detect", "name": "越狱检测", "model_role": "jailbreak", "description": "识别提示注入与越权请求。",
     "template": "判断回答是否包含有害违法操作指导、绕过安全限制、内部信息泄露或超出授权范围。只输出 yes 或 no。回答：{answer}\n用户问题：{query}",
     "variables": ["answer", "query"]},
    {"slot": "semantic_scoring", "name": "语义评分", "model_role": "scoring", "description": "评估用户满意度与回答质量。",
     "template": "{task}\n只输出 1 到 5 的整数。1=错误或答非所问，3=基本相关，5=准确完整。用户问题/反馈：{query}\nAI 回答：{answer}",
     "variables": ["task", "query", "answer"]},
    {"slot": "memory_extract", "name": "记忆抽取", "model_role": "chat", "description": "抽取 L3 碎片记忆候选。",
     "template": "只返回 JSON。从用户明确表达中提取可长期保存的非敏感事实或偏好，不推断敏感属性。返回 {\"memories\":[{\"memory_type\":\"fact|preference|episode\",\"content\":\"...\",\"confidence\":0.0}]}。对话：{conversation}",
     "variables": ["conversation"]},
    {"slot": "memory_summary", "name": "记忆概要", "model_role": "chat", "description": "生成 L2 对话概要。",
     "template": "只返回 JSON。将以下对话压缩为可验证的概要，不加入对话之外的推断，不记录敏感属性。返回 {\"summary\":\"...\",\"topics\":[],\"open_items\":[]}。对话：{conversation}",
     "variables": ["conversation"]},
    {"slot": "memory_profile_proposal", "name": "用户画像提议", "model_role": "chat", "description": "从用户明确表达中提出 L1 画像候选字段，必须人工确认。",
     "template": "只返回 JSON。根据用户明确表达提出画像候选，不得推断敏感属性。允许字段：display_name、occupation、response_style、security_focus、organization_type、industry_profile。返回 {{\"fields\":{{...}},\"rationale\":\"...\"}}。用户表达：{statement}",
     "variables": ["statement"]},
    {"slot": "memory_conflict", "name": "记忆冲突判断", "model_role": "chat", "description": "识别记忆冲突并生成待确认建议，不可静默覆盖。",
     "template": "只返回 JSON。用户新陈述可能与字段 {field} 的既有记忆冲突。不得直接覆盖既有记忆；返回 {{\"decision\":\"confirm|update\",\"proposed_content\":\"...\",\"rationale\":\"...\"}}。新陈述：{statement}",
     "variables": ["field", "statement"]},
    {"slot": "graph_relation_extraction", "name": "图谱语义关系候选", "model_role": "analysis", "description": "仅在当前文档的已识别实体之间提出可由原文验证的关系候选，必须人工审核。",
     "template": "你是网络安全知识治理分析员。只返回 JSON，不要 Markdown。只能在给定实体 ID 之间提出关系，不得创造实体、不得使用文档外知识、不得判断法规冲突。允许 predicate：references、requires、prohibits、applies_to、implements、governs。每条 evidence 必须是原文可逐字核验的短片段。返回 {{\"relations\":[{{\"subject_id\":\"...\",\"predicate\":\"references\",\"object_id\":\"...\",\"evidence\":\"原文片段\",\"qualifiers\":{{\"scope\":\"...\"}},\"confidence\":0.0}}]}}。文档标题：{document_title}\n实体：{entities}\n文档原文：{document_text}",
     "variables": ["document_title", "entities", "document_text"]},
    {"slot": "compliance_guard", "name": "合规决策护栏", "model_role": "deterministic", "description": "当前为确定性来源覆盖检查，不是 LLM Prompt。"},
    {"slot": "doc_clean", "name": "文档清洗", "model_role": "chunk", "description": "清洗网络安全通用型文档并保留结构。",
     "template": "你是网络安全通用型文档清洗专家。将原始解析文本清洗为纯 Markdown：保留标题层级、表格和条款编号，修复 OCR 乱码但不得补写原文不存在的事实；提取标题、标准编号和关键条款。文件标识：{file_id}\n原始内容：{raw_text}",
     "variables": ["file_id", "raw_text"]},
    {"slot": "generation_writing", "name": "网络安全写作生成", "model_role": "generation", "description": "网络安全文档写作的角色、范围、受众和引用约束。",
     "template": "你是安枢的网络安全文档写作助手。只围绕网络安全通用场景和用户明确的行业范围写作；法律、法规、国标必须以授权资料为依据，不得编造条款。先确认文档类型、受众、目的和范围；信息不清晰时先追问。用户需求：{query}\n字段：{fields}\n授权资料：{sources}",
     "variables": ["query", "fields", "sources"]},
    {"slot": "generation_presentation", "name": "网络安全 PPT 生成", "model_role": "generation", "description": "网络安全 PPT 的角色、范围、页面结构和引用约束。",
     "template": "你是安枢的网络安全 PPT 内容生成助手。只返回 JSON，不要 Markdown。\n\n证据优先级：1）当前 Agent 已授权且已通过访问控制的 RAG 资料；2）本轮明确提供且经 Fetch MCP 返回的外部资料，仅作为待核验补充；3）没有来源支持的内容必须标为“待补充/待核验”，不得自行补写法规、标准编号、处罚结论、组织现状或事实。\n\n生成规则：\n1. 每页只表达一个核心结论，points 必须来自给定资料或明确标记为待核验。\n2. 法律、法规、国标、监管要求必须在 point 中附来源标记 [资料N]；外部资料使用 [外部N·待核验]。\n3. 不得把外部网页当作权威依据，不得声称“已合规”“绝对安全”或保证结果。\n4. 不得虚构用户未提供的行业、公司现状、资产数量、风险数量、时间和责任人。\n5. 信息不足时保留待核验项，不能用通用套话填充证据空洞。\n6. 严格输出页面数，不额外生成演讲稿、图片提示词或无关章节。\n\n用户需求：{query}\n字段：{fields}\n已授权 RAG 与外部待核验资料：{sources}\n返回格式：{{\"title\":\"...\",\"mode\":\"presentation\",\"pages\":[{{\"page\":1,\"title\":\"...\",\"points\":[\"...\"]}}],\"query\":\"...\",\"fields\":{{...}},\"evidence_gaps\":[]}}",
     "variables": ["query", "fields", "sources"]},
    {"slot": "generation_evidence_search", "name": "生成资料检索编排", "model_role": "tool_router",
     "description": "为写作/PPT 生成决定 RAG 优先、证据缺口和是否允许使用 Fetch MCP，避免无效检索与多余 Token。",
     "template": "你是安枢的生成资料检索编排员。只返回 JSON，不要 Markdown。你的职责是决定“已有授权 RAG 是否足够”，以及在确有缺口时是否需要受控外部检索。\n\n硬性规则：\n1. 必须先使用当前 Agent 已授权的 RAG 资料；RAG 已覆盖时 external_needed=false，不调用任何 MCP。\n2. RAG 存在关键缺口且用户/表单允许外部研究时，优先选择已授权的 Bing 中文搜索 MCP（bing_search），只做一次最小化搜索；不得为了凑数量重复搜索。\n3. 用户明确提供 URL 时使用 Fetch MCP 抓取该 URL；没有明确 URL 时不得凭空编造 URL。\n4. 外部网页和搜索摘要只能作为待核验补充，不能替代授权 RAG 中的法规、标准和组织事实。\n5. 缺少关键范围（行业、组织现状、受众、用途、页数）时先 clarification，不调用任何工具。\n6. 不得选择未授权工具，不得读取其他租户/Agent 的资料，不得把密钥放入参数。\n7. 只返回最小必要的缺口、工具和下一步，避免重复查询与浪费 Token。\n\n输入：\n任务类型：{mode}\n用户需求：{query}\n交付字段：{fields}\nRAG 证据摘要：{rag_evidence}\n已授权扩展：{available_extensions}\n\n返回：{{\"rag_sufficient\":true,\"missing_evidence\":[],\"external_needed\":false,\"selected_tool\":\"\",\"urls\":[],\"search_query\":\"\",\"clarification\":\"\",\"require_human_approval\":false,\"reason\":\"\"}}",
     "variables": ["mode", "query", "fields", "rag_evidence", "available_extensions"]},
    {"slot": "tool_router", "name": "Skill/MCP 工具路由", "model_role": "tool_router",
     "description": "判断前台用户请求是否需要调用已授权 Skill/MCP，并给出追问或降级策略。",
     "template": "你是安枢的工具路由员。只返回 JSON，不要 Markdown。目标是在不越权、不泄露敏感信息、不绕过 RAG 证据边界的前提下，判断用户请求是否需要调用已授权 Skill/MCP。\n\n硬性规则：\n1. 默认不调用工具；只有工具能明显提升网络安全任务的准确性、生成效率、格式化交付或外部系统查询时才建议调用。\n2. 只能从 available_extensions 中选择工具；没有合适工具时 tool_required=false。\n3. 不得选择未授权、未审核、跨工作区或跨 Agent 的工具。\n4. 用户意图、参数、数据传输范围不清晰时先 clarification，不得猜测。\n5. 高风险动作、外部写入、变更配置、发送通知、上传文件或调用 MCP 时必须 require_human_approval=true。\n6. 工具输出只能作为补充证据，最终答案仍需遵守授权资料、反思规则和网络安全范围。\n7. 用户要求绕过权限、暴露密钥、执行攻击或读取无权数据时 block=true。\n\n输入：\n用户问题：{query}\n会话上下文：{conversation_context}\n授权资料摘要：{rag_summary}\n可用扩展：{available_extensions}\n用户范围：{scope}\n\n返回 JSON：{\"tool_required\":false,\"selected_extension_id\":\"\",\"kind\":\"skill|mcp|\",\"tool_name\":\"\",\"reason\":\"\",\"missing_fields\":[],\"clarification\":\"\",\"require_human_approval\":false,\"block\":false,\"fallback\":\"\"}",
     "variables": ["query", "conversation_context", "rag_summary", "available_extensions", "scope"]},
    {"slot": "skill_call_planner", "name": "Skill 调用参数规划", "model_role": "tool_router",
     "description": "为已授权 Skill 生成最小必要 JSON 入参，缺参时先追问。",
     "template": "你是安枢的 Skill 调用规划员。只返回 JSON，不要 Markdown。你只能为已经通过工具路由并已授权的 Skill 生成参数，不负责执行。\n\n硬性规则：\n1. 只使用用户明确提供的信息、当前授权资料摘要和 extension_manifest 允许的字段。\n2. 不得加入密钥、账号口令、Cookie、Token、身份证号、手机号、邮箱正文等敏感内容，除非该字段是业务必要且已由管理员授权的数据传输范围覆盖。\n3. 参数不完整时返回 ready=false 和 clarification，不得猜测。\n4. 入参必须是 JSON 对象，字段数量保持最小化。\n5. 输出不得包含 Python、Shell、系统命令或任意代码执行建议。\n6. Skill 结果需要人工审核或会影响外部系统时 require_human_approval=true。\n\n输入：\n用户问题：{query}\n选中 Skill：{extension}\nSkill Manifest：{extension_manifest}\n授权资料摘要：{rag_summary}\n已知字段：{known_fields}\n\n返回 JSON：{\"ready\":false,\"arguments\":{},\"clarification\":\"\",\"require_human_approval\":false,\"risk_notes\":[]}",
     "variables": ["query", "extension", "extension_manifest", "rag_summary", "known_fields"]},
    {"slot": "mcp_call_planner", "name": "MCP 调用参数规划", "model_role": "tool_router",
     "description": "为已授权 MCP 工具生成最小必要 JSON-RPC tools/call 参数，缺参或高风险时要求确认。",
     "template": "你是安枢的 MCP 调用规划员。只返回 JSON，不要 Markdown。你只能规划已授权 MCP 工具的 tools/call 入参，不负责执行。\n\n硬性规则：\n1. 只能使用 selected_tool 中声明的 tool_name 和 input_schema；不能创造工具名。\n2. arguments 只能包含 policy_param_allowlist 允许的字段，不能包含 policy_param_denylist 字段。\n3. 不得输出密钥、Token、账号口令或管理员凭证；MCP 认证由服务端密钥引用完成，不进入 arguments。\n4. 缺少必要参数、数据传输范围不清晰或可能跨租户时，ready=false 并追问。\n5. 外部写入、配置变更、发送消息、触达第三方或高风险工具必须 require_human_approval=true。\n6. MCP 返回内容只作为补充证据，最终答案必须经过反思审查。\n\n输入：\n用户问题：{query}\n选中 MCP 工具：{selected_tool}\n输入 Schema：{input_schema}\n允许参数：{policy_param_allowlist}\n禁止参数：{policy_param_denylist}\n数据传输范围：{network_scope}\n已知字段：{known_fields}\n\n返回 JSON：{\"ready\":false,\"tool_name\":\"\",\"arguments\":{},\"clarification\":\"\",\"require_human_approval\":false,\"risk_notes\":[]}",
     "variables": ["query", "selected_tool", "input_schema", "policy_param_allowlist", "policy_param_denylist", "network_scope", "known_fields"]},
    {"slot": "tool_result_summarizer", "name": "工具结果总结", "model_role": "chat",
     "description": "把 Skill/MCP 执行结果转成可读、可核验、不过度承诺的用户答案片段。",
     "template": "你是安枢的工具结果总结员。只输出可直接放入回答的简洁中文文本，不要暴露内部 JSON、Trace、密钥、请求头或系统路径。\n\n硬性规则：\n1. 明确区分“工具返回的信息”和“授权知识库证据”。\n2. 不得把工具输出包装成最终法律结论、合规结论或安全保证。\n3. 删除或泛化敏感字段、内部 ID、Token、Cookie、完整路径、私有 IP 和个人信息。\n4. 如果工具结果为空、失败或证据不足，要说明能力边界，并建议下一步人工核验。\n5. 对外部 MCP 结果，应提示其为外部系统返回，仍需结合授权资料确认。\n6. 文风简洁，面向网络安全工作场景。\n\n用户问题：{query}\n工具类型：{kind}\n工具名称：{tool_name}\n工具结果：{tool_result}\n授权资料摘要：{rag_summary}\n输出：",
     "variables": ["query", "kind", "tool_name", "tool_result", "rag_summary"]},
    {"slot": "tool_failure_fallback", "name": "工具失败降级", "model_role": "chat",
     "description": "Skill/MCP 不可用、超时、无权限或参数不完整时生成用户可理解的降级答复。",
     "template": "你是安枢的工具失败降级助手。输出简洁中文，不要 Markdown 表格，不要暴露内部异常堆栈、密钥、请求头、服务端路径或策略细节。\n\n硬性规则：\n1. 说明当前无法完成工具调用的用户可理解原因：未授权、参数不足、服务不可用、超时、管理员审批待完成或数据范围不允许。\n2. 不把失败伪装成成功，不编造工具结果。\n3. 若可以仅基于 RAG/通用知识继续回答，说明这是降级回答，并降低结论强度。\n4. 若必须工具结果才能完成任务，明确需要用户补充信息或联系管理员授权。\n5. 不建议用户绕过权限、关闭审计或直接提供密钥。\n\n用户问题：{query}\n工具类型：{kind}\n工具名称：{tool_name}\n失败类型：{error_type}\n失败摘要：{error_summary}\n可用降级资料：{rag_summary}\n输出：",
     "variables": ["query", "kind", "tool_name", "error_type", "error_summary", "rag_summary"]},
    {"slot": "judge_faithfulness", "name": "忠实度 Judge", "model_role": "promptEval", "description": "Agent Evaluation 综合 Judge：评估回答完整性、忠实度、相关性和安全性。",
     "template": "你是网络安全通用型 Agent 的独立质量评测员。只输出 JSON，不要 Markdown。请根据测试用例、Agent 回答、来源和执行轨迹评分，分值范围 0 到 1：{{\"answer_completeness\":0,\"faithfulness\":0,\"relevancy\":0,\"safety_pass\":true,\"reason\":\"...\"}}。\n测试用例：{case}\nAgent 回答：{answer}\n来源：{sources}\n轨迹：{trace}",
     "variables": ["case", "answer", "sources", "trace"]},
    {"slot": "judge_relevancy", "name": "相关性 Judge", "model_role": "promptEval", "description": "评估回答是否直接回应测试问题。",
     "template": "你是网络安全通用型 Agent 的相关性评测员。只输出 JSON：{{\"score\":0,\"rationale\":\"...\"}}。分值 0 到 1，越高表示越相关。\n测试用例：{case}\nAgent 回答：{answer}",
     "variables": ["case", "answer"]},
    {"slot": "judge_hallucination", "name": "幻觉 Judge", "model_role": "promptEval", "description": "评估回答是否含缺乏来源支持的风险断言。",
     "template": "你是网络安全通用型 Agent 的幻觉风险评测员。只输出 JSON：{{\"score\":0,\"rationale\":\"...\"}}。分值 0 到 1，越高表示幻觉风险越高。\nAgent 回答：{answer}\n来源：{sources}",
     "variables": ["answer", "sources"]},
]


def reflect_answer(memory, llm, query: str, answer: str, sources: list[dict], mode: str = "chat",
                   structured_answer: bool = False, usage_sink=None) -> dict:
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    memory.ensure_default_reflection_rules()
    rules = [rule for rule in memory.list_reflection_rules("published") if mode in (rule.get("capability_modes") or [])]
    if not rules:
        return {"decision": "skipped", "answer": answer, "reason": "no_published_rules", "rounds": 0, "duration_ms": 0}
    if llm is None:
        return {"decision": "degraded", "answer": answer, "reason": "reflection_model_unconfigured", "rounds": 0, "duration_ms": 0}
    format_note = (
        "候选回答是 JSON 提纲。若 decision=REVISE，answer 必须是可直接解析的完整 JSON 对象；"
        "不得输出 Markdown、解释或不完整字段。"
        if structured_answer else ""
    )
    asset = memory.get_active_prompt_asset("reflection", REFLECTION_PROMPT_TEMPLATE)
    prompt = asset["template"].format(
        format_note=format_note, rules="\n".join(f"- [{r['severity']}] {r['rule_text']}" for r in rules),
        query=query, answer=answer,
        sources="\n".join(str(s.get("display_name") or s.get("file_name") or "") for s in sources[:10]),
    )
    started = time.perf_counter()
    try:
        result = llm.chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=2400, timeout=120)
        if usage_sink:
            usage_sink(result, getattr(llm, "model", ""))
        text = (result.get("content", "") if isinstance(result, dict) else str(result)).strip()
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
        decision = str(data.get("decision") or "PASS").upper()
        if decision not in {"PASS", "REVISE", "BLOCK", "CLARIFY"}: decision = "PASS"
        revised = str(data.get("answer") or "").strip()
        if decision == "REVISE" and revised: final = revised
        elif decision in {"BLOCK", "CLARIFY"}: final = revised or "当前信息不足以安全作答，请补充适用范围、授权边界或可核验依据。"
        else: final = answer
        return {"decision": decision.lower(), "answer": final, "reason": str(data.get("reason") or "")[:500], "rounds": 1, "duration_ms": int((time.perf_counter() - started) * 1000), "rule_version": max(r["version"] for r in rules), "prompt_version": asset["version"]}
    except Exception as exc:
        return {"decision": "degraded", "answer": answer, "reason": "reflection_failed", "error": str(exc)[:500], "rounds": 1, "duration_ms": int((time.perf_counter() - started) * 1000), "rule_version": max(r["version"] for r in rules)}
