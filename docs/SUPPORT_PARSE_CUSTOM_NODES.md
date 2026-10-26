# ComfyUI 自定义节点解析支持

Genesis 的 ComfyUI 解析器通过 **节点 `class_type`** 识别工作流中的自定义节点。支持情况按“节点包 → 支持的节点”组织如下。

说明：

- 节点名必须与 API 工作流 `prompt` JSON 中的 `class_type` **完全一致**（区分大小写）。
- “提示词”指正向 / 反向提示词文本；“模型”指 Checkpoint / LoRA；“放大”指放大模型（写入解析结果的 `models.upscales`）。
- 未列出的自定义节点仍可能通过“通用兜底”规则提取部分字段，见文末。
- 图片同时带有 A1111 infotext 时，ComfyUI 解析结果缺失的字段会由 A1111 infotext 补齐。
- 包标识优先取自 ComfyUI 工作流节点的 `properties.cnr_id` / `properties.aux_id`；未在样本工作流中出现的包按节点类名或常见标识标注，无法确认归属的节点单独说明。

---

## comfyui-easy-use

**包标识**：`comfyui-easy-use`（ComfyUI-Easy-Use）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `easy positive` | 提示词 | 读取 `positive` 字段作为正向提示词 |
| `easy negative` | 提示词 | 读取 `negative` 字段作为反向提示词 |
| `easy promptConcat` | 提示词 | 拼接 `prompt1`、`prompt2`；分隔符取 `separator`，缺省为 `","` |
| `easy loraStack` | 模型 | 解析 LoRA 槽位列表（`lora_N_name` / `lora_N_strength` / `lora_N_model_strength`） |
| `easy loraStackApply` | 模型 | 继续回溯 `model` 链，并合并 `lora_stack` 中的 LoRA |

**easy loraStack 细节**：

- `toggle: false` 时整组 LoRA 忽略。
- `mode: "advanced"` 时权重取 `lora_N_model_strength`，否则取 `lora_N_strength`。
- 未提供 `num_loras` 时最多扫描 64 个槽位。
- 本节点只收集 LoRA，不继续回溯模型链；通常与 `easy loraStackApply` 配合使用。

> `easy cleanGpuUsed` 等其它 Easy-Use 节点不参与元数据解析。

---

## comfyui-lora-manager

**包标识**：`comfyui-lora-manager`（willmiao/ComfyUI-Lora-Manager）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `Lora Loader (LoraManager)` | 模型 | 解析 `loras` 列表中的 LoRA，并继续回溯 `model` 链 |
| `Lora Stacker (LoraManager)` | 模型 | 解析 `loras` 列表中的 LoRA；不再回溯模型链 |

**LoRA 列表解析规则**：

- 列表可能包裹在 `{"__value__": [...]}` 或 `{"value": [...]}` 中。
- `active: false` 的条目跳过。
- 名称依次取 `name`、`lora_name`、`file`；权重取 `strength`，缺省取 `weight`。
- `Lora Loader (LoraManager)` 的 `loras` 为空时，会退回解析 `text` 字段中的 `<lora:...>` / `<lyco:...>` 标签。

> `TriggerWord Toggle (LoraManager)`、`Save Image (LoraManager)` 等节点不参与元数据解析。

---

## efficiency-nodes-comfyui

**包标识**：`efficiency-nodes-comfyui`（Efficiency Nodes for ComfyUI）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `KSampler (Efficient)` | 采样器 | `seed` / `steps` / `cfg` / `sampler_name` / `scheduler`、正负提示词与模型链 |
| `Efficient Loader` | 提示词 + 模型 | Checkpoint、LoRA 堆、正负提示词 |

**细节**：

- `KSampler (Efficient)` 与核心 `KSampler` 一样参与采样器回溯；额外参数（`add_noise`、`start_at_step`、`end_at_step`、`return_with_left_over_noise`、非 1.0 的 `denoise`）写入 `note.comfyui`。
- `Efficient Loader` 的 `ckpt_name` 为 `"None"`/空时忽略；`lora_stack` 与 `lora_name`（同样忽略 `"None"`）会被收集。
- `Efficient Loader` 的 `positive` / `negative` 字段作为提示词来源。
- Baked VAE 不记录。

> `Control Net Stacker` 等其它 Efficiency 节点不参与元数据解析。

---

## ComfyUI_ADV_CLIP_emb

**包标识**：`ComfyUI_ADV_CLIP_emb`（BNK / Advanced CLIP Text Encode）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `BNK_CLIPTextEncodeAdvanced` | 提示词 | 读取 `text`（字符串或上游文本节点链接） |

> `token_normalization`、`weight_interpretation` 等编码参数不写入元数据。

---

## ComfyUI_Comfyroll_CustomNodes

**包标识**：`ComfyUI_Comfyroll_CustomNodes`（ComfyRoll Custom Nodes）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `CR Combine Prompt` | 提示词 | 按 `separator` 拼接 `part1` ~ `part4`；`separator` 非字符串时按空串处理 |
| `CR Upscale Image` | 放大 | 读取 `upscale_model` 放大模型文件名 |

> `CR Text` 没有独立解析模块，但其 `text` 输入符合通用兜底规则，通常仍可被正常提取。

---

## ComfyUI-Crystools

**包标识**：`crystian/comfyui-crystools`（ComfyUI-Crystools）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `Switch conditioning [Crystools]` | 提示词 | 根据 `boolean` 标量选择 `on_true` 或 `on_false` 分支的提示词 |

> `Switch any [Crystools]` 等其它 Crystools 节点不参与提示词 / 模型解析。

---

## ComfyUI_JPS-Nodes

**包标识**：`ComfyUI_JPS-Nodes`（JPS Nodes）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `Text Concatenate (JPS)` | 提示词 | 拼接 `text1` ~ `text4`，分隔符由 `delimiter` 决定 |

**`delimiter` 支持值**：

| 值 | 实际分隔符 |
|---|---|
| `comma` | `", "` |
| `space` | `" "` |
| `newline` | `"\n"` |
| `semicolon` | `"; "` |
| 其它 / 缺省 | 空串（直接拼接） |

> `SDXL Resolutions (JPS)` 等尺寸节点不参与元数据解析（分辨率始终以图片实际尺寸为准）。

---

## reservedvram

**包标识**：`reservedvram` / `Windecay/ComfyUI-ReservedVRAM`

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `ReservedVRAMSetter` | 提示词 | 透传 `anything` 输入，用于保持提示词链完整 |

---

## weilin-comfyui-tools

**包标识**：`weilin-comfyui-tools`（WeiLin Prompt UI）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `WeiLinPromptUIWithoutLora` | 提示词 | 读取 `positive` 字段；该字段固定为 `positive`，正向与反向提示词节点都使用它 |
| `WeiLinPromptUIOnlyLoraStack` | 模型 | 解析 `lora_str`（JSON 列表）中的 LoRA，并继续回溯 `model` 链 |

**`WeiLinPromptUIOnlyLoraStack` 细节**：

- `lora_str` 必须是 JSON 数组；解析失败时视为空。
- `hidden: true` 的条目跳过。
- 名称取 `display_name` / `name` / `lora`，并去掉末尾的 `-数字` 自动后缀。
- 名称中包含 `" - "` 时按最后一个 `" - "` 拆分 `name` 与 `version`；否则使用通用的 `name_version` 拆分规则。
- 权重取 `weight`，缺省取 `lora_model_strength`，再缺省为 `1.0`。

---

## was-ns

**包标识**：`was-ns`（WAS Node Suite）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `Image Save` | 输出节点 | 作为回溯起点，不直接提取字段 |
| `Checkpoint Loader (Simple)` | 模型 | 读取 `ckpt_name` 作为 Checkpoint |

> 注意区分：核心 ComfyUI 的 `CheckpointLoaderSimple` 与 WAS 的 `Checkpoint Loader (Simple)` 是不同节点，二者都受支持。
> WAS Node Suite 的其它节点（如 `Checkpoint Loader` 高级版）不在解析范围内。

---

## receyuki/comfyui-prompt-reader-node

**包标识**：`receyuki/comfyui-prompt-reader-node`（ComfyUI Prompt Reader Node）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `SDPromptSaver` | 输出节点 + 元数据来源 | 正负提示词、`seed`、`steps`、`cfg`、`sampler_name`、`scheduler`、`model_name`、`vae_name` |

**细节**：

- `SDPromptSaver` 同时是输出回溯起点；工作流中缺失的字段优先由该节点补齐。
- 即使工作流被移除，只要 A1111 infotext 中存在 `Version: ComfyUI`，图片仍会被识别为 ComfyUI 并尝试解析。
- 该节点的 `model_name` / `vae_name` 会按模型文件名规则拆分 `name` 与 `version`。

---

## sdxl_prompt_styler

**包标识**：`sdxl_prompt_styler`（twri/sdxl_prompt_styler）

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `SDXLPromptStyler` | 提示词 | 正向取 `text_positive`，反向取 `text_negative` |

> `SDXLPromptStylerAdvanced` 尚未注册；若其输入结构不符合通用兜底规则，相关提示词可能无法提取。

---

## Prompt Browser（节点包未标注）

**包标识**：样本工作流中该节点未记录 `cnr_id` / `aux_id`，此处按 `class_type` 归档。

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `PromptBrowserNode` | 提示词 | 读取 `prompt_text` 字段 |

---

## ComfyUI 核心（非自定义包）

以下节点属于 ComfyUI 核心（`comfy-core`），是自定义节点解析的基础；列在此处便于对照。

### 支持的节点

| 节点（class_type） | 解析类型 | 提取内容 |
|---|---|---|
| `SaveImage` | 输出节点 | 作为回溯起点 |
| `KSampler` | 采样器 | `seed`、`steps`、`cfg`、`sampler_name`、`scheduler`、正负提示词、模型链 |
| `KSamplerAdvanced` | 采样器 | 同上；种子读取 `noise_seed`；额外采样参数写入 `note.comfyui` |
| `CheckpointLoader` | 模型 | `ckpt_name` / `checkpoint` |
| `CheckpointLoaderSimple` | 模型 | `ckpt_name` |
| `unCLIPCheckpointLoader` | 模型 | `ckpt_name` |
| `LoraLoader` | 模型 | `lora_name`、`strength_model` / `strength`，并继续回溯 `model` |
| `LoraLoaderModelOnly` | 模型 | 同上 |
| `CLIPTextEncode` | 提示词 | `text` |
| `CLIPTextEncodeSDXL` | 提示词 | `text_g` 与 `text_l`；相同则取一份，否则按 `",\n"` 合并 |
| `CLIPTextEncodeSDXLRefiner` | 提示词 | `text` |
| `ConditioningCombine` | 提示词 | 合并 `conditioning_1` 与 `conditioning_2`（`", "`） |
| `ControlNetApplyAdvanced` | 提示词 | 透传正 / 负分支的条件 |
| `ControlNetApplySD3` | 提示词 | 透传正 / 负分支的条件 |
| `ControlNetApply` | 提示词 | 透传 `conditioning` |
| `ConditioningSetArea` | 提示词 | 透传 `conditioning` |
| `VAEDecode` → `VAELoader` | VAE | 输出路径上遇到 `VAEDecode` 时，读取其连接 `VAELoader` 的 `vae_name` |
| `FreeU` / `FreeU_V2` | 模型 | 透传 `model` 链 |
| `UpscaleModelLoader` | 放大 | 读取 `model_name` |
| `ImageUpscaleWithModel` | 放大 | 沿 `upscale_model` 找到放大模型名称 |
| `Upscale Image (using Model)` | 放大 | 同上（显示名/别名形式） |

---

## 通用兜底规则

未单独注册的自定义节点不会直接导致解析失败，解析器会尝试以下启发式规则：

1. **提示词桥接节点**
   - 依次尝试输入键：当前侧（`positive` / `negative`）、`conditioning`、`text`、`positive`、`negative`；
   - 若输入中存在字符串 `value`，直接作为提示词。
2. **模型桥接节点**
   - 依次尝试输入键：`model`、`base_model`、`optional_model`、`MODEL`，继续沿模型链回溯。
3. **标量节点**
   - 链接节点若带有 `value`、`string`，或仅有两个输入且含 `seed`，则将其解析为对应标量。
4. **输出回溯**
   - 默认从 `SaveImage` / `Image Save` / `SDPromptSaver` 等输出节点开始，查找距离输出最近的采样器；
   - 如果工作流没有已知输出节点，则从全部节点开始尝试。

兜底规则只能提取结构可识别的字段；遇到未知或结构复杂的节点时，图片仍会正常入库，但可能缺少提示词、模型或部分采样参数。

---

## 扩展新的自定义节点

新增解析器时，在 `scripts/parsers/comfy_nodes_parsers/` 下添加模块：

```python
# 提示词节点
PROMPT_TYPES = ("MyPromptNode",)

def extract_prompt(inputs, side, ctx):
    return ctx.prompt(inputs.get("text"), side)


# 模型节点
MODEL_TYPES = ("MyModelNode",)

def extract_model(inputs, ctx, class_type):
    checkpoint = inputs.get("ckpt_name")
    loras = ctx.loras(inputs.get("lora_stack"))
    return checkpoint, loras, inputs.get("model")   # 第三个返回值用于继续回溯


# 放大节点
UPSCALE_TYPES = ("MyUpscaleNode",)

def extract_upscale(inputs, ctx):
    return inputs.get("model_name")
```

然后在 `comfy_nodes_parsers/__init__.py` 中导入该模块并加入 `_MODULES`。注册表会按 `PROMPT_TYPES` / `MODEL_TYPES` / `UPSCALE_TYPES` 自动建立 `class_type` 索引。

`ctx` 提供的递归能力：

| 方法 | 说明 |
|---|---|
| `ctx.prompt(value, side)` | 递归解析提示词（自动处理条件链） |
| `ctx.scalar(value)` | 解析字面值或上游标量节点 |
| `ctx.join(parts, separator)` | 拼接非空提示词片段 |
| `ctx.stem(name)` | 去掉目录与模型扩展名 |
| `ctx.lora(name, weight)` | 生成标准 LoRA 条目（按最后一个下划线拆分 name/version） |
| `ctx.lora_entry(name, weight, version)` | 生成显式指定版本的 LoRA 条目 |
| `ctx.inline_loras(text)` | 解析 `<lora:...>` / `<lyco:...>` 标签 |
| `ctx.model(value)` | 递归解析模型链，返回 `(checkpoint, loras)` |
| `ctx.loras(value)` | 只取模型链中的 LoRA 列表 |
| `ctx.upscale(value)` | 沿放大模型链接解析文件名 |
