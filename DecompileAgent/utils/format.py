import re

def extract_json_content(text: str) -> str:
    """
    Extract the first JSON object from LLM output.
    Supports fenced ```json ... ``` and raw {...}.
    Returns empty string if no JSON object is found.
    """
    if not text:
        return ""

    # 优先匹配 ```json ... ``` 或 ``` ... ```
    fence_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        text,
        re.DOTALL | re.IGNORECASE
    )
    if fence_match:
        return fence_match.group(1).strip()

    # 兜底：匹配第一个 {...}
    brace_match = re.search(
        r"(\{.*\})",
        text,
        re.DOTALL
    )
    if brace_match:
        return brace_match.group(1).strip()

    return ""
