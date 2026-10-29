"""JPS: Text Concatenate (text1..text4, delimiter 为分隔符名称)。"""
PROMPT_TYPES = ("Text Concatenate (JPS)",)

_DELIMITERS = {
    "comma": ", ",
    "space": " ",
    "newline": "\n",
    "semicolon": "; ",
}


def extract_prompt(inputs, side, ctx):
    parts = [ctx.prompt(inputs.get(f"text{index}"), side)
             for index in range(1, 5)]
    key = str(inputs.get("delimiter") or "").strip().lower()
    return ctx.join(parts, _DELIMITERS.get(key, ""))
