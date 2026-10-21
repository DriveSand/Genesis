<script setup lang="ts">
import Icon from './Icon.vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api'
import { moveTo, store, toastError } from '../store'
import type { FolderEntry } from '../types'

const currentPath = ref('/')
const folders = ref<FolderEntry[]>([])
const selected = ref('')
const loading = ref(false)
const busy = ref(false)

const crumbs = computed(() => {
  const result = [{ name: '根', path: '/' }]
  if (currentPath.value !== '/') {
    let acc = ''
    for (const part of currentPath.value.split('/').filter(Boolean)) {
      acc += `/${part}`
      result.push({ name: part, path: acc })
    }
  }
  return result
})

const targetLabel = computed(() =>
  store.moveTargets.length === 1
    ? store.moveTargets[0].name
    : `${store.moveTargets.length} 个项目`,
)

async function load(path: string): Promise<void> {
  loading.value = true
  selected.value = ''
  try {
    const data = await api.contents(path, 500)
    folders.value = data.folders
    currentPath.value = path
  } catch (err) {
    toastError(err, '读取目录失败')
  } finally {
    loading.value = false
  }
}

function enter(folder: FolderEntry): void {
  const base = folder.path === '/' ? '' : folder.path
  void load(`${base}/${folder.name}`)
}

function up(): void {
  if (currentPath.value === '/') return
  const parts = currentPath.value.split('/').filter(Boolean)
  parts.pop()
  void load(parts.length ? `/${parts.join('/')}` : '/')
}

function close(): void {
  if (!busy.value) store.moveOpen = false
}

async function confirm(target: string): Promise<void> {
  busy.value = true
  try {
    await moveTo(target)
  } finally {
    busy.value = false
  }
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') close()
}

onMounted(() => {
  void load('/')
  window.addEventListener('keydown', onKey)
})

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="overlay" @mousedown.self="close">
    <div class="modal panel">
      <div class="panel-title">
        <span>移动 {{ targetLabel }}</span>
        <button class="icon-btn" :disabled="busy" @click="close">
          <Icon name="close" label="close" />
        </button>
      </div>

      <div class="nav">
        <button class="btn ghost small" :disabled="currentPath === '/'" @click="up">返回上级</button>
        <nav class="crumbs">
          <template v-for="(crumb, index) in crumbs" :key="crumb.path">
            <span v-if="index" class="sep">/</span>
            <span class="crumb" :class="{ active: index === crumbs.length - 1 }"
                  @click="load(crumb.path)">{{ crumb.name }}</span>
          </template>
        </nav>
      </div>

      <div class="list">
        <div v-if="loading" class="empty-tip">正在加载…</div>
        <div v-else-if="!folders.length" class="empty-tip">此目录下没有子文件夹</div>
        <template v-else>
          <div v-for="folder in folders" :key="folder.uuid" class="folder-row"
               :class="{ selected: selected === folder.name }"
               @click="selected = selected === folder.name ? '' : folder.name"
               @dblclick="enter(folder)">
            <Icon name="folder" class="folder-glyph" />
            <span>{{ folder.name }}</span>
            <span class="spacer"></span>
            <button class="btn ghost small" @click.stop="enter(folder)">进入</button>
          </div>
        </template>
      </div>

      <div class="footer">
        <span class="muted small">目标：{{ currentPath }}</span>
        <div class="spacer"></div>
        <button class="btn" :disabled="busy" @click="close">取消</button>
        <button class="btn" :disabled="busy" @click="confirm(currentPath)">
          移动到此处
        </button>
        <button class="btn accent" :disabled="busy || !selected"
                @click="confirm(currentPath === '/' ? `/${selected}` : `${currentPath}/${selected}`)">
          移动到「{{ selected || '未选择' }}」
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
  width: min(680px, calc(100vw - 60px));
  height: min(600px, calc(100vh - 90px));
  display: flex;
  flex-direction: column;
  background: #1b1b1b;
}

.nav {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border);
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 4px;
  overflow: hidden;
  white-space: nowrap;
}

.crumb {
  color: var(--text-dim);
  cursor: pointer;
}

.crumb:hover {
  color: var(--text);
}

.crumb.active {
  color: var(--text);
  cursor: default;
}

.sep {
  color: var(--text-faint);
}

.list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.folder-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  cursor: pointer;
  border: 1px solid transparent;
}

.folder-row:hover {
  background: var(--control);
}

.folder-row.selected {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.folder-glyph {
  width: 24px;
  height: 24px;
  color: var(--accent);
}

.footer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}

.small {
  font-size: 12px;
}
</style>
