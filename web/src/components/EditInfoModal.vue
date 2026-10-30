<script setup lang="ts">
import Icon from './Icon.vue'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import {
  afterMetadataSaved,
  askConfirm,
  closeEditor,
  editTarget,
  imagePathOf,
  toast,
  toastError,
} from '../store'
import {
  RATING_NAMES,
  SAMPLERS,
  SCHEDULERS,
  TOOLS,
  type ImageMetadata,
  type ModelSimple,
} from '../types'

interface EditModel {
  kind: string
  name: string
  version: string
  url: string
  weight: number | null
}

const MODEL_KINDS = ['Checkpoint', 'LoRA', 'LyCORIS', 'Embedding', 'VAE', 'Upscale'] as const

const loading = ref(true)
const saving = ref(false)
const original = ref<ImageMetadata | null>(null)
const models = ref<EditModel[]>([])

const form = reactive({
  rating: 0,
  steps: 0,
  cfg: 0,
  seed: 0,
  note: '',
  positive_prompt: '',
  negative_prompt: '',
})

const toolChoice = ref('')
const toolOther = ref('')
const samplerChoice = ref('')
const samplerOther = ref('')
const schedulerChoice = ref('')
const schedulerOther = ref('')

const fileName = computed(() => editTarget.value?.name ?? '')

function initChoice(value: string, options: string[], choice: { value: string }, other: { value: string }): void {
  if (!value) {
    choice.value = ''
  } else if (options.includes(value)) {
    choice.value = value
  } else {
    choice.value = 'other'
    other.value = value
  }
}

function resolveChoice(choice: string, other: string): string {
  return choice === 'other' ? other.trim() : choice
}

function toEditModels(
  kind: string,
  rows: ModelSimple[],
  weight: number | null = null,
): EditModel[] {
  return rows.map((m) => ({
    kind,
    name: m.model_name,
    version: m.version ?? '',
    url: m.model_url ?? '',
    weight,
  }))
}

onMounted(async () => {
  const file = editTarget.value
  if (!file) return
  try {
    const path = imagePathOf(file)
    const [meta, grouped] = await Promise.all([api.metadata(path), api.models(path)])
    original.value = meta
    Object.assign(form, {
      rating: meta.rating,
      steps: meta.steps,
      cfg: meta.cfg,
      seed: meta.seed,
      note: meta.note,
      positive_prompt: meta.positive_prompt,
      negative_prompt: meta.negative_prompt,
    })
    initChoice(meta.tool, TOOLS, toolChoice, toolOther)
    initChoice(meta.sampler, SAMPLERS, samplerChoice, samplerOther)
    initChoice(meta.scheduler, SCHEDULERS, schedulerChoice, schedulerOther)
    models.value = [
      ...toEditModels('Checkpoint', grouped.checkpoint),
      ...grouped.lora.map((m) => ({
        kind: m.type === 1 ? 'LyCORIS' : 'LoRA',
        name: m.model_name,
        version: m.version ?? '',
        url: m.model_url ?? '',
        weight: m.weight,
      })),
      ...toEditModels('Embedding', grouped.embedding),
      ...toEditModels('VAE', grouped.vae),
      ...toEditModels('Upscale', grouped.upscale),
    ]
    origModels.value = serialiseModels(models.value)
  } catch (err) {
    toastError(err, '读取信息失败')
  } finally {
    loading.value = false
  }
})

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') closeEditor()
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

function addModel(): void {
  models.value.push({ kind: 'Checkpoint', name: '', version: '', url: '', weight: null })
}

async function removeModel(index: number): Promise<void> {
  const model = models.value[index]
  if (!model) return
  const label = model.name.trim() ? `“${model.name.trim()}”` : '该模型'
  const ok = await askConfirm(
    '移除模型',
    `确定要移除${label}吗？保存修改后才会生效。`,
    { danger: true },
  )
  if (!ok) return
  models.value.splice(index, 1)
}

function onKindChange(model: EditModel): void {
  if (model.kind === 'LoRA' || model.kind === 'LyCORIS') {
    if (model.weight === null) model.weight = 1.0
  } else {
    model.weight = null
  }
}

function serialiseModels(rows: EditModel[]): Record<string, unknown[]> {
  const checkpoints = rows.filter((m) => m.kind === 'Checkpoint')
    .map((m) => ({ model_name: m.name, version: m.version, model_url: m.url || null }))
  const loras = rows.filter((m) => m.kind === 'LoRA' || m.kind === 'LyCORIS')
    .map((m) => ({
      model_name: m.name,
      version: m.version,
      model_url: m.url || null,
      weight: m.weight ?? 1.0,
      type: m.kind === 'LyCORIS' ? 1 : 0,
    }))
  const embeddings = rows.filter((m) => m.kind === 'Embedding')
    .map((m) => ({ model_name: m.name, version: m.version, model_url: m.url || null }))
  const vaes = rows.filter((m) => m.kind === 'VAE')
    .map((m) => ({ model_name: m.name, version: m.version, model_url: m.url || null }))
  const upscales = rows.filter((m) => m.kind === 'Upscale')
    .map((m) => ({ model_name: m.name, version: m.version, model_url: m.url || null }))
  return {
    checkpoint: checkpoints, lora: loras, embedding: embeddings, vae: vaes,
    upscale: upscales,
  }
}

function buildChange(): Record<string, unknown> | null {
  const orig = original.value
  if (!orig) return null
  if (models.value.some((m) => !m.name.trim())) {
    toast('模型名称不能为空', 'error')
    return null
  }
  const change: Record<string, unknown> = {}
  const textFields: Array<[string, string, string]> = [
    ['positive_prompt', form.positive_prompt, orig.positive_prompt],
    ['negative_prompt', form.negative_prompt, orig.negative_prompt],
    ['note', form.note, orig.note],
  ]
  for (const [key, value, before] of textFields) {
    if (value !== before) change[key] = value
  }
  if (Number(form.rating) !== orig.rating) change.rating = Number(form.rating)
  if (Number(form.steps) !== orig.steps) change.steps = Number(form.steps)
  if (Number(form.cfg) !== orig.cfg) change.cfg = Number(form.cfg)
  if (Number(form.seed) !== orig.seed) change.seed = Number(form.seed)
  const tool = resolveChoice(toolChoice.value, toolOther.value)
  const sampler = resolveChoice(samplerChoice.value, samplerOther.value)
  const scheduler = resolveChoice(schedulerChoice.value, schedulerOther.value)
  if (tool !== orig.tool) change.tool = tool
  if (sampler !== orig.sampler) change.sampler = sampler
  if (scheduler !== orig.scheduler) change.scheduler = scheduler

  const current = serialiseModels(models.value)
  const beforeModels = {
    checkpoint: origModels.value.checkpoint,
    lora: origModels.value.lora,
    embedding: origModels.value.embedding,
    vae: origModels.value.vae,
    upscale: origModels.value.upscale,
  }
  for (const key of ['checkpoint', 'lora', 'embedding', 'vae', 'upscale'] as const) {
    if (JSON.stringify(current[key]) !== JSON.stringify(beforeModels[key])) {
      change[key] = current[key]
    }
  }
  return change
}

const origModels = ref<Record<string, unknown[]>>({
  checkpoint: [], lora: [], embedding: [], vae: [], upscale: [],
})

async function save(): Promise<void> {
  const file = editTarget.value
  if (!file || !original.value || saving.value) return
  const change = buildChange()
  if (change === null) return
  if (!Object.keys(change).length) {
    toast('没有需要保存的修改')
    return
  }
  saving.value = true
  try {
    const target = { name: file.name, path: file.path }
    await api.updateMetadata(target, change)
    toast('修改已保存', 'success')
    closeEditor()
    await afterMetadataSaved(target, change)
  } catch (err) {
    toastError(err, '保存失败')
  } finally {
    saving.value = false
  }
}

function emitClose(): void {
  closeEditor()
}
</script>

<template>
  <div class="overlay" @mousedown.self="emitClose">
    <div class="modal panel">
      <div class="panel-title">
        <span>编辑信息 — {{ fileName }}</span>
        <button class="icon-btn" @click="emitClose">
          <Icon name="close" label="close" />
        </button>
      </div>

      <div v-if="loading" class="empty-tip">正在读取…</div>

      <div v-else class="body">
        <section class="top">
          <div class="row">
            <div>
              <span class="form-label">文件名</span>
              <input class="field" :value="fileName" disabled />
            </div>
            <div>
              <span class="form-label">分级</span>
              <select v-model.number="form.rating" class="field">
                <option v-for="(name, level) in RATING_NAMES" :key="name" :value="level">{{ name }}</option>
              </select>
            </div>
            <div>
              <span class="form-label">工具</span>
              <div class="row">
                <select v-model="toolChoice" class="field">
                  <option value="">未指定</option>
                  <option v-for="tool in TOOLS" :key="tool" :value="tool">{{ tool }}</option>
                  <option value="other">Other</option>
                </select>
                <input v-if="toolChoice === 'other'" v-model="toolOther" class="field"
                       placeholder="输入工具名" />
              </div>
            </div>
          </div>

          <div class="params-grid">
            <div>
              <span class="form-label">步数</span>
              <input v-model.number="form.steps" type="number" class="field" />
            </div>
            <div>
              <span class="form-label">CFG</span>
              <input v-model.number="form.cfg" type="number" step="0.1" class="field" />
            </div>
            <div>
              <span class="form-label">采样器</span>
              <select v-model="samplerChoice" class="field">
                <option value="">未指定</option>
                <option v-for="sampler in SAMPLERS" :key="sampler" :value="sampler">{{ sampler }}</option>
                <option value="other">Other</option>
              </select>
              <input v-if="samplerChoice === 'other'" v-model="samplerOther" class="field mt"
                     placeholder="输入采样器名" />
            </div>
            <div>
              <span class="form-label">调度器</span>
              <select v-model="schedulerChoice" class="field">
                <option value="">未指定</option>
                <option v-for="scheduler in SCHEDULERS" :key="scheduler" :value="scheduler">{{ scheduler }}</option>
                <option value="other">Other</option>
              </select>
              <input v-if="schedulerChoice === 'other'" v-model="schedulerOther" class="field mt"
                     placeholder="输入调度器名" />
            </div>
            <div>
              <span class="form-label">随机种</span>
              <input v-model.number="form.seed" type="number" class="field" />
            </div>
          </div>
        </section>

        <div class="columns">
          <section class="col models">
            <div class="section-title">
              <span>模型</span>
              <button class="btn ghost small" @click="addModel">＋ 添加模型</button>
            </div>
            <div v-if="!models.length" class="muted empty-models">尚未记录模型</div>
            <div v-for="(model, index) in models" :key="index" class="model-card">
              <div class="model-head">
                <select v-model="model.kind" class="field kind" @change="onKindChange(model)">
                  <option v-for="kind in MODEL_KINDS" :key="kind" :value="kind">{{ kind }}</option>
                </select>
                <button class="icon-btn small" title="移除" @click="removeModel(index)">
                  <Icon name="delete" label="remove" />
                </button>
              </div>
              <div class="form-grid">
                <div>
                  <span class="form-label">名称</span>
                  <input v-model="model.name" class="field" placeholder="模型名称" />
                </div>
                <div>
                  <span class="form-label">版本</span>
                  <input v-model="model.version" class="field" placeholder="版本 (可空)" />
                </div>
                <div>
                  <span class="form-label">URL</span>
                  <input v-model="model.url" class="field" placeholder="https://…" />
                </div>
                <div v-if="model.kind === 'LoRA' || model.kind === 'LyCORIS'">
                  <span class="form-label">权重</span>
                  <input v-model.number="model.weight" type="number" step="0.05" class="field" />
                </div>
              </div>
            </div>
          </section>

          <section class="col texts">
            <div>
              <span class="form-label">正向提示</span>
              <textarea v-model="form.positive_prompt" class="field prompt-area"
                        placeholder="正向提示词"></textarea>
            </div>
            <div>
              <span class="form-label">反向提示</span>
              <textarea v-model="form.negative_prompt" class="field prompt-area"
                        placeholder="反向提示词"></textarea>
            </div>
            <div>
              <span class="form-label">备注</span>
              <textarea v-model="form.note" class="field note-area" placeholder="备注"></textarea>
            </div>
          </section>
        </div>
      </div>

      <div class="footer">
        <button class="btn" @click="emitClose">取消</button>
        <button class="btn accent" :disabled="saving || loading" @click="save">
          {{ saving ? '保存中…' : '保存修改' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  z-index: 120;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
}

.modal {
  width: min(1040px, calc(100vw - 60px));
  max-height: calc(100vh - 60px);
  display: flex;
  flex-direction: column;
  background: #1b1b1b;
}

.body {
  overflow-y: auto;
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.top {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.params-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
}

.columns {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
}

.col {
  min-width: 0;
}

.models {
  max-height: 46vh;
  overflow-y: auto;
}

.model-card {
  padding: 8px;
  margin-bottom: 8px;
  border: 1px solid var(--border);
  background: var(--control);
}

.model-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  margin-bottom: 8px;
}

.kind {
  width: 140px;
}

.texts {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* 正向 / 反向提示输入框保持完全相同的宽高 */
.prompt-area {
  height: 150px;
  min-height: 150px;
}

.note-area {
  height: 90px;
  min-height: 90px;
}

.mt {
  margin-top: 6px;
}

.empty-models {
  padding: 16px 0;
}

.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 10px 18px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}
</style>
