<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { applySettings, store } from '../store'
import { BLUR_LEVELS, RATING_NAMES } from '../types'

const panelEl = ref<HTMLElement | null>(null)

// 右边缘对齐快速调整按钮, 顶边紧贴按钮下方
function reposition(): void {
  const trigger = document.querySelector('[data-panel="quick"]') as HTMLElement | null
  const panel = panelEl.value
  if (!trigger || !panel) return
  const rect = trigger.getBoundingClientRect()
  const width = panel.offsetWidth || 380
  const left = Math.max(10,
    Math.min(rect.right - width, window.innerWidth - width - 10))
  panel.style.left = `${left}px`
  panel.style.right = 'auto'
  panel.style.top = `${rect.bottom + 4}px`
}

watch(
  () => store.quickOpen,
  async (open) => {
    if (!open) return
    await nextTick()
    reposition()
  },
)

onMounted(() => {
  window.addEventListener('resize', reposition)
  // 组件以 v-if 挂载, 打开瞬间 watch 不会触发, 挂载后立即定位
  void nextTick(reposition)
})
onBeforeUnmount(() => window.removeEventListener('resize', reposition))

function setCellSize(event: Event): void {
  const raw = Number((event.target as HTMLInputElement).value)
  if (!Number.isFinite(raw)) return
  const value = Math.min(1024, Math.max(64, Math.round(raw)))
  applySettings({ GridCellSize: value })
}

function toggleLevel(level: number): void {
  const current = new Set(store.settings.ShowLevel)
  if (current.has(level) && current.size === 1) return
  if (current.has(level)) current.delete(level)
  else current.add(level)
  applySettings({ ShowLevel: [...current].sort((a, b) => a - b) })
}

function setBlurLevel(level: number): void {
  applySettings({ BlurLevel: level })
}

function toggleSafeBrowsing(): void {
  applySettings({ SafeBrowsingMode: !store.settings.SafeBrowsingMode })
}
</script>

<template>
  <div ref="panelEl" class="quick panel">
    <div class="quick-section">
      <label class="quick-label">网格单元大小</label>
      <div class="row">
        <input class="field size-input" type="number" min="64" max="1024"
               :value="store.settings.GridCellSize" @change="setCellSize" />
        <span class="muted">px</span>
      </div>
    </div>

    <div class="quick-section">
      <label class="quick-label">想要看见的图片等级</label>
      <div class="chips">
        <span v-for="(name, level) in RATING_NAMES" :key="name" class="chip"
              :class="{ active: store.settings.ShowLevel.includes(level) }"
              @click="toggleLevel(level)">{{ name }}</span>
      </div>
    </div>

    <div class="quick-section">
      <label class="quick-label">开始模糊的等级</label>
      <div class="chips">
        <!-- 旧配置可能仍为 0 (G) -->
        <span v-if="store.settings.BlurLevel === 0" class="chip active"
              @click="setBlurLevel(0)">G</span>
        <span v-for="level in BLUR_LEVELS" :key="level.value" class="chip"
              :class="{ active: store.settings.BlurLevel === level.value }"
              @click="setBlurLevel(level.value)">{{ level.label }}</span>
      </div>
    </div>

    <div class="quick-section quick-row">
      <label class="quick-label inline">安全浏览模式</label>
      <div class="switch" :class="{ on: store.settings.SafeBrowsingMode }"
           @click="toggleSafeBrowsing"></div>
    </div>
  </div>
</template>

<style scoped>
.quick {
  position: fixed;
  /* left/top 由 reposition() 按触发按钮的包围盒计算 */
  top: 50px;
  left: 10px;
  width: 380px;
  padding: 14px;
  z-index: 40;
}

.quick-section {
  padding: 8px 0 12px;
  border-bottom: 1px solid var(--border);
}

.quick-section:last-child {
  border-bottom: none;
  padding-bottom: 2px;
}

.quick-label {
  display: block;
  margin-bottom: 8px;
  color: #d0d0d0;
  font-size: 13px;
}

.quick-label.inline {
  margin-bottom: 0;
}

.quick-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.size-input {
  width: 120px;
}

.chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
</style>
