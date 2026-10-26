"""WeiLin Prompt UI: 仅 LoRA 堆 (lora_str 为 JSON 列表)。"""
import json
import re

MODEL_TYPES = ("WeiLinPromptUIOnlyLoraStack",)

_AUTO_SUFFIX = re.compile(r"-\d+$")


def _entries(inputs):
    raw = inputs.get("lora_str")
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def extract_model(inputs, ctx, _class_type):
    loras = []
    for entry in _entries(inputs):
        if not isinstance(entry, dict) or entry.get("hidden"):
            continue
        display = (entry.get("display_name") or entry.get("name")
                   or entry.get("lora") or "")
        if not isinstance(display, str) or not display.strip():
            continue
        display = _AUTO_SUFFIX.sub("", ctx.stem(display))
        weight = entry.get("weight", entry.get("lora_model_strength", 1.0))
        if " - " in display:
            name, version = display.rsplit(" - ", 1)
            loras.append(ctx.lora_entry(name.strip(), weight, version.strip()))
        else:
            loras.append(ctx.lora(display, weight))
    checkpoint, parents = ctx.model(inputs.get("model"))
    return checkpoint, loras + parents, None
