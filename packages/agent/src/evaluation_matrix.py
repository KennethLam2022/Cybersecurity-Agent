"""
加权评估矩阵 — 8 维度 + 权重配置
=================================
参考：掘金《Prompt多版本测试指南》第 7 节 "构建评估矩阵"

每个维度有权重，加权得分 = Σ(维度得分 × 权重)
"""

import json
from typing import Optional

# 默认评估矩阵（可配置）
DEFAULT_WEIGHTS = {
    "来源标注": 0.20,       # 每条知识是否标注来源
    "品牌禁止": 0.15,       # 是否包含品牌名
    "越狱拦截": 0.15,       # 是否拦截非安全话题
    "偏题检测": 0.10,       # 是否识别无关话题
    "首答完整": 0.15,       # 是否一次给出完整回答
    "知识准确": 0.15,       # 关键知识点是否正确
    "输出格式": 0.05,       # 是否按预期格式输出
    "响应效率": 0.05,       # 响应时间是否合理
}

# 弹性测试配置
ELASTIC_TESTS = {
    "input_variation": {
        "description": "输入变化测试 — 用不同表达方式的相似问题",
        "variations": [
            "标准表达",
            "非正式表达",
            "复杂询问",
            "带拼写错误",
            "中英混合",
        ],
    },
    "edge_cases": {
        "description": "边缘情况测试 — 极端或异常输入",
        "cases": [
            "空输入",
            "超长输入 (>1000 字)",
            "乱码输入",
            "重复输入",
            "特殊字符输入",
        ],
    },
    "noise_injection": {
        "description": "噪声注入测试 — 在输入中添加干扰信息",
        "noise_types": [
            "拼写错误",
            "语法错误",
            "冗余信息",
            "无关内容",
            "诱导性语言",
        ],
    },
}


def evaluate_with_weights(scores: dict, weights: Optional[dict] = None) -> float:
    """加权计算总分"""
    w = weights or DEFAULT_WEIGHTS
    total = 0.0
    weight_sum = 0.0
    for dim, score in scores.items():
        weight = w.get(dim, 0.05)  # 默认 5%
        total += score * weight
        weight_sum += weight
    if weight_sum > 0:
        return round(total / weight_sum * 100, 1)
    return 0.0


def get_dimension_breakdown(scores: dict, weights: Optional[dict] = None) -> dict:
    """获取各维度贡献度分析"""
    w = weights or DEFAULT_WEIGHTS
    breakdown = {}
    total_weighted = 0.0
    for dim, score in scores.items():
        weight = w.get(dim, 0.05)
        contribution = score * weight
        total_weighted += contribution
        breakdown[dim] = {
            "score": score,
            "weight": weight,
            "contribution": round(contribution, 4),
            "contribution_pct": round(contribution / total_weighted * 100, 1) if total_weighted > 0 else 0,
        }
    return breakdown
