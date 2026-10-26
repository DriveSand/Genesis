"""ComfyRoll: CR Upscale Image (upscale_model 为放大模型文件名)。"""
UPSCALE_TYPES = ("CR Upscale Image",)


def extract_upscale(inputs, ctx):
    return inputs.get("upscale_model")
