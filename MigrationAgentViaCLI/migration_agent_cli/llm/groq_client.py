from __future__ import annotations

import json
import os
from typing import Any


def summarize_with_groq(agent_title: str, output: dict[str, Any]) -> str | None:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You summarize .NET migration agent outputs in concise engineering language.",
                },
                {
                    "role": "user",
                    "content": f"Agent: {agent_title}\nOutput JSON:\n{json.dumps(output, default=str)[:12000]}",
                },
            ],
            temperature=0.1,
        )
        return response.choices[0].message.content
    except Exception as exc:
        return f"Groq summary unavailable: {exc}"

