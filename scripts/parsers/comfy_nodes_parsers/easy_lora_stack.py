"""easy-use: easy loraStack (LoRA 槽位列表, simple/advanced 权重)。"""
MODEL_TYPES = ("easy loraStack",)


def parse_stack(inputs, ctx):
    """把 easy loraStack 的 lora_N_* 字段解析为 LoRA 列表。"""
    if inputs.get("toggle") is False:
        return []
    mode = str(inputs.get("mode") or "").strip().lower()
    advanced = mode == "advanced"
    try:
        count = int(inputs.get("num_loras") or 0)
    except (TypeError, ValueError):
        count = 0
    result = []
    limit = count if count > 0 else 64
    for index in range(1, limit + 1):
        key = f"lora_{index}_name"
        if key not in inputs and count <= 0:
            break
        name = inputs.get(key)
        if not isinstance(name, str) or not name.strip() \
                or name.strip().lower() == "none":
            continue
        if advanced:
            weight = inputs.get(f"lora_{index}_model_strength",
                                inputs.get(f"lora_{index}_strength", 1.0))
        else:
            weight = inputs.get(f"lora_{index}_strength", 1.0)
        result.append(ctx.lora(name, weight))
    return result


def extract_model(inputs, ctx, _class_type):
    return None, parse_stack(inputs, ctx), None
