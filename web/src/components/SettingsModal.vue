<script setup lang="ts">
import Icon from './Icon.vue'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, formatBytes } from '../api'
import {
  applySettings,
  askConfirm,
  store,
  toast,
  toastError,
} from '../store'
import { RATING_NAMES, type Settings } from '../types'

const cacheBytes = ref(0)
const cacheFiles = ref(0)
const cacheBusy = ref(false)

async function loadCache(): Promise<void> {
  try {
    const stats = await api.thumbnailCache()
    cacheBytes.value = stats.bytes
    cacheFiles.value = stats.files
  } catch (err) {
    toastError(err, '读取缓存信息失败')
  }
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') close()
}

onMounted(() => {
  void loadCache()
  window.addEventListener('keydown', onKey)
})

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

function close(): void {
  store.settingsOpen = false
}

function update(change: Partial<Settings>): void {
  // 本地立即生效, 由 store 在 1 秒后合并写回后端
  applySettings(change)
}

function setNumber(key: 'ThumbnailShortSideSize' | 'GridCellSize'
                         | 'DatabaseIdleCheckpointMinutes',
                   event: Event, min: number, max: number): void {
  const raw = Number((event.target as HTMLInputElement).value)
  if (!Number.isFinite(raw)) return
  const value = Math.min(max, Math.max(min, Math.round(raw)))
  update({ [key]: value })
}

function setSelectNumber(key: 'BlurLevel' | 'NewlyAddedImageLevel',
                         event: Event): void {
  update({ [key]: Number((event.target as HTMLSelectElement).value) })
}

function toggle(key: 'EnableThumbnail' | 'AutoRefresh' | 'SafeBrowsingMode'): void {
  update({ [key]: !store.settings[key] })
}

async function clearCache(): Promise<void> {
  const ok = await askConfirm(
    '清空缩略图缓存',
    '确定要删除全部已生成的缩略图吗？图片浏览时会按需重新生成。',
    { danger: true },
  )
  if (!ok) return
  cacheBusy.value = true
  try {
    await api.clearThumbnailCache()
    await loadCache()
    toast('缩略图缓存已清空', 'success')
  } catch (err) {
    toastError(err, '清空缓存失败')
  } finally {
    cacheBusy.value = false
  }
}

</script>

<template>
  <div class="overlay" @mousedown.self="close">
    <div class="modal panel">
      <div class="panel-title title-lg">
        <span>全局设置</span>
        <button class="icon-btn" @click="close">
          <Icon name="close" label="close" />
        </button>
      </div>

      <div class="body">
        <section class="group">
          <h3>缩略图设置</h3>
          <div class="line">
            <span class="label">使用缩略图预览</span>
            <div class="switch" :class="{ on: store.settings.EnableThumbnail }"
                 @click="toggle('EnableThumbnail')"></div>
          </div>
          <div class="line">
            <span class="label">缩略图短边长度</span>
            <input class="field num" type="number" min="64" max="1024"
                   :value="store.settings.ThumbnailShortSideSize"
                   @change="setNumber('ThumbnailShortSideSize', $event, 64, 1024)" />
            <span class="muted">px</span>
          </div>
          <div class="line">
            <span class="label">缩略图缓存占用</span>
            <span class="value">{{ formatBytes(cacheBytes) }} · {{ cacheFiles }} 个文件</span>
            <button class="btn small danger-solid" :disabled="cacheBusy" @click="clearCache">
              清空缩略图缓存
            </button>
          </div>
        </section>

        <section class="group">
          <h3>自动刷新</h3>
          <div class="line">
            <span class="label">启用自动刷新</span>
            <div class="switch" :class="{ on: store.settings.AutoRefresh }"
                 @click="toggle('AutoRefresh')"></div>
            <span class="muted small">目录内容变化时自动更新视图</span>
          </div>
        </section>

        <section class="group">
          <h3>浏览设置</h3>
          <div class="line">
            <span class="label">网格单元大小</span>
            <input class="field num" type="number" min="64" max="1024"
                   :value="store.settings.GridCellSize"
                   @change="setNumber('GridCellSize', $event, 64, 1024)" />
            <span class="muted">px</span>
          </div>
          <div class="line">
            <span class="label">安全浏览模式</span>
            <div class="switch" :class="{ on: store.settings.SafeBrowsingMode }"
                 @click="toggle('SafeBrowsingMode')"></div>
            <span class="muted small">仅显示 G 级图像</span>
          </div>
          <div class="line">
            <span class="label">开始模糊的等级</span>
            <select class="field select" :value="store.settings.BlurLevel"
                    @change="setSelectNumber('BlurLevel', $event)">
              <option v-for="(name, level) in RATING_NAMES" :key="name" :value="level">
                {{ name }}
              </option>
            </select>
          </div>
        </section>

        <section class="group">
          <h3>新增图片设置</h3>
          <div class="line">
            <span class="label">默认分级</span>
            <select class="field select" :value="store.settings.NewlyAddedImageLevel"
                    @change="setSelectNumber('NewlyAddedImageLevel', $event)">
              <option v-for="(name, level) in RATING_NAMES" :key="name" :value="level">
                {{ name }}
              </option>
            </select>
          </div>
        </section>

        <section class="group">
          <h3>数据库</h3>
          <div class="line">
            <span class="label">空闲合并 WAL</span>
            <input class="field num" type="number" min="0" max="1440"
                   :value="store.settings.DatabaseIdleCheckpointMinutes"
                   @change="setNumber('DatabaseIdleCheckpointMinutes', $event, 0, 1440)" />
            <span class="muted">分钟</span>
            <span class="muted small">0 表示关闭；修改后重启程序生效</span>
          </div>
        </section>
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
  align-items: flex-start;
  justify-content: center;
  padding-top: 40px;
  overflow-y: auto;
}

.modal {
  width: min(1180px, calc(100vw - 60px));
  background: #1b1b1b;
}

.title-lg {
  height: 48px;
  font-size: 20px;
  font-weight: 600;
}

.body {
  padding: 22px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.group {
  border: 1px solid var(--border);
  padding: 12px 16px 16px;
  background: var(--bg-elevated);
}

.group h3 {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 600;
  color: #e2e2e2;
}

.line {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 34px;
}

.label {
  min-width: 130px;
  color: var(--text-dim);
  font-size: 13px;
}

.value {
  color: var(--text);
  font-size: 13px;
  min-width: 180px;
}

.num {
  width: 110px;
}

.select {
  width: 170px;
}

.small {
  font-size: 12px;
}

.danger {
  color: #eb6d70;
  background: transparent;
  border-color: transparent;
}

.danger:hover:not(:disabled) {
  background: var(--danger-soft);
  border-color: #4d2a2b;
}
</style>
