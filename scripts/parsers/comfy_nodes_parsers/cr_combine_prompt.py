"""ComfyRoll: CR Combine Prompt (part1..part4 按 separator 拼接)。"""
PROMPT_TYPES = ("CR Combine Prompt",)


def extract_prompt(inputs, side, ctx):
    parts = [ctx.prompt(inputs.get(f"part{index}"), side)
             for index in range(1, 5)]
    separator = inputs.get("separator")
    if not isinstance(separator, str):
        separator = ""
    return ctx.join(parts, separator)
