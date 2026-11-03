"""清洗模型输出，便于结构化解析。"""


def strip_code_fence(raw: str) -> str:
    """剥掉模型输出里 ```json ... ``` 之类的外壳。

    模型经常无视"只输出 JSON 不要代码块"的要求，外面包一层代码块，
    直接 model_validate_json 会整个炸掉。解析前先过一遍这里。
    """

    text = (raw or "").strip()
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
