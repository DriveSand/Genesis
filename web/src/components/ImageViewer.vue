<script setup lang="ts">
import Icon from './Icon.vue'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, formatBytes, formatTime } from '../api'
import {
  closeViewer,
  concealFile,
  copyTextToClipboard,
  currentViewerFile,
  downloadSingle,
  imagePathOf,
  isBlurred,
  isFileRevealed,
  openEditor,
  reparseMetadata,
  store,
  viewerStep,
} from '../store'
import { RATING_COLORS, RATING_NAMES } from '../types'

const rotation = ref(0)
const zoom = ref(1)
const zoomHintVisible = ref(false)
const canvasEl = ref<HTMLElement | null>(null)
const imageEl = ref<HTMLImageElement | null>(null)
const MIN_ZOOM = 0.1
// 最大放大 5000% 这下应该没有人说看不清细节了，像素点看的清清楚楚
const MAX_ZOOM = 50
const ZOOM_STEP = 0.25
let zoomHintTimer: number | undefined

const WHEEL_STEP = 0.01
const WHEEL_FAST_STEP = 0.1
const WHEEL_ULTRA_STEP = 0.5
// 连续滑动触发时间，单位 ms
const WHEEL_RAPID_INTERVAL = 300
// 最大空闲时间，单位 ms
const WHEEL_RESET_DELAY = 500
// 连续快速滑动触发时间，单位 ms
const WHEEL_ULTRA_AFTER = 1500
let wheelLastAt = 0
let wheelDirection = 0
let wheelRapid = false
let wheelRapidSince = 0
let wheelResetTimer: number | undefined

watch(zoom, () => {
  zoomHintVisible.value = true
  if (zoomHintTimer) window.clearTimeout(zoomHintTimer)
  zoomHintTimer = window.setTimeout(() => {
    zoomHintVisible.value = false
  }, 1200)
})

const file = computed(() => currentViewerFile.value)
const imageUrl = computed(() =>
  file.value ? `${api.originalUrl(imagePathOf(file.value))}&v=${store.viewerBust}` : '',
)
const imageTransform = computed(() => locked.value
  ? `rotate(${rotation.value}deg) scale(1.3)`
  : `translate(${panX.value}px, ${panY.value}px) rotate(${rotation.value}deg) scale(${zoom.value})`,
)
const meta = computed(() => store.metadata)
const models = computed(() => store.models)
const ratingName = computed(() =>
  meta.value ? RATING_NAMES[meta.value.rating] ?? String(meta.value.rating) : '',
)
const ratingColor = computed(() =>
  meta.value ? RATING_COLORS[meta.value.rating] ?? '#0078d4' : '#0078d4',
)

// 查看器内的临时"显示"状态: 切换图片后复位, 不写回主界面
const viewerRevealed = ref(false)
const fileRating = computed(() => file.value?.rating ?? 0)
const sensitive = computed(() => file.value !== null && isBlurred(fileRating.value))
const locked = computed(() =>
  sensitive.value && !!file.value
  && !isFileRevealed(file.value) && !viewerRevealed.value,
)
const fileRatingName = computed(() =>
  RATING_NAMES[fileRating.value] ?? String(fileRating.value),
)
const fileRatingColor = computed(() =>
  RATING_COLORS[fileRating.value] ?? '#0078d4',
)

function revealViewer(): void {
  viewerRevealed.value = true
}

function reblur(): void {
  if (!file.value) return
  viewerRevealed.value = false
  concealFile(file.value)  // 同时取消主界面的"已显示"状态
  zoom.value = 1
  panX.value = 0
  panY.value = 0
}

const modelRows = computed(() => {
  const m = models.value
  if (!m) return []
  const rows: Array<{ name: string; version: string; url: string; weight?: number; kind: string }> = []
  for (const item of m.checkpoint) {
    rows.push({ name: item.model_name, version: item.version, url: item.model_url, kind: 'Checkpoint' })
  }
  for (const item of m.lora) {
    rows.push({
      name: item.model_name,
      version: item.version,
      url: item.model_url,
      weight: item.weight,
      kind: item.type === 1 ? 'LyCORIS' : 'LoRA',
    })
  }
  for (const item of m.embedding) {
    rows.push({ name: item.model_name, version: item.version, url: item.model_url, kind: 'Embedding' })
  }
  for (const item of m.vae) {
    rows.push({ name: item.model_name, version: item.version, url: item.model_url, kind: 'VAE' })
  }
  for (const item of m.upscale) {
    rows.push({ name: item.model_name, version: item.version, url: item.model_url, kind: 'Upscale' })
  }
  return rows
})

function download(): void {
  if (!file.value) return
  void downloadSingle({ kind: 'file', name: file.value.name, path: file.value.path })
}

function reparse(): void {
  if (file.value) void reparseMetadata(file.value)
}

function rotateLeft(): void {
  rotation.value = (rotation.value - 90 + 360) % 360
}

function rotateRight(): void {
  rotation.value = (rotation.value + 90) % 360
}

function zoomIn(): void {
  if (locked.value) return
  zoom.value = Math.min(MAX_ZOOM, zoom.value + ZOOM_STEP)
}

function zoomOut(): void {
  if (locked.value) return
  zoom.value = Math.max(MIN_ZOOM, zoom.value - ZOOM_STEP)
}

function toggleZoom(): void {
  if (locked.value) return
  zoom.value = zoom.value === 1 ? 2 : 1
}

function clampZoom(value: number): number {
  return Math.round(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value)) * 100) / 100
}

function resetWheelRapid(): void {
  wheelRapid = false
  wheelRapidSince = 0
  wheelLastAt = 0
  wheelDirection = 0
  if (wheelResetTimer) {
    window.clearTimeout(wheelResetTimer)
    wheelResetTimer = undefined
  }
}

function onWheel(event: WheelEvent): void {
  if (locked.value) return  // 模糊状态下禁止缩放
  const direction = event.deltaY < 0 ? 1 : -1
  const now = performance.now()
  // 同方向且间隔足够短视为连续快速滑动, 步进临时放大到 10%
  const rapid = now - wheelLastAt <= WHEEL_RAPID_INTERVAL
    && direction === wheelDirection
  if (rapid && !wheelRapid) wheelRapidSince = now
  else if (!rapid) wheelRapidSince = 0
  wheelRapid = rapid
  wheelLastAt = now
  wheelDirection = direction
  if (wheelResetTimer) window.clearTimeout(wheelResetTimer)
  wheelResetTimer = window.setTimeout(resetWheelRapid, WHEEL_RESET_DELAY)
  // 快速滑动持续设置时间后, 单步提升到 50%
  const ultra = rapid && wheelRapidSince > 0
    && now - wheelRapidSince >= WHEEL_ULTRA_AFTER
  const step = ultra ? WHEEL_ULTRA_STEP
    : rapid ? WHEEL_FAST_STEP : WHEEL_STEP
  const oldZoom = zoom.value
  const nextZoom = clampZoom(oldZoom + direction * step)
  // 以鼠标指针为缩放中心: 保持指针下方的图片位置不变
  const canvas = canvasEl.value
  if (canvas && oldZoom > 0 && nextZoom !== oldZoom) {
    const rect = canvas.getBoundingClientRect()
    const px = event.clientX - rect.left - rect.width / 2
    const py = event.clientY - rect.top - rect.height / 2
    const ratio = nextZoom / oldZoom
    panX.value += (px - panX.value) * (1 - ratio)
    panY.value += (py - panY.value) * (1 - ratio)
  }
  zoom.value = nextZoom
}

// ---------------- 拖动 ----------------

const panX = ref(0)
const panY = ref(0)
const dragging = ref(false)
let dragState = { x: 0, y: 0, px: 0, py: 0, moved: false }

/**
 * 限制平移范围: 图片边缘最多贴到查看区边缘。
 *
 * 放大后在边缘继续缩小, 旧的平移量可能把图片完全推出查看区而无法拖回;
 * 每次缩放/旋转/拖动后都按当前缩放与旋转后的包围盒重新夹取平移量。
 */
function clampPan(): void {
  const image = imageEl.value
  const canvas = canvasEl.value
  if (!image || !canvas) return
  const scale = zoom.value
  const rad = rotation.value * Math.PI / 180
  const sin = Math.abs(Math.sin(rad))
  const cos = Math.abs(Math.cos(rad))
  const width = image.clientWidth * scale
  const height = image.clientHeight * scale
  // 旋转后的轴对齐包围盒
  const boundsW = width * cos + height * sin
  const boundsH = width * sin + height * cos
  // 图片大于查看区时限制边缘不越过视口; 小于查看区时允许拖动但保持完整可见
  const maxX = Math.abs(canvas.clientWidth - boundsW) / 2
  const maxY = Math.abs(canvas.clientHeight - boundsH) / 2
  panX.value = Math.max(-maxX, Math.min(maxX, panX.value))
  panY.value = Math.max(-maxY, Math.min(maxY, panY.value))
}

function onImageMouseDown(event: MouseEvent): void {
  if (locked.value) return
  dragging.value = true
  dragState = {
    x: event.clientX, y: event.clientY,
    px: panX.value, py: panY.value, moved: false,
  }
  event.preventDefault()
}

function onWindowMouseMove(event: MouseEvent): void {
  if (!dragging.value) return
  const dx = event.clientX - dragState.x
  const dy = event.clientY - dragState.y
  if (!dragState.moved && Math.abs(dx) + Math.abs(dy) > 3) {
    dragState.moved = true
  }
  panX.value = dragState.px + dx
  panY.value = dragState.py + dy
  clampPan()
}

function onWindowMouseUp(): void {
  dragging.value = false
}

function onImageClick(): void {
  if (dragState.moved) {
    dragState.moved = false
    return
  }
  toggleZoom()
}

function onKey(event: KeyboardEvent): void {
  if (!store.viewerOpen || store.editOpen) return
  if (event.key === 'Escape') closeViewer()
  else if (event.key === 'ArrowLeft') void viewerStep(-1)
  else if (event.key === 'ArrowRight') void viewerStep(1)
}

watch(
  () => store.viewerIndex,
  () => {
    resetWheelRapid()
    viewerRevealed.value = false  // 切换图片后重新模糊
    rotation.value = 0
    zoom.value = 1
    panX.value = 0
    panY.value = 0
  },
)

watch(locked, (value) => {
  if (!value) return
  zoom.value = 1
  panX.value = 0
  panY.value = 0
})

watch(zoom, () => {
  // 任何倍率下都允许平移, 只夹取到仍可见的范围
  clampPan()
})

watch(rotation, () => {
  clampPan()
})

onMounted(() => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('mousemove', onWindowMouseMove)
  window.addEventListener('mouseup', onWindowMouseUp)
})

onBeforeUnmount(() => {
  resetWheelRapid()
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('mousemove', onWindowMouseMove)
  window.removeEventListener('mouseup', onWindowMouseUp)
})
</script>

<template>
  <div class="viewer">
    <div ref="canvasEl" class="canvas" @wheel.prevent="onWheel">
      <button class="nav-arrow left" title="上一张" @click="viewerStep(-1)">
        <Icon name="left" label="prev" />
      </button>
      <img v-if="file" ref="imageEl" class="main-image"
           :class="{ dragging, blurred: locked, locked }" :src="imageUrl" :alt="file.name"
           :style="{ transform: imageTransform }"
           @load="clampPan" @mousedown="onImageMouseDown" @click="onImageClick" />
      <button class="nav-arrow right" title="下一张" @click="viewerStep(1)">
        <Icon name="right" label="next" />
      </button>
      <div v-if="locked" class="locked-overlay">
        <div class="lock-content">
          <Icon name="protection" class="lock-icon" label="locked" />
          <span class="badge lock-badge" :style="{ background: fileRatingColor }">{{ fileRatingName }}</span>
          <span class="lock-text">已模糊处理该图像</span>
          <button class="btn accent lock-show-btn" @click.stop="revealViewer">显示</button>
        </div>
      </div>
      <div v-if="zoomHintVisible && !locked" class="zoom-hint">{{ Math.round(zoom * 100) }}%</div>
      <div v-if="store.viewerLoading" class="loading">正在读取信息…</div>
    </div>

    <div class="rail">
      <button class="rail-btn" title="关闭 (Esc)" @click="closeViewer">
        <Icon name="close" label="close" />
      </button>
      <button class="rail-btn" title="放大" :disabled="locked || zoom >= MAX_ZOOM" @click="zoomIn">
        <Icon name="zoom-in" label="zoom in" />
      </button>
      <button class="rail-btn" title="缩小" :disabled="locked || zoom <= MIN_ZOOM" @click="zoomOut">
        <Icon name="zoom-out" label="zoom out" />
      </button>
      <button class="rail-btn" title="向左旋转 90°" @click="rotateLeft">
        <Icon name="rotate-flip" label="rotate left" />
      </button>
      <button class="rail-btn" title="向右旋转 90°" @click="rotateRight">
        <Icon name="rotate" label="rotate right" />
      </button>
      <button v-if="sensitive" class="rail-btn" title="重新模糊"
              :disabled="locked" @click="reblur">
        <Icon name="preview-close" label="re-blur" />
      </button>
    </div>

    <aside class="info">
      <div class="actions">
        <button class="btn small accent" @click="openEditor()">
          <Icon name="edit" class="icon" />编辑信息
        </button>
        <button class="btn small" @click="reparse">
          <Icon name="refresh" class="icon" />重新读取元数据
        </button>
        <button class="btn small" @click="download">
          <Icon name="download" class="icon" />下载图像
        </button>
      </div>

      <div class="scroll">
        <div class="rows">
          <div class="kv"><span class="k">文件名</span><span class="v">{{ meta?.file_name || file?.name }}</span></div>
          <div class="kv"><span class="k">分级</span>
            <span class="badge" :style="{ background: ratingColor }">{{ ratingName || '—' }}</span>
          </div>
          <div class="kv"><span class="k">文件大小</span><span class="v">{{ formatBytes(meta?.file_size ?? file?.file_size ?? 0) }}</span></div>
          <div class="kv"><span class="k">分辨率</span>
            <span class="v">{{ meta && meta.width ? `${meta.width} × ${meta.height}` : '—' }}</span>
          </div>
          <div class="kv"><span class="k">添加时间</span><span class="v">{{ formatTime(meta?.added_at ?? file?.add_time ?? 0) }}</span></div>
        </div>

        <section class="block">
          <div class="block-head">
            <h4>正向提示</h4>
            <button class="icon-btn small" title="复制正向提示"
                    @click="copyTextToClipboard(meta?.positive_prompt ?? '', '正向提示')">
              <Icon name="copy" label="copy" />
            </button>
          </div>
          <p class="prompt">{{ meta?.positive_prompt || '—' }}</p>
        </section>
        <section class="block">
          <div class="block-head">
            <h4>反向提示</h4>
            <button class="icon-btn small" title="复制反向提示"
                    @click="copyTextToClipboard(meta?.negative_prompt ?? '', '反向提示')">
              <Icon name="copy" label="copy" />
            </button>
          </div>
          <p class="prompt">{{ meta?.negative_prompt || '—' }}</p>
        </section>
        <section class="block">
          <h4>生成参数</h4>
          <div class="kv"><span class="k">工具</span><span class="v">{{ meta?.tool || '—' }}</span></div>
          <div class="kv"><span class="k">步数</span><span class="v">{{ meta?.steps || '—' }}</span></div>
          <div class="kv"><span class="k">CFG</span><span class="v">{{ meta?.cfg || '—' }}</span></div>
          <div class="kv"><span class="k">采样器</span><span class="v">{{ meta?.sampler || '—' }}</span></div>
          <div class="kv"><span class="k">调度器</span><span class="v">{{ meta?.scheduler || '—' }}</span></div>
          <div class="kv"><span class="k">随机种</span><span class="v">{{ meta?.seed ?? '—' }}</span></div>
        </section>
        <section class="block">
          <h4>模型</h4>
          <div v-if="!modelRows.length" class="muted">—</div>
          <div v-for="(row, index) in modelRows" :key="index" class="model">
            <div class="model-top">
              <span class="model-name">{{ row.name }}</span>
              <span class="model-tags">
                <span v-if="row.weight !== undefined" class="weight">{{ row.weight }}</span>
                <span class="kind">{{ row.kind }}</span>
              </span>
            </div>
            <div class="model-sub">{{ row.version || '—' }}</div>
          </div>
        </section>
        <section class="block">
          <h4>备注</h4>
          <p class="note">{{ meta?.note || '—' }}</p>
        </section>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.viewer {
  position: fixed;
  inset: 0;
  z-index: 90;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 44px 360px;
  background: #0b0b0b;
}

.rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 10px 0;
  border-left: 1px solid var(--border);
  border-right: 1px solid var(--border);
  background: var(--bg-elevated);
}

.rail-btn {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-dim);
}

.rail-btn:hover {
  background: var(--control);
  border-color: var(--border);
  color: var(--text);
}

.rail-btn .app-icon {
  width: 18px;
  height: 18px;
}

.rail-btn:disabled {
  opacity: 0.35;
  cursor: default;
}

.canvas {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  cursor: default;
  background:
    radial-gradient(circle at 50% 40%, rgba(255, 255, 255, 0.03), transparent 60%),
    #0e0e0e;
}

.main-image {
  max-width: calc(100% - 96px);
  max-height: calc(100% - 48px);
  object-fit: contain;
  cursor: grab;
  transition: transform 0.16s ease;
  box-shadow: 0 0 60px rgba(0, 0, 0, 0.7);
  user-select: none;
}

.main-image.dragging {
  cursor: grabbing;
  transition: none;
}

/* 实测模糊半径为 128px 时，你摘下眼镜看/离远看/截图发聊天软件看，都看不清具体内容 */
.main-image.blurred {
  filter: blur(128px) saturate(0.7) brightness(0.75);
}

.main-image.locked {
  cursor: default;
}

.locked-overlay {
  position: absolute;
  inset: 0;
  z-index: 3;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  background: rgba(16, 16, 16, 0.55);
  pointer-events: none;
}

.locked-overlay .btn {
  pointer-events: auto;
}

/* 提示内容整体约占展示区域的 30% */
.lock-content {
  width: 30%;
  min-width: 300px;
  max-width: 560px;
  min-height: 30%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: space-evenly;
  gap: 12px;
}

.lock-icon {
  width: clamp(80px, 12vmin, 180px);
  height: auto;
  color: var(--warn);
}

.lock-badge {
  height: auto;
  padding: 4px clamp(14px, 2vmin, 28px);
  font-size: clamp(16px, 2.2vmin, 28px);
  line-height: 1.4;
}

.lock-text {
  color: var(--text);
  font-size: clamp(15px, 2vmin, 24px);
}

.lock-show-btn {
  height: clamp(40px, 5.5vmin, 68px);
  padding: 0 clamp(22px, 3vmin, 40px);
  font-size: clamp(16px, 2.2vmin, 26px);
}

.nav-arrow {
  position: absolute;
  z-index: 2;  /* 高于 transform 后的图片, 避免左箭头被遮挡 */
  top: 50%;
  transform: translateY(-50%);
  width: 42px;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(20, 20, 20, 0.55);
  border: 1px solid var(--border);
  opacity: 0;
  transition: opacity 0.15s ease;
}

.canvas:hover .nav-arrow {
  opacity: 1;
}

.nav-arrow.left {
  left: 14px;
}

.nav-arrow.right {
  right: 14px;
}

.nav-arrow {
  color: var(--text-dim);
}

.nav-arrow:hover {
  color: var(--text);
}

.nav-arrow .app-icon {
  width: 20px;
  height: 20px;
}

.zoom-hint {
  position: absolute;
  top: 14px;
  left: 14px;
  padding: 4px 10px;
  background: rgba(16, 16, 16, 0.82);
  border: 1px solid var(--border);
  font-size: 13px;
  color: var(--text);
  letter-spacing: 0.04em;
  pointer-events: none;
}

.loading {
  position: absolute;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  color: var(--text-dim);
  font-size: 12px;
}

.info {
  display: flex;
  flex-direction: column;
  background: var(--bg-elevated);
  min-height: 0;
}

.actions {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}

.scroll {
  flex: 1;
  overflow-y: auto;
  padding: 0 12px 20px;
}

.rows {
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}

.kv {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 24px;
  font-size: 12px;
}

.kv .k {
  width: 84px;
  flex: none;
  color: var(--text-faint);
}

.kv .v {
  color: var(--text);
  overflow-wrap: anywhere;
}

.block {
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}

.block h4 {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim);
  letter-spacing: 0.04em;
}

.block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.block-head h4 {
  margin: 0;
}

.block-head .icon-btn {
  width: 24px;
  height: 24px;
}

.prompt,
.note {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: #d6d6d6;
  white-space: pre-wrap;
  word-break: break-word;
}

.model {
  padding: 6px 0;
  border-bottom: 1px dashed #303030;
}

.model:last-child {
  border-bottom: none;
}

.model-top {
  display: flex;
  align-items: center;
  gap: 6px;
}

.model-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: #f0f0f0;
}

.model-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}

.weight {
  padding: 0 5px;
  font-size: 11px;
  color: #f0b94d;
  border: 1px solid #5c4a1f;
}

.kind {
  padding: 0 6px;
  font-size: 11px;
  color: #fff;
  background: var(--accent);
}

.model-sub {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-faint);
}
</style>
