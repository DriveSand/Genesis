<script setup lang="ts">
import Icon from './Icon.vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { runAdvancedSearch, store, toast } from '../store'
import {
  FILE_TYPES,
  RATING_NAMES,
  SAMPLERS,
  SCHEDULERS,
  TOOLS,
  type AdvancedQuery,
} from '../types'

type RuleKind =
  | 'prompt' | 'neg_prompt' | 'file_name' | 'path'
  | 'ckpt' | 'lora' | 'embed'
  | 'size' | 'width' | 'height' | 'seed'
  | 'add_time'
  | 'tool' | 'sampler' | 'scheduler' | 'rating' | 'file_type'

interface Rule {
  uid: number
  kind: RuleKind
  mode?: 'include' | 'exclusion'
  value?: string | number | null
  other?: string
  name?: string
  ver?: string
  min?: number | null
  max?: number | null
  unit?: string
  from?: string
  to?: string
}

const RULE_DEFS: Array<{ kind: RuleKind; label: string; group: string }> = [
  { kind: 'prompt', label: '提示词', group: '文本' },
  { kind: 'neg_prompt', label: '反向提示词', group: '文本' },
  { kind: 'file_name', label: '文件名', group: '文本' },
  { kind: 'path', label: '路径', group: '文本' },
  { kind: 'ckpt', label: 'Checkpoint', group: '模型' },
  { kind: 'lora', label: 'LoRA / LyCORIS', group: '模型' },
  { kind: 'embed', label: 'Embedding', group: '模型' },
  { kind: 'size', label: '文件大小', group: '数值' },
  { kind: 'width', label: '图像宽度', group: '数值' },
  { kind: 'height', label: '图像高度', group: '数值' },
  { kind: 'seed', label: '随机种', group: '数值' },
  { kind: 'add_time', label: '添加时间', group: '时间' },
  { kind: 'tool', label: '工具', group: '生成参数' },
  { kind: 'sampler', label: '采样器', group: '生成参数' },
  { kind: 'scheduler', label: '调度器', group: '生成参数' },
  { kind: 'rating', label: '分级', group: '生成参数' },
  { kind: 'file_type', label: '文件类型', group: '生成参数' },
]

const UNIT_SCALE: Record<string, number> = { B: 1, KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3 }
const RANGE_KEY: Record<'size' | 'width' | 'height', 'file_size' | 'width' | 'height'> = {
  size: 'file_size',
  width: 'width',
  height: 'height',
}
const KEYWORD_KINDS: RuleKind[] = ['prompt', 'neg_prompt', 'file_name', 'path']
const MODEL_KINDS: RuleKind[] = ['ckpt', 'lora', 'embed']
const RANGE_KINDS: RuleKind[] = ['size', 'width', 'height']
const CHOICE_OPTIONS: Partial<Record<RuleKind, string[]>> = {
  tool: TOOLS,
  sampler: SAMPLERS,
  scheduler: SCHEDULERS,
}

const rules = ref<Rule[]>([])
const ruleMenuOpen = ref(false)
const panelEl = ref<HTMLElement | null>(null)

// ---------------------------------------------------------------------- #
// 弹窗定位: 右边缘对齐高级搜索按钮, 顶边紧贴按钮下方
// ---------------------------------------------------------------------- #

function reposition(): void {
  const panel = panelEl.value
  if (!panel) return
  // 与搜索框水平中心对齐; 搜索框消失时退回高级搜索按钮
  const anchor = (document.querySelector('.search-wrap')
    ?? document.querySelector('[data-panel="search"]')) as HTMLElement | null
  if (!anchor) return
  const rect = anchor.getBoundingClientRect()
  const width = panel.offsetWidth || Math.min(760, window.innerWidth - 60)
  const left = Math.max(10,
    Math.min(rect.left + rect.width / 2 - width / 2,
             window.innerWidth - width - 10))
  panel.style.left = `${left}px`
  panel.style.right = 'auto'
  panel.style.top = `${rect.bottom + 4}px`
}

watch(
  () => store.searchPanelOpen,
  async (open) => {
    if (!open) return
    await nextTick()
    reposition()
  },
)

onMounted(() => window.addEventListener('resize', reposition))
onBeforeUnmount(() => window.removeEventListener('resize', reposition))
let seq = 0

// 面板通过 v-show 隐藏时保留已添加的规则, 仅关闭规则菜单
watch(
  () => store.searchPanelOpen,
  (open) => {
    if (!open) ruleMenuOpen.value = false
  },
)

const groupedDefs = computed(() => {
  const groups: Array<{ name: string; items: typeof RULE_DEFS }> = []
  for (const def of RULE_DEFS) {
    let group = groups.find((g) => g.name === def.group)
    if (!group) {
      group = { name: def.group, items: [] }
      groups.push(group)
    }
    group.items.push(def)
  }
  return groups
})

function labelOf(kind: RuleKind): string {
  return RULE_DEFS.find((d) => d.kind === kind)?.label ?? kind
}

function isKeyword(kind: RuleKind): boolean {
  return KEYWORD_KINDS.includes(kind)
}

function isModel(kind: RuleKind): boolean {
  return MODEL_KINDS.includes(kind)
}

function isRange(kind: RuleKind): boolean {
  return RANGE_KINDS.includes(kind)
}

function isChoice(kind: RuleKind): boolean {
  return kind in CHOICE_OPTIONS
}

function addRule(kind: RuleKind): void {
  const rule: Rule = { uid: ++seq, kind }
  if (isKeyword(kind)) {
    rule.mode = 'include'
    rule.value = ''
  } else if (isModel(kind)) {
    rule.name = ''
    rule.ver = ''
  } else if (isRange(kind)) {
    rule.min = null
    rule.max = null
    if (kind === 'size') rule.unit = 'MB'
  } else if (kind === 'add_time') {
    rule.from = ''
    rule.to = ''
  } else {
    rule.value = ''
  }
  rules.value.push(rule)
  ruleMenuOpen.value = false
}

function removeRule(uid: number): void {
  rules.value = rules.value.filter((r) => r.uid !== uid)
}

function toggleMode(rule: Rule): void {
  rule.mode = rule.mode === 'include' ? 'exclusion' : 'include'
}

function resetRules(): void {
  rules.value = []
  ruleMenuOpen.value = false
}

function toEpoch(value: string | undefined): number {
  if (!value) return 0
  const time = new Date(value).getTime()
  return Number.isFinite(time) ? Math.floor(time / 1000) : 0
}

function choiceValue(rule: Rule): string {
  const raw = String(rule.value ?? '')
  return raw === 'other' ? String(rule.other ?? '').trim() : raw.trim()
}

function pushKeyword(target: { include: string[]; exclusion: string[] }, rule: Rule): void {
  const value = String(rule.value ?? '').trim()
  if (!value) return
  if (rule.mode === 'exclusion') target.exclusion.push(value)
  else target.include.push(value)
}

function buildQuery(): AdvancedQuery {
  const q: AdvancedQuery = {
    path: { include: [], exclusion: [] },
    file_name: { include: [], exclusion: [] },
    file_type: [],
    file_size: [],
    width: [],
    height: [],
    sha256: [],
    prompt: { include: [], exclusion: [] },
    neg_prompt: { include: [], exclusion: [] },
    ckpt: [],
    lora: [],
    embed: [],
    tool: [],
    seed: [],
    sampler: [],
    scheduler: [],
    add_time: [],
    rating: [],
    note: [],
  }
  for (const rule of rules.value) {
    switch (rule.kind) {
      case 'prompt':
        pushKeyword(q.prompt, rule)
        break
      case 'neg_prompt':
        pushKeyword(q.neg_prompt, rule)
        break
      case 'file_name':
        pushKeyword(q.file_name, rule)
        break
      case 'path':
        pushKeyword(q.path, rule)
        break
      case 'ckpt':
      case 'lora':
      case 'embed': {
        const name = String(rule.name ?? '').trim()
        if (name) q[rule.kind].push({ name, ver: String(rule.ver ?? '').trim() })
        break
      }
      case 'size':
      case 'width':
      case 'height': {
        const scale = rule.kind === 'size' ? (UNIT_SCALE[rule.unit ?? 'MB'] ?? 1) : 1
        const low = rule.min ? Math.round(Number(rule.min) * scale) : 0
        const high = rule.max ? Math.round(Number(rule.max) * scale) : 0
        if (low || high) {
          q[RANGE_KEY[rule.kind]].push({ bigger_than: low, smaller_than: high })
        }
        break
      }
      case 'seed': {
        if (rule.value !== '' && rule.value !== null && rule.value !== undefined) {
          q.seed.push(Number(rule.value))
        }
        break
      }
      case 'add_time': {
        const after = toEpoch(rule.from)
        const before = toEpoch(rule.to)
        if (after || before) q.add_time.push({ after, before })
        break
      }
      case 'tool':
      case 'sampler':
      case 'scheduler': {
        const value = choiceValue(rule)
        if (value) q[rule.kind].push(value)
        break
      }
      case 'rating': {
        if (rule.value !== '' && rule.value !== null) q.rating.push(Number(rule.value))
        break
      }
      case 'file_type': {
        if (rule.value) q.file_type.push(String(rule.value))
        break
      }
    }
  }
  return q
}

function search(): void {
  if (!rules.value.length) {
    toast('请先添加搜索规则', 'info')
    return
  }
  void runAdvancedSearch(buildQuery())
}
</script>

<template>
  <div ref="panelEl" class="search-panel panel">
    <div class="panel-title">
      <span>高级搜索</span>
      <div class="row">
        <div class="menu-wrap">
          <button class="btn small accent" @click.stop="ruleMenuOpen = !ruleMenuOpen">
            添加搜索规则
          </button>
          <div v-if="ruleMenuOpen" class="rule-menu panel" @click.stop>
            <div v-for="group in groupedDefs" :key="group.name" class="menu-group">
              <div class="menu-group-title">{{ group.name }}</div>
              <button v-for="def in group.items" :key="def.kind" class="rule-menu-item"
                      @click="addRule(def.kind)">{{ def.label }}</button>
            </div>
          </div>
        </div>
        <button class="btn small danger" :disabled="!rules.length" @click="resetRules">
          清空所有规则
        </button>
        <button class="btn small" @click="search">搜索</button>
      </div>
    </div>

    <div class="panel-body">
      <div v-if="!rules.length" class="empty-tip">
        尚未添加搜索规则，点击右上角「添加搜索规则」开始
      </div>

      <div v-for="rule in rules" :key="rule.uid" class="rule">
        <div class="rule-head">
          <span class="rule-label">{{ labelOf(rule.kind) }}</span>
          <button class="icon-btn small" title="移除规则" @click="removeRule(rule.uid)">
            <Icon name="close" label="remove" />
          </button>
        </div>

        <div v-if="isKeyword(rule.kind)" class="rule-row">
          <button class="mode-btn" :class="{ exclusion: rule.mode === 'exclusion' }"
                  @click="toggleMode(rule)">
            {{ rule.mode === 'include' ? '包含' : '不包含' }}
          </button>
          <input v-model="rule.value" class="field"
                 :placeholder="rule.mode === 'include' ? '必须含有…' : '不得含有…'" />
        </div>

        <div v-else-if="isModel(rule.kind)" class="rule-row">
          <input v-model="rule.name" class="field" placeholder="名称" />
          <input v-model="rule.ver" class="field" placeholder="版本 (留空匹配全部)" />
        </div>

        <div v-else-if="isRange(rule.kind)" class="rule-row">
          <input v-model.number="rule.min" type="number" min="0" class="field"
                 placeholder="最小" />
          <input v-model.number="rule.max" type="number" min="0" class="field"
                 placeholder="最大" />
          <select v-if="rule.kind === 'size'" v-model="rule.unit" class="field unit">
            <option value="B">B</option>
            <option value="KB">KiB</option>
            <option value="MB">MiB</option>
            <option value="GB">GiB</option>
          </select>
        </div>

        <div v-else-if="rule.kind === 'add_time'" class="rule-row">
          <span class="muted small">从</span>
          <input v-model="rule.from" type="datetime-local" class="field" />
          <span class="muted small">到</span>
          <input v-model="rule.to" type="datetime-local" class="field" />
        </div>

        <div v-else-if="isChoice(rule.kind)" class="rule-row">
          <select v-model="rule.value" class="field">
            <option value="">不限</option>
            <option v-for="option in CHOICE_OPTIONS[rule.kind]" :key="option" :value="option">
              {{ option }}
            </option>
            <option value="other">Other</option>
          </select>
          <input v-if="rule.value === 'other'" v-model="rule.other" class="field"
                 placeholder="输入自定义值" />
        </div>

        <div v-else-if="rule.kind === 'rating'" class="rule-row">
          <select v-model="rule.value" class="field">
            <option value="">不限</option>
            <option v-for="(name, level) in RATING_NAMES" :key="name" :value="level">
              {{ name }}
            </option>
          </select>
        </div>

        <div v-else-if="rule.kind === 'file_type'" class="rule-row">
          <select v-model="rule.value" class="field">
            <option value="">不限</option>
            <option v-for="type in FILE_TYPES" :key="type" :value="type">{{ type }}</option>
          </select>
        </div>

        <div v-else-if="rule.kind === 'seed'" class="rule-row">
          <input v-model.number="rule.value" type="number" class="field" placeholder="随机种" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.search-panel {
  position: fixed;
  /* left/top 由 reposition() 按触发按钮的包围盒计算 */
  top: 50px;
  left: 10px;
  width: min(760px, calc(100vw - 60px));
  max-height: calc(100vh - 60px);
  display: flex;
  flex-direction: column;
  z-index: 45;
}

.panel-body {
  overflow-y: auto;
  padding: 10px 16px 16px;
}

.rule {
  margin-bottom: 10px;
  border: 1px solid var(--border);
  background: var(--bg-elevated);
}

.rule-head {
  display: flex;
  align-items: center;
  height: 30px;
  padding: 0 8px 0 12px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}

.rule-label {
  flex: 1;
  font-size: 12.5px;
  color: var(--text-dim);
  letter-spacing: 0.02em;
}

.rule-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
}

.rule-row .field {
  flex: 1;
}

.unit {
  flex: none;
  width: 76px;
}

.mode-btn {
  flex: none;
  width: 64px;
  height: 28px;
  background: var(--accent);
  border: 1px solid var(--accent);
  color: #fff;
  font-size: 12px;
}

.mode-btn.exclusion {
  background: var(--danger-soft);
  border-color: #5c2a2c;
  color: #eb6d70;
}

.small {
  font-size: 12px;
}

.menu-wrap {
  position: relative;
}

.rule-menu {
  position: absolute;
  top: 30px;
  right: 0;
  width: 620px;
  max-height: 60vh;
  overflow-y: auto;
  padding: 8px;
  z-index: 5;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 4px 10px;
}

.menu-group-title {
  padding: 6px 8px 2px;
  color: var(--text-faint);
  font-size: 11px;
  letter-spacing: 0.06em;
}

.rule-menu-item {
  display: block;
  width: 100%;
  padding: 6px 10px;
  background: transparent;
  border: none;
  text-align: left;
  color: var(--text);
  font-size: 12.5px;
}

.rule-menu-item:hover {
  background: var(--control);
}
</style>
