"""ComfyUI-Lora-Manager: Lora Loader / Lora Stacker。"""
MODEL_TYPES = ("Lora Loader (LoraManager)", "Lora Stacker (LoraManager)")

_STACKER = "Lora Stacker (LoraManager)"


def _entries(value):
    if isinstance(value, dict):
        value = value.get("__value__", value.get("value"))
    return value if isinstance(value, list) else []


def _parse_loras(value, ctx):
    result = []
    for entry in _entries(value):
        if isinstance(entry, str):
            result.append(ctx.lora(entry, 1.0))
            continue
        if not isinstance(entry, dict) or entry.get("active") is False:
            continue
        name = entry.get("name") or entry.get("lora_name") or entry.get("file")
        if isinstance(name, str) and name.strip():
            weight = entry.get("strength", entry.get("weight", 1.0))
            result.append(ctx.lora(name, weight))
    return result


def extract_model(inputs, ctx, class_type):
    loras = _parse_loras(inputs.get("loras"), ctx)
    if not loras:
        # loras 列表缺失时退回解析节点文本中的 <lora:...> 标签
        loras = ctx.inline_loras(inputs.get("text"))
    follow = None if class_type == _STACKER else inputs.get("model")
    return None, loras, follow
