#!/usr/bin/env python
"""初始化 active_prompt.txt"""
from pathlib import Path
import re

src_path = Path(__file__).parent.parent / "packages" / "agent" / "src" / "agent.py"
txt = src_path.read_text(encoding="utf-8")
m = re.search(r'SYSTEM_PROMPT_SOURCE\s*=\s*"""(.+?)"""', txt, re.DOTALL)
if m:
    content = m.group(1).strip()
    dest = Path(__file__).parent.parent / "packages" / "agent" / "agent_data" / "active_prompt.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    print(f"✅ active_prompt.txt created ({len(content)} chars)")
else:
    print("❌ SYSTEM_PROMPT_SOURCE not found in agent.py")
