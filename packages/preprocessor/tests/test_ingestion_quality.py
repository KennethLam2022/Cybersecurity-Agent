import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from llm_cleaner import validate_cleaned_text
from odl_parser import assess_text_quality


def test_empty_or_short_parse_result_is_rejected():
    report = assess_text_quality("", total_pages=2)
    assert report["valid"] is False
    assert "empty_text" in report["reasons"]


def test_parse_quality_accepts_structured_text():
    report = assess_text_quality("# 标题\n\n这是足够长的正文。" * 8, total_pages=1)
    assert report["valid"] is True
    assert report["headings"] >= 1


def test_cleaning_validation_detects_lost_standard_and_article():
    report = validate_cleaned_text(
        "# 文档\nGB/T 22239-2019 第8.1.4条要求。",
        "# 文档\n访问控制要求。",
    )
    assert report["valid"] is False
    assert report["missing_identifiers"]


def test_cleaning_validation_accepts_preserved_identifiers():
    report = validate_cleaned_text(
        "# 文档\nGB/T 22239-2019 第8.1.4条要求。",
        "# 文档\nGB/T 22239-2019 第8.1.4条要求。",
    )
    assert report["valid"] is True
