import subprocess
import sys
import os
from pathlib import Path

from jieba_compat import load_jieba


def test_jieba_compat_keeps_tokenization_available():
    jieba = load_jieba()

    assert "网络安全" in jieba.lcut("网络安全风险评估")


def test_jieba_compat_import_has_no_pkg_resources_warning():
    code = "from jieba_compat import load_jieba; load_jieba().lcut('网络安全')"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([
                str(Path(__file__).resolve().parents[1]),
                str(Path(__file__).resolve().parents[3] / "preprocessor" / "src"),
            ]),
        },
        check=True,
    )

    assert "pkg_resources is deprecated" not in result.stderr
