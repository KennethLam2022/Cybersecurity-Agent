import re
from pathlib import Path


SOURCE = Path(__file__).parents[1]


def test_chat_and_shared_css_custom_properties_are_defined():
    chat = (SOURCE / "templates" / "index.html").read_text(encoding="utf-8")
    shared = (SOURCE / "static" / "style.css").read_text(encoding="utf-8")
    definitions = set(re.findall(r"--([\w-]+)\s*:", shared))
    references = set(re.findall(r"var\(--([\w-]+)", chat + shared))

    assert references <= definitions
