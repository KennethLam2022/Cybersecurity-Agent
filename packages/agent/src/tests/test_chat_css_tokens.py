import re
from pathlib import Path


SOURCE = Path(__file__).parents[1]


def test_chat_and_shared_css_custom_properties_are_defined():
    chat = (SOURCE / "templates" / "index.html").read_text(encoding="utf-8")
    shared = (SOURCE / "static" / "style.css").read_text(encoding="utf-8")
    definitions = set(re.findall(r"--([\w-]+)\s*:", shared))
    references = set(re.findall(r"var\(--([\w-]+)", chat + shared))

    assert references <= definitions


def test_mobile_chat_input_keeps_wrapped_placeholder_and_send_button_visible():
    shared = (SOURCE / "static" / "style.css").read_text(encoding="utf-8")
    mobile = re.search(
        r"@media\s*\(max-width:\s*480px\)\s*"
        r"\{((?:[^{}]|\{[^{}]*\})*)\}",
        shared,
    )
    assert mobile is not None
    assert mobile.start() > shared.index(".chat-input {")
    assert mobile.start() > shared.index(".send-btn {")

    rules = mobile.group(1)
    input_area = re.search(r"\.chat-input-area\s*\{([^}]*)\}", rules)
    input_row = re.search(r"\.chat-input-row\s*\{([^}]*)\}", rules)
    chat_input = re.search(r"\.chat-input\s*\{([^}]*)\}", rules)
    send_button = re.search(r"\.send-btn\s*\{([^}]*)\}", rules)
    assert all((input_area, input_row, chat_input, send_button))

    assert re.search(r"padding:\s*12px 16px", input_area.group(1))
    assert re.search(r"gap:\s*8px", input_row.group(1))
    assert re.search(r"min-width:\s*0", chat_input.group(1))
    assert re.search(r"min-height:\s*(6[0-9]|[7-9]\d)px", chat_input.group(1))
    assert re.search(r"padding:\s*10px 16px", send_button.group(1))
    assert re.search(r"white-space:\s*nowrap", shared[shared.index(".send-btn {"):mobile.start()])
