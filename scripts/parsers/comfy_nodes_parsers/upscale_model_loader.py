"""ComfyUI 放大模型节点: UpscaleModelLoader / 使用放大模型的节点。"""
UPSCALE_TYPES = ("UpscaleModelLoader", "ImageUpscaleWithModel",
                 "Upscale Image (using Model)")


def extract_upscale(inputs, ctx):
    name = inputs.get("model_name")
    if isinstance(name, str) and name.strip():
        return name
    return ctx.upscale(inputs.get("upscale_model"))
