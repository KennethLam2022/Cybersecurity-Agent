(function () {
  const dictionaries = {
    'en-US': {
      '邮件通知': 'Email Notifications', '默认关闭。仅向管理员配置的收件人发送指定系统事件；SMTP 密码加密保存，页面不会回显原始密钥。': 'Disabled by default. Send selected system events only to administrator-configured recipients. SMTP passwords are encrypted and never revealed.',
      'SMTP 主机': 'SMTP Host', '端口': 'Port', '加密方式': 'Security', '发件人': 'Sender', '用户名': 'Username', '密码': 'Password', '收件人（逗号分隔）': 'Recipients (comma-separated)', '启用邮件通知': 'Enable email notifications', '订阅事件': 'Subscribed events', '保存配置': 'Save Configuration', '发送测试邮件': 'Send Test Email', '刷新投递记录': 'Refresh Deliveries', '最近投递': 'Recent Deliveries',
      'SSO / 企业身份集成': 'SSO / Enterprise Identity', '配置 LDAP/AD 绑定登录或 OIDC 授权码登录；首次登录按企业策略自动开通或进入管理员审批。': 'Configure LDAP/AD bind login or OIDC authorization-code login. First-time access is provisioned or sent for approval according to enterprise policy.', '用途与边界': 'Purpose and boundaries', '已配置登录方式': 'Configured Login Methods', '新增登录方式': 'Add Login Method', '首次登录审批': 'First-login approvals',
      'Langfuse 观测配置': 'Langfuse Observability', '配置外部观测服务、数据传输范围与 Agent Eval Dataset 同步。': 'Configure external observability, data transfer scope, and Agent Eval Dataset synchronization.', '连接配置': 'Connection Configuration', '启用 Langfuse': 'Enable Langfuse', '允许导出脱敏正文': 'Allow redacted content export', '保存配置': 'Save Configuration', '测试配置': 'Test Configuration', 'Agent Eval Dataset 同步': 'Agent Eval Dataset Sync', '同步测试集': 'Sync Test Set'
      , '后端模型配置': 'Backend Model Configuration', '隔离配置各后端模块使用的 LLM 模型，每个模型独立选择平台和 API Key': 'Configure LLMs for each backend module independently, including provider and API key.', '文档入库管理': 'Document Ingestion', '拖拽上传文档 → 扫描校验 → 确认范围 → 自动清洗切片 → 入库知识库': 'Upload documents → scan and validate → confirm scope → clean and chunk → index into the knowledge base', '知识库治理': 'Knowledge Base Governance', '管理知识库归属、文档生命周期、版本和入库结果。权限与审核状态独立于向量索引。': 'Manage knowledge-base ownership, document lifecycle, versions, and ingestion results. Permissions and review status are independent from the vector index.', '知识库': 'Knowledge Bases', '数据源登记': 'Data Source Registry',
      '刷新': 'Refresh', '保存': 'Save', '编辑': 'Edit', '删除': 'Delete', '启用': 'Enable', '停用': 'Disable', '测试': 'Test', '取消': 'Cancel', '创建知识库': 'Create knowledge base', '登记数据源': 'Register data source', '暂无知识库': 'No knowledge bases', '暂无数据源': 'No data sources', '加载中...': 'Loading...', '加载失败': 'Load failed', '暂无记录': 'No records', '请求失败': 'Request failed', '配置已加载': 'Configuration loaded', '配置已保存': 'Configuration saved', '测试中...': 'Testing...', '同步中...': 'Synchronizing...', '暂无配置 SSO 登录方式。': 'No SSO providers configured.', '账号审批': 'Account approval', '行业扩展': 'Industry extensions', '公共': 'Public', '私有': 'Private', '工作区共享': 'Workspace shared', '手动同步': 'Manual sync', '定时同步': 'Scheduled sync'
    }
  };
  function dictionary(language) { return dictionaries[language] || {}; }
  function translateTextNodes(root, language) {
    const dict = dictionary(language);
    const reverse = language === 'zh-CN'
      ? Object.fromEntries(Object.entries(dictionary('en-US')).map(([key, value]) => [value, key]))
      : {};
    const walker = document.createTreeWalker(root || document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let node;
    while ((node = walker.nextNode())) {
      const parent = node.parentElement;
      if (!parent || ['SCRIPT', 'STYLE', 'TEXTAREA'].includes(parent.tagName)) continue;
      const raw = node.nodeValue || '';
      const key = raw.trim();
      const translated = dict[key] || reverse[key];
      if (!key || !translated) continue;
      node.nodeValue = raw.replace(key, translated);
    }
  }
  function apply(language) {
    const dict = dictionary(language);
    document.documentElement.lang = language;
    document.querySelectorAll('[data-i18n]').forEach((el) => {
      const value = dict[el.dataset.i18n] || el.dataset.i18n;
      if (el.dataset.i18nAttr) el.setAttribute(el.dataset.i18nAttr, value);
      else el.textContent = value;
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
      el.placeholder = dict[el.dataset.i18nPlaceholder] || el.dataset.i18nPlaceholder;
    });
    translateTextNodes(document.body, language);
  }
  function init() {
    const current = localStorage.getItem('securenexus_language') === 'en-US' ? 'en-US' : 'zh-CN';
    const select = document.querySelector('[data-page-language]');
    if (select) {
      select.value = current;
      select.addEventListener('change', () => {
        const value = select.value === 'en-US' ? 'en-US' : 'zh-CN';
        localStorage.setItem('securenexus_language', value);
        apply(value);
      });
    }
    apply(current);
    if (!window.__secureNexusPageI18nObserver) {
      window.__secureNexusPageI18nObserver = new MutationObserver((records) => {
        const dict = dictionary(localStorage.getItem('securenexus_language') === 'en-US' ? 'en-US' : 'zh-CN');
        records.forEach((record) => record.addedNodes.forEach((node) => {
          if (node.nodeType === Node.ELEMENT_NODE) translateTextNodes(node, localStorage.getItem('securenexus_language') === 'en-US' ? 'en-US' : 'zh-CN');
        }));
      });
      window.__secureNexusPageI18nObserver.observe(document.body, {childList: true, subtree: true});
    }
  }
  window.SecureNexusPageI18n = { init, apply };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
