---
status: open
priority: high
created: 2026-05-27
found_by: 用户手动测试
---

# 2026-05-27 待修复清单

## Bug 1: Query 改写 `dict.strip()`  ✅ 已修复
- 文件: agent.py:722
- 日志: `Query 改写失败: 'dict' object has no attribute 'strip'`
- 原因: P4-2 将 chat() 返回值从 str→dict，_rewrite_query() 没同步改
- 修复: `rewritten.get("content", "") if isinstance(rewritten, dict) else rewritten`

## Bug 2: 流式属性名 `llm_provider` → `llm`  ✅ 已修复
- 文件: agent.py:1017
- 日志: `AttributeError: 'CyberAgent' object has no attribute 'llm_provider'`
- 原因: 属性名写错
- 提交: 55f0a82

## Bug 3: 浏览器 ERR_ABORTED on /api/chat/stream  ✅ 已修复
- 原因: Bug 2 的连锁反应，修复后流式正常 (38.2s, 1260 tokens)

## Bug 4: 切换提供商 API Key 被覆盖  ✅ 已修复
- 文件: main.py /api/llm/config
- 现象: 配置 DeepSeek → 切硅基 → 切回 DeepSeek 时 Key 丢失
- 原因: llm_config.json 只存单套配置，切换时覆盖
- 修复: 改为按 provider 名多套存储 {current, providers: {name: {base_url, api_key, model}}}
- 提交: (本次)
- 验证: DeepSeek + 硅基流动 同时保存 ✅ 切换测试连接均成功 ✅
