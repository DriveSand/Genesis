<script setup lang="ts">
import Icon from './Icon.vue'
import { computed, ref, watch } from 'vue'
import { api, formatBytes, formatTime } from '../api'
import {
  deleteTargets,
  downloadTargets,
  imagePathOf,
  isBlurred,
  loadMore,
  navigate,
  openBackgroundContext,
  openContext,
  openEditor,
  openInNewTab,
  openMoveDialog,
  openViewer,
  store,
  visibleFiles,
  visibleFolders,
  type ContextTarget,
} from '../store'
import { RATING_COLORS, RATING_NAMES } from '../types'
import type { SearchFile } from '../types'

const scrollEl = ref<HTMLElement | null>(null)
const revealed = ref(new Set<string>())
const failedThumbs = ref(new Set<string>())
const selected = ref(new Set<string>())

const cell = computed(() => `${store.settings.GridCellSize}px`)

function fileKey(file: SearchFile): string {
  return `file\u0000${file.path}\u0000${file.name}`
}

function folderKey(folder: { path: string; name: string }): string {
  return `folder\u0000${folder.path}\u0000${folder.name}`
}

function thumbSrc(file: SearchFile): string {
  if (failedThumbs.value.has(file.sha256)) {
    return api.originalUrl(imagePathOf(file))
  }
  const base = store.settings.EnableThumbnail
    ? api.thumbnailUrl(file.sha256)
    : api.originalUrl(imagePathOf(file))
  return `${base}${base.includes('?') ? '&' : '?'}v=${store.viewerBust}`
}

function onThumbError(file: SearchFile): void {
  if (!failedThumbs.value.has(file.sha256)) {
    failedThumbs.value.add(file.sha256)
    failedThumbs.value = new Set(failedThumbs.value)
  }
}

function isRevealed(file: SearchFile): boolean {
  return revealed.value.has(fileKey(file))
}

function reveal(file: SearchFile): void {
  revealed.value.add(fileKey(file))
  revealed.value = new Set(revealed.value)
}

function conceal(file: SearchFile): void {
  // 已显示的图片: 点击分级角标重新设为模糊状态
  revealed.value.delete(fileKey(file))
  revealed.value = new Set(revealed.value)
}

function locked(file: SearchFile): boolean {
  return isBlurred(file.rating) && !isRevealed(file)
}

function showRatingBadge(file: SearchFile): boolean {
  // 分级低于"开始模糊的等级"时不显示角标
  return file.rating >= store.settings.BlurLevel
}

function fileTarget(file: SearchFile): ContextTarget {
  return { kind: 'file', name: file.name, path: file.path, sha256: file.sha256 }
}

function folderTarget(folder: { name: string; path: string }): ContextTarget {
  return { kind: 'folder', name: folder.name, path: folder.path }
}

function onFileClick(event: MouseEvent, file: SearchFile): void {
  if (event.ctrlKey || event.metaKey) {
    toggleSelection(fileKey(file))
    return
  }
  if (locked(file)) return
  void openViewer(file)
}

function onFolderClick(event: MouseEvent, folder: { name: string; path: string }): void {
  if (event.ctrlKey || event.metaKey) {
    toggleSelection(folderKey(folder))
    return
  }
  const base = folder.path === '/' ? '' : folder.path
  void navigate(`${base}/${folder.name}`)
}

function toggleSelection(key: string): void {
  if (selected.value.has(key)) selected.value.delete(key)
  else selected.value.add(key)
  selected.value = new Set(selected.value)
}

function clearSelection(): void {
  selected.value = new Set()
}

watch(
  () => [store.path, store.inSearch] as const,
  () => {
    clearSelection()
    revealed.value = new Set()
    scrollEl.value?.scrollTo({ top: 0 })
  },
)

function isSelectedFile(file: SearchFile): boolean {
  return selected.value.has(fileKey(file))
}

function isSelectedFolder(folder: { path: string; name: string }): boolean {
  return selected.value.has(folderKey(folder))
}

const selectedFiles = computed<ContextTarget[]>(() =>
  visibleFiles.value
    .filter((f) => selected.value.has(fileKey(f)))
    .map(fileTarget),
)

const selectedFolders = computed<ContextTarget[]>(() =>
  visibleFolders.value
    .filter((f) => selected.value.has(folderKey(f)))
    .map(folderTarget),
)

const selectionCount = computed(
  () => selectedFiles.value.length + selectedFolders.value.length,
)

function onScroll(event: Event): void {
  const el = event.target as HTMLElement
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 500) {
    void loadMore()
  }
}

function onSelectionDownload(): void {
  void downloadTargets(selectedFiles.value, selectedFolders.value)
  clearSelection()
}

function onSelectionMove(): void {
  openMoveDialog([...selectedFiles.value, ...selectedFolders.value])
  clearSelection()
}

async function onSelectionDelete(): Promise<void> {
  await deleteTargets([...selectedFiles.value, ...selectedFolders.value])
  clearSelection()
}

function ratingBadge(rating: number): string {
  return RATING_NAMES[rating] ?? String(rating)
}
</script>

<template>
  <section ref="scrollEl" class="stage" :style="{ '--cell': cell }" @scroll="onScroll"
           @contextmenu.prevent="openBackgroundContext">
    <div class="view-bar">
      <div class="row">
        <span v-if="store.inSearch" class="muted results-tip">
          共找到 {{ visibleFiles.length }} 项{{ store.searchHasMore ? '（可继续加载）' : '' }}
        </span>
      </div>
      <transition name="fade">
        <div v-if="selectionCount" class="selection-bar">
          <span class="muted">已选 {{ selectionCount }} 项</span>
          <button class="btn small" @click="onSelectionDownload">下载</button>
          <button class="btn small" @click="onSelectionMove">移动</button>
          <button class="btn small danger" @click="onSelectionDelete">删除</button>
          <button class="btn ghost small" @click="clearSelection">取消</button>
        </div>
      </transition>
    </div>

    <div v-if="store.loading && !store.folders.length && !visibleFiles.length"
         class="empty-tip">正在加载…</div>

    <template v-else>
      <!-- 文件夹与图片在同一网格中顺次排列, 不换行分组 -->
      <div v-if="visibleFolders.length || visibleFiles.length" class="grid">
        <div v-for="folder in visibleFolders" :key="`${folder.path}/${folder.name}`"
             class="cell folder"
             :class="{ selected: isSelectedFolder(folder) }"
             @click="onFolderClick($event, folder)"
             @contextmenu.stop="openContext($event, folderTarget(folder))">
          <div class="folder-tile">
            <Icon name="folder" class="folder-glyph" />
            <span class="folder-count">文件夹</span>
          </div>
          <div class="name" :title="folder.name">{{ folder.name }}</div>
          <div class="sub">
            <span>{{ folder.scanned_path ? '实时' : '离线' }}</span>
            <span>{{ formatTime(folder.edit_time) }}</span>
          </div>
        </div>

        <div v-for="file in visibleFiles" :key="fileKey(file)" class="cell"
             :class="{ selected: isSelectedFile(file) }">
          <div class="thumb" @click="onFileClick($event, file)"
               @contextmenu.stop="openContext($event, fileTarget(file))">
            <template v-if="locked(file)">
              <img class="thumb-img blurred" :src="thumbSrc(file)" loading="lazy"
                   :alt="file.name" @error="onThumbError(file)" />
              <div class="locked">
                <Icon name="protection" class="lock-icon" label="locked" />
                <span class="badge lock-badge"
                      :style="{ background: RATING_COLORS[file.rating] }">{{ ratingBadge(file.rating) }}</span>
                <span class="lock-text">已模糊处理该图像</span>
                <button class="btn small" @click.stop="reveal(file)">显示</button>
              </div>
            </template>
            <template v-else>
              <img class="thumb-img" :src="thumbSrc(file)" loading="lazy"
                   :alt="file.name" @error="onThumbError(file)" />
              <span v-if="showRatingBadge(file)" class="badge rating-badge clickable"
                    title="点击重新模糊" @click.stop="conceal(file)"
                    :style="{ background: RATING_COLORS[file.rating] }">{{ ratingBadge(file.rating) }}</span>
            </template>

            <div class="hover-actions" @click.stop>
              <button class="icon-btn small" title="编辑信息" @click="openEditor(file)">
                <Icon name="edit" label="edit" />
              </button>
              <button class="icon-btn small" title="在新窗口查看" @click="openInNewTab(file)">
                <Icon name="newtab-open" label="open" />
              </button>
            </div>
          </div>
          <div class="name" :title="file.name">{{ file.name }}</div>
          <div class="sub">
            <span>{{ formatBytes(file.file_size) }}</span>
            <span>{{ formatTime(file.add_time) }}</span>
          </div>
        </div>
      </div>

      <div v-if="!visibleFiles.length && !visibleFolders.length && !store.loading"
           class="empty-tip">
        {{ store.inSearch ? '没有匹配的图片' : '此目录为空，拖入图片或点击上传' }}
      </div>

      <div v-if="store.loadingMore" class="load-more muted">加载更多…</div>
    </template>
  </section>
</template>

<style scoped>
.stage {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  background:
    radial-gradient(circle at 50% 0%, rgba(255, 255, 255, 0.028), transparent 420px),
    var(--bg-stage);
  padding-bottom: 60px;
}

.view-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 40px;
  padding: 8px 26px 0;
}

.results-tip {
  font-size: 12px;
}

.selection-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  background: var(--panel);
  border: 1px solid var(--border);
  font-size: 12px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, var(--cell));
  gap: 16px 18px;
  padding: 14px 26px 0;
  justify-content: start;
}

.cell {
  width: var(--cell);
  user-select: none;
  position: relative;
}

.cell.selected .thumb,
.cell.selected .folder-tile {
  outline: 1px solid var(--accent);
  outline-offset: 0;
}

.thumb {
  position: relative;
  width: var(--cell);
  height: var(--cell);
  background: var(--panel);
  border: 1px solid var(--border);
  overflow: hidden;
  cursor: pointer;
  transition: border-color 0.12s ease;
}

.thumb:hover {
  border-color: var(--border-strong);
}

.thumb-img {
  width: 100%;
  height: 100%;
  /* 预览图自适应网格, 完整显示在窗格内 (不裁切) */
  object-fit: contain;
  object-position: center center;
  display: block;
  background: #1b1b1b;
}

.rating-badge {
  position: absolute;
  left: 6px;
  top: 6px;
  height: 20px;
  padding: 0 7px;
  font-size: 12px;
  line-height: 20px;
  opacity: 0.92;
}

.rating-badge.clickable {
  cursor: pointer;
}

.rating-badge.clickable:hover {
  opacity: 1;
}

.hover-actions {
  position: absolute;
  top: 4px;
  right: 4px;
  display: flex;
  gap: 2px;
  opacity: 0;
  transition: opacity 0.12s ease;
}

.hover-actions .icon-btn {
  width: 28px;
  height: 28px;
  background: rgba(20, 20, 20, 0.78);
  border-color: rgba(255, 255, 255, 0.12);
}

.thumb:hover .hover-actions {
  opacity: 1;
}

.thumb-img.blurred {
  filter: blur(26px) saturate(0.85);
  transform: scale(1.15);
}

.locked {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: rgba(16, 16, 16, 0.55);
}

.lock-icon {
  width: 44px;
  height: 44px;
  opacity: 0.65;
  color: var(--text-dim);
}

.lock-badge {
  height: 24px;
  padding: 0 10px;
  font-size: 14px;
  line-height: 24px;
}

.lock-text {
  color: var(--text-dim);
  font-size: 12px;
}

.name {
  margin-top: 7px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: #e3e3e3;
  font-size: 13px;
}

.sub {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-top: 2px;
  color: var(--text-faint);
  font-size: 11px;
  white-space: nowrap;
}

.folder-tile {
  position: relative;
  width: var(--cell);
  height: var(--cell);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--panel);
  border: 1px solid var(--border);
  cursor: pointer;
}

.folder-tile:hover {
  border-color: var(--border-strong);
  background: #262626;
}

.folder-glyph {
  width: 62%;
  height: 62%;
  color: #7f7f7f;
}

.folder-tile:hover .folder-glyph {
  color: #9b9b9b;
}

.folder-count {
  position: absolute;
  bottom: 10px;
  left: 0;
  right: 0;
  text-align: center;
  color: var(--text-faint);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0 10px;
}

.load-more {
  padding: 26px;
  text-align: center;
  font-size: 12px;
}
</style>
