<script setup lang="ts">
import Icon from './Icon.vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError, api, formatBytes, sha256OfFile } from '../api'
import { loadContents, store, toast, toastError } from '../store'

interface UploadItem {
  id: number
  file: File
  relPath: string
  target: string
  status: 'pending' | 'hashing' | 'waiting' | 'uploading' | 'success' | 'error'
  progress: number
  message: string
}

type StructureMode = 'flat' | 'tree'

const items = ref<UploadItem[]>([])
const running = ref(false)
const dragOver = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const folderInput = ref<HTMLInputElement | null>(null)
let seq = 0

const structureMode = ref<StructureMode | null>(null)
const structureDialogOpen = ref(false)
const conflictDialogOpen = ref(false)
const conflicts = ref<string[]>([])
const conflictChoices = ref<Record<string, 'merge' | 'new'>>({})
let pendingTodo: UploadItem[] = []
let folderCache: Record<string, string> = {}

const totalSize = computed(() => items.value.reduce((sum, item) => sum + item.file.size, 0))
const doneCount = computed(() => items.value.filter((i) => i.status === 'success').length)
const hasFolderItems = computed(() => items.value.some((i) => i.relPath.includes('/')))

// ---------------- 等待上传列表: 树状结构预览 ----------------

interface TreeRow {
  key: string
  kind: 'dir' | 'file'
  depth: number
  label: string
  size: number
  item?: UploadItem
}

/** 按目录层级展开待上传项; 扁平结构下退化为单层文件列表。 */
const treeRows = computed<TreeRow[]>(() => {
  if (structureMode.value !== 'tree' || !hasFolderItems.value) return []
  interface DirNode {
    name: string
    dirs: Map<string, DirNode>
    files: UploadItem[]
    size: number
  }
  const makeNode = (name: string): DirNode => ({
    name, dirs: new Map(), files: [], size: 0,
  })
  const root = makeNode('')
  for (const item of items.value) {
    const parts = item.relPath.split('/').filter(Boolean)
    parts.pop()
    let node = root
    node.size += item.file.size
    for (const part of parts) {
      let child = node.dirs.get(part)
      if (!child) {
        child = makeNode(part)
        node.dirs.set(part, child)
      }
      child.size += item.file.size
      node = child
    }
    node.files.push(item)
  }
  const rows: TreeRow[] = []
  const walk = (node: DirNode, depth: number, prefix: string): void => {
    const dirs = [...node.dirs.values()]
      .sort((a, b) => a.name.localeCompare(b.name))
    for (const dir of dirs) {
      const path = prefix ? `${prefix}/${dir.name}` : dir.name
      rows.push({
        key: `dir:${path}`, kind: 'dir', depth,
        label: dir.name, size: dir.size,
      })
      walk(dir, depth + 1, path)
    }
    const files = [...node.files]
      .sort((a, b) => a.file.name.localeCompare(b.file.name))
    for (const item of files) {
      rows.push({
        key: `file:${item.id}`, kind: 'file', depth,
        label: item.relPath.split('/').pop() ?? item.file.name,
        size: item.file.size, item,
      })
    }
  }
  walk(root, 0, '')
  return rows
})

// ---------------------------------------------------------------------- #
// 文件收集 (普通文件 / 文件夹 / 拖拽目录)
// ---------------------------------------------------------------------- #

function addEntries(entries: Array<{ file: File; relPath: string }>): void {
  for (const { file, relPath } of entries) {
    items.value.push({
      id: ++seq,
      file,
      relPath: relPath.replace(/\\/g, '/'),
      target: store.path,
      status: 'pending',
      progress: 0,
      message: '',
    })
  }
  if (entries.some((e) => e.relPath.includes('/')) && structureMode.value === null) {
    structureDialogOpen.value = true
  }
}

function onPick(event: Event): void {
  const input = event.target as HTMLInputElement
  if (input.files) {
    addEntries(Array.from(input.files).map((file) => ({
      file,
      relPath: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
    })))
  }
  input.value = ''
}

async function walkEntry(
  entry: FileSystemEntry,
  prefix: string,
  out: Array<{ file: File; relPath: string }>,
): Promise<void> {
  if (entry.isFile) {
    const fileEntry = entry as FileSystemFileEntry
    const file = await new Promise<File>((resolve, reject) => {
      fileEntry.file(resolve, reject)
    })
    out.push({ file, relPath: prefix ? `${prefix}/${file.name}` : file.name })
    return
  }
  if (entry.isDirectory) {
    const dirEntry = entry as FileSystemDirectoryEntry
    const dirPrefix = prefix ? `${prefix}/${entry.name}` : entry.name
    const reader = dirEntry.createReader()
    let batch: FileSystemEntry[] = []
    do {
      batch = await new Promise<FileSystemEntry[]>((resolve, reject) => {
        reader.readEntries(resolve, reject)
      })
      for (const child of batch) await walkEntry(child, dirPrefix, out)
    } while (batch.length)
  }
}

function onDrop(event: DragEvent): void {
  dragOver.value = false
  const transfer = event.dataTransfer
  if (!transfer) return
  const entries: Array<{ file: File; relPath: string }> = []
  const transferItems = Array.from(transfer.items || [])
  const supportsEntries = transferItems.length > 0
    && typeof (transferItems[0] as DataTransferItem & {
      webkitGetAsEntry?: () => FileSystemEntry | null
    }).webkitGetAsEntry === 'function'
  if (!supportsEntries) {
    addEntries(Array.from(transfer.files).map((file) => ({ file, relPath: file.name })))
    return
  }
  const tasks: Array<Promise<void>> = []
  for (const item of transferItems) {
    if (item.kind !== 'file') continue
    const entry = (item as DataTransferItem & {
      webkitGetAsEntry?: () => FileSystemEntry | null
    }).webkitGetAsEntry?.()
    if (entry) tasks.push(walkEntry(entry, '', entries))
  }
  void Promise.all(tasks).then(() => {
    if (entries.length) addEntries(entries)
  })
}

function removeItem(id: number): void {
  if (running.value) return
  items.value = items.value.filter((i) => i.id !== id)
}

function close(): void {
  if (running.value) return
  store.uploadOpen = false
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape' && !structureDialogOpen.value && !conflictDialogOpen.value) {
    close()
  }
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

// ---------------------------------------------------------------------- #
// 上传结构选择
// ---------------------------------------------------------------------- #

function chooseStructure(mode: StructureMode): void {
  // 仅记录结构选择, 回到等待上传阶段; 由用户确认后再开始上传
  structureMode.value = mode
  structureDialogOpen.value = false
}

// ---------------------------------------------------------------------- #
// 文件夹冲突探测与创建
// ---------------------------------------------------------------------- #

function folderPathsOf(todo: UploadItem[]): string[] {
  const paths = new Set<string>()
  for (const item of todo) {
    const parts = item.relPath.split('/')
    parts.pop()
    let acc = ''
    for (const part of parts) {
      acc = acc ? `${acc}/${part}` : part
      paths.add(acc)
    }
  }
  return [...paths].sort((a, b) =>
    a.split('/').length - b.split('/').length || a.localeCompare(b))
}

async function ensureFolders(relDir: string): Promise<string> {
  if (!relDir) return store.path
  const cachedAll = folderCache[relDir]
  if (cachedAll) return cachedAll
  let parent = store.path
  let original = ''
  for (const name of relDir.split('/').filter(Boolean)) {
    original = original ? `${original}/${name}` : name
    const cached = folderCache[original]
    if (cached) {
      parent = cached
      continue
    }
    try {
      const res = await api.createFolder(parent, name)
      folderCache[original] = res.new_path
      parent = res.new_path
    } catch (err) {
      const conflict = err instanceof ApiError && err.code === 40901
      if (!conflict) throw err
      if (conflictChoices.value[original] === 'new') {
        const res = await api.createFolder(parent, name, true)
        folderCache[original] = res.new_path
        parent = res.new_path
      } else {
        const merged = parent === '/' ? `/${name}` : `${parent}/${name}`
        folderCache[original] = merged
        parent = merged
      }
    }
  }
  return parent
}

function setAllConflicts(choice: 'merge' | 'new'): void {
  for (const path of conflicts.value) conflictChoices.value[path] = choice
}

function cancelConflicts(): void {
  conflictDialogOpen.value = false
  pendingTodo = []
}

function confirmConflicts(): void {
  conflictDialogOpen.value = false
  void proceedUpload(pendingTodo)
}

// ---------------------------------------------------------------------- #
// 上传执行
// ---------------------------------------------------------------------- #

async function start(): Promise<void> {
  if (running.value || !items.value.length) return
  const todo = items.value.filter((i) => i.status === 'pending' || i.status === 'error')
  if (!todo.length) return
  const foldersInvolved = todo.some((i) => i.relPath.includes('/'))
  if (foldersInvolved && structureMode.value === null) {
    pendingTodo = todo
    structureDialogOpen.value = true
    return
  }
  if (foldersInvolved && structureMode.value === 'tree') {
    try {
      const existing = (await api.checkFolders(store.path, folderPathsOf(todo))).existing
      if (existing.length) {
        conflicts.value = existing
        conflictChoices.value = Object.fromEntries(
          existing.map((path) => [path, 'merge' as const]))
        pendingTodo = todo
        conflictDialogOpen.value = true
        return
      }
    } catch (err) {
      toastError(err, '检查文件夹失败')
      return
    }
  }
  await proceedUpload(todo)
}

async function uploadOne(
  item: UploadItem,
  taskId: string,
  token: string,
  chunkSize: number,
): Promise<void> {
  const total = Math.max(1, Math.ceil(item.file.size / chunkSize))
  item.status = 'uploading'
  for (let index = 0; index < total; index += 1) {
    const chunk = item.file.slice(index * chunkSize, (index + 1) * chunkSize)
    const form = new FormData()
    form.append('task_id', taskId)
    form.append('token', token)
    form.append('chunk_index', String(index))
    form.append('total_chunks', String(total))
    form.append('file', chunk, item.file.name)
    const result = await api.executeUpload(form)
    if (result.status === 'fail') {
      throw new Error(result.error || '上传失败')
    }
    item.progress = Math.round(((index + 1) / total) * 100)
    if (result.status === 'success') {
      item.status = 'success'
      item.message = '上传成功'
    }
  }
  if (item.status !== 'success') item.status = 'success'
}

async function proceedUpload(todo: UploadItem[]): Promise<void> {
  if (running.value || !todo.length) return
  running.value = true
  try {
    folderCache = {}
    // 1) 计算每个文件的实际目标目录 (树状结构按文件夹映射)
    const byTarget = new Map<string, UploadItem[]>()
    for (const item of todo) {
      let target = store.path
      if (structureMode.value === 'tree' && item.relPath.includes('/')) {
        const parts = item.relPath.split('/')
        parts.pop()
        target = await ensureFolders(parts.join('/'))
      }
      item.target = target
      const list = byTarget.get(target) ?? []
      list.push(item)
      byTarget.set(target, list)
    }

    // 2) 计算 SHA256
    for (const item of todo) {
      item.status = 'hashing'
      item.message = '计算 SHA256…'
      item.progress = 0
    }
    const preparedByTarget = new Map<string, Array<{ item: UploadItem; sha256: string }>>()
    for (const item of todo) {
      try {
        const sha256 = await sha256OfFile(item.file, (ratio) => {
          item.progress = Math.round(ratio * 100)
        })
        const list = preparedByTarget.get(item.target) ?? []
        list.push({ item, sha256 })
        preparedByTarget.set(item.target, list)
        item.status = 'waiting'
        item.message = '等待后端准备…'
      } catch (err) {
        item.status = 'error'
        item.message = err instanceof Error ? err.message : '读取文件失败'
      }
    }

    // 3) 按目标目录分组准备并上传
    for (const [target, prepared] of preparedByTarget) {
      if (!prepared.length) continue
      const task = await api.prepareUpload(
        target,
        prepared.map(({ item, sha256 }) => ({
          name: item.file.name,
          mime: item.file.type || 'application/octet-stream',
          sha256,
          size: item.file.size,
        })),
      )
      for (let index = 0; index < prepared.length; index += 1) {
        const { item } = prepared[index]
        const result = task.results[index]
        if (!result) {
          item.status = 'error'
          item.message = '后端未返回该文件的上传任务'
          continue
        }
        if (result.duplicate) {
          item.status = 'success'
          item.progress = 100
          item.message = '秒传成功'
          continue
        }
        if (!result.token) {
          item.status = 'error'
          item.message = '未获得上传 token'
          continue
        }
        try {
          await uploadOne(item, task.task_id, result.token, task.chunk_size)
        } catch (err) {
          item.status = 'error'
          item.message = err instanceof Error ? err.message : '上传失败'
        }
      }
    }

    if (items.value.every((i) => i.status === 'success')) {
      toast(`已上传 ${doneCount.value} 个文件`, 'success')
    } else {
      toast('部分文件上传失败，请查看列表', 'error')
    }
    await loadContents(store.path, false)
  } catch (err) {
    toastError(err, '上传失败')
  } finally {
    running.value = false
    pendingTodo = []
  }
}

function statusText(item: UploadItem): string {
  switch (item.status) {
    case 'pending': return '等待上传'
    case 'hashing': return '计算 SHA256…'
    case 'waiting': return '准备中…'
    case 'uploading': return `上传中 ${item.progress}%`
    case 'success': return item.message || '完成'
    case 'error': return item.message || '失败'
    default: return ''
  }
}
</script>

<template>
  <div class="overlay" @mousedown.self="close">
    <div class="modal panel">
      <div class="panel-title">
        <span>上传文件 到 {{ store.path }}</span>
        <button class="icon-btn" :disabled="running" @click="close">
          <Icon name="close" label="close" />
        </button>
      </div>

      <div class="body">
        <div class="dropzone" :class="{ over: dragOver }"
             @dragover.prevent="dragOver = true"
             @dragleave.prevent="dragOver = false"
             @drop.prevent="onDrop"
             @click="fileInput?.click()">
          <Icon name="upload" class="drop-icon" label="upload" />
          <div>拖入文件或点击选择</div>
          <div class="muted small">支持图片、视频与文件夹；相同内容将秒传</div>
          <div class="pick-actions">
            <button class="btn small" @click.stop="fileInput?.click()">选择文件</button>
            <button class="btn small" @click.stop="folderInput?.click()">选择文件夹</button>
          </div>
          <!-- 隐藏 input 的合成 click 会冒泡到拖放区; 必须在自身阻断,
               否则点击"选择文件夹"会先打开文件选择框 -->
          <input ref="fileInput" type="file" multiple hidden
                 @click.stop @change="onPick" />
          <input ref="folderInput" type="file" webkitdirectory multiple hidden
                 @click.stop @change="onPick" />
        </div>

        <!-- 树状结构: 等待上传阶段预览目录层级与各文件大小 -->
        <div v-if="items.length && treeRows.length" class="list tree-list">
          <div class="tree-root">
            <Icon name="folder" class="tree-folder" />
            <span class="tree-name">{{ store.path }}</span>
            <span class="muted small">{{ formatBytes(totalSize) }}</span>
          </div>
          <div v-for="row in treeRows" :key="row.key" class="tree-row"
               :class="row.kind" :style="{ paddingLeft: `${10 + row.depth * 18}px` }">
            <template v-if="row.kind === 'dir'">
              <Icon name="folder" class="tree-folder" />
              <span class="tree-name" :title="row.label">{{ row.label }}</span>
              <span class="muted small">{{ formatBytes(row.size) }}</span>
            </template>
            <template v-else>
              <span class="tree-file-name" :title="row.item?.relPath">{{ row.label }}</span>
              <span class="muted small">{{ formatBytes(row.size) }}</span>
              <span class="tree-status" :class="row.item?.status">
                {{ row.item ? statusText(row.item) : '' }}
              </span>
              <button v-if="!running" class="icon-btn small"
                      @click="row.item && removeItem(row.item.id)">
                <Icon name="close" label="remove" />
              </button>
            </template>
          </div>
        </div>

        <div v-else-if="items.length" class="list">
          <div v-for="item in items" :key="item.id" class="item">
            <div class="item-head">
              <span class="item-name" :title="item.relPath">{{ item.relPath }}</span>
              <span class="muted small">{{ formatBytes(item.file.size) }}</span>
              <button v-if="!running" class="icon-btn small" @click="removeItem(item.id)">
                <Icon name="close" label="remove" />
              </button>
            </div>
            <div class="progress">
              <div class="bar" :class="item.status"
                   :style="{ width: `${item.progress}%` }"></div>
            </div>
            <div class="item-status" :class="item.status">{{ statusText(item) }}</div>
          </div>
        </div>
        <div v-else class="muted small center">尚未选择文件</div>
      </div>

      <div class="footer">
        <span class="muted small">
          {{ items.length }} 个文件 · {{ formatBytes(totalSize) }}
          <template v-if="doneCount">· 已完成 {{ doneCount }}</template>
          <template v-if="hasFolderItems && structureMode">
            · {{ structureMode === 'tree' ? '树状结构' : '扁平结构' }}
          </template>
        </span>
        <div class="spacer"></div>
        <button class="btn" :disabled="running" @click="close">关闭</button>
        <button class="btn accent" :disabled="running || !items.length" @click="start">
          {{ running ? '上传中…' : '开始上传' }}
        </button>
      </div>
    </div>

    <!-- 上传结构选择 -->
    <div v-if="structureDialogOpen" class="dialog-layer" @mousedown.self="structureDialogOpen = false">
      <div class="dialog panel">
        <div class="panel-title">选择上传结构</div>
        <div class="structure-options">
          <button class="structure-card" @click="chooseStructure('flat')">
            <Icon name="flat-structure" class="structure-icon" label="flat" />
            <span class="structure-name">扁平结构</span>
            <span class="muted small">忽略文件夹结构，所有文件放入 {{ store.path }}</span>
          </button>
          <button class="structure-card" @click="chooseStructure('tree')">
            <Icon name="tree-structure" class="structure-icon" label="tree" />
            <span class="structure-name">树状结构</span>
            <span class="muted small">保留文件夹结构，在目标位置重建目录</span>
          </button>
        </div>
      </div>
    </div>

    <!-- 同名文件夹处理 -->
    <div v-if="conflictDialogOpen" class="dialog-layer" @mousedown.self="cancelConflicts">
      <div class="dialog panel">
        <div class="panel-title">目标位置存在同名文件夹</div>
        <div class="dialog-body">
          <p class="muted small">请选择每个同名文件夹的处理方式：合并（使用已有文件夹）或新建（追加 (N) 后缀）。</p>
          <div v-for="path in conflicts" :key="path" class="conflict-row">
            <span class="conflict-path">{{ path }}</span>
            <div class="row">
              <span class="chip" :class="{ active: conflictChoices[path] === 'merge' }"
                    @click="conflictChoices[path] = 'merge'">合并</span>
              <span class="chip" :class="{ active: conflictChoices[path] === 'new' }"
                    @click="conflictChoices[path] = 'new'">新建 (N)</span>
            </div>
          </div>
          <div class="row">
            <button class="btn small" @click="setAllConflicts('merge')">全部合并</button>
            <button class="btn small" @click="setAllConflicts('new')">全部新建</button>
          </div>
        </div>
        <div class="dialog-footer">
          <button class="btn" @click="cancelConflicts">取消</button>
          <button class="btn accent" @click="confirmConflicts">继续上传</button>
        </div>
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
  max-height: calc(100vh - 80px);
  display: flex;
  flex-direction: column;
  background: #1b1b1b;
}

.body {
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.dropzone {
  border: 1px dashed var(--border-strong);
  background: var(--bg-elevated);
  padding: 26px;
  text-align: center;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.dropzone.over,
.dropzone:hover {
  border-color: var(--accent);
  background: rgba(0, 120, 212, 0.06);
}

.drop-icon {
  width: 40px;
  height: 40px;
  color: var(--accent);
  opacity: 0.85;
}

.pick-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.small {
  font-size: 12px;
}

.list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* ---------------- 树状等待列表 ---------------- */

.tree-list {
  gap: 0;
  padding: 6px 0;
  border: 1px solid var(--border);
  background: var(--bg-elevated);
}

.tree-root,
.tree-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 28px;
  padding-right: 10px;
}

.tree-root {
  padding-left: 10px;
  padding-bottom: 6px;
  margin-bottom: 4px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
}

.tree-folder {
  width: 16px;
  height: 16px;
  flex: none;
  color: var(--accent);
}

.tree-name,
.tree-file-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tree-row.file .tree-file-name {
  color: var(--text-dim);
}

.tree-status {
  width: 120px;
  flex: none;
  text-align: right;
  font-size: 11px;
  color: var(--text-dim);
}

.tree-status.success {
  color: var(--ok);
}

.tree-status.error {
  color: #eb6d70;
}

.item {
  padding: 8px 10px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
}

.item-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.item-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.progress {
  height: 4px;
  margin: 8px 0 4px;
  background: var(--control);
}

.bar {
  height: 100%;
  background: var(--accent);
  transition: width 0.15s ease;
}

.bar.success {
  background: var(--ok);
}

.bar.error {
  background: var(--danger);
}

.item-status {
  font-size: 11px;
  color: var(--text-dim);
}

.item-status.success {
  color: var(--ok);
}

.item-status.error {
  color: #eb6d70;
}

.center {
  text-align: center;
}

.footer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}

/* ---------------- 结构 / 冲突 对话框 ---------------- */

.dialog-layer {
  position: fixed;
  inset: 0;
  z-index: 130;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
}

.dialog {
  width: min(560px, calc(100vw - 60px));
  max-height: calc(100vh - 100px);
  display: flex;
  flex-direction: column;
  background: #1e1e1e;
}

.structure-options {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding: 18px;
}

.structure-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 20px 14px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text);
  text-align: center;
}

.structure-card:hover {
  border-color: var(--accent);
  background: rgba(0, 120, 212, 0.08);
}

.structure-icon {
  width: 52px;
  height: 52px;
  opacity: 0.9;
  color: var(--text-dim);
}

.structure-card:hover .structure-icon {
  color: var(--accent);
}

.structure-name {
  font-size: 14px;
  font-weight: 600;
}

.dialog-body {
  padding: 14px 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.conflict-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  background: var(--bg-elevated);
}

.conflict-path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}
</style>
