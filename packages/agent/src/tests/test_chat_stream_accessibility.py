from pathlib import Path


def test_chat_log_uses_non_live_streaming_semantics():
    html = (Path(__file__).parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="chatMessages" role="log" aria-label="对话消息" aria-live="off"' in html
    assert 'id="chatResponseStatus" class="sr-only" role="status" aria-live="polite" aria-atomic="true"' in html


def test_chat_stream_announces_start_and_terminal_state_once():
    html = (Path(__file__).parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
    send_message = html[html.index("async function sendMessage()") : html.index("function generationFieldLabel")]

    assert send_message.count("chatResponseStatus').textContent") == 2
    assert "助手正在生成回答" in send_message
    assert "answerCompleted ? '助手回答完成' : '助手回答未能完成'" in send_message
