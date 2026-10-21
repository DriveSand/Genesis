<script setup lang="ts">
import { api } from '../api'
import {
  closeContext,
  copyTextToClipboard,
  createFolderHere,
  deleteTargets,
  downloadSingle,
  downloadTargets,
  imagePathOf,
  navigate,
  openEditor,
  openInNewTab,
  openMoveDialog,
  regenerateThumbnail,
  renameTarget,
  reparseMetadata,
  store,
  toastError,
  visibleFiles,
} from '../store'
import type { SearchFile } from '../types'

const target = () => store.context.target

function asSearchFile(): SearchFile | null {
  const t = target()
  if (!t) return null
  return visibleFiles.value.find((f) => f.path === t.path && f.name === t.name) ?? null
}

function view(): void {
  const file = asSearchFile()
  closeContext()
  if (file) openInNewTab(file)
}

function edit(): void {
  // 直接打开编辑界面, 不经过图片查看界面
  const file = asSearchFile()
  closeContext()
  if (file) openEditor(file)
}

function reparse(): void {
  const t = target()
  closeContext()
  if (t) void reparseMetadata(t)
}

async function copyPrompt(): Promise<void> {
  const t = target()
  closeContext()
  if (!t) return
  try {
    const meta = await api.metadata(imagePathOf(t))
    await copyTextToClipboard(meta.positive_prompt, '正向提示')
  } catch (err) {
    toastError(err, '复制提示词失败')
  }
}

function download(): void {
  const t = target()
  closeContext()
  if (!t) return
  if (t.kind === 'folder') void downloadTargets([], [t])
  else void downloadSingle(t)
}

function regenerate(): void {
  const t = target()
  closeContext()
  if (t?.sha256) void regenerateThumbnail({ sha256: t.sha256 })
}

function remove(): void {
  const t = target()
  closeContext()
  if (t) void deleteTargets([t])
}

function rename(): void {
  const t = target()
  closeContext()
  if (t) void renameTarget(t)
}

function move(): void {
  const t = target()
  closeContext()
  if (t) openMoveDialog([t])
}

function openFolder(): void {
  const t = target()
  closeContext()
  if (!t) return
  const base = t.path === '/' ? '' : t.path
  void navigate(`${base}/${t.name}`)
}

function createFolderHereAction(): void {
  closeContext()
  void createFolderHere()
}

function uploadHere(): void {
  closeContext()
  store.uploadOpen = true
}
</script>

<template>
  <div class="menu panel"
       :style="{ left: `${store.context.x}px`, top: `${store.context.y}px` }"
       @click.stop>
    <template v-if="!store.context.target">
      <button class="item" @click="createFolderHereAction">新建文件夹</button>
      <button class="item" @click="uploadHere">上传文件到此处</button>
    </template>
    <template v-else-if="store.context.target.kind === 'file'">
      <button class="item" @click="view">在新窗口查看</button>
      <button class="item" @click="edit">编辑信息</button>
      <button class="item" @click="copyPrompt">复制提示词</button>
      <button class="item" @click="reparse">重新读取元数据</button>
      <button class="item" @click="rename">重命名</button>
      <button class="item" @click="download">下载此图像</button>
      <button class="item" @click="regenerate">重新生成此图像缩略图</button>
      <button class="item" @click="move">移动到…</button>
      <div class="sep"></div>
      <button class="item danger" @click="remove">删除此图像</button>
    </template>
    <template v-else>
      <button class="item" @click="openFolder">打开</button>
      <button class="item" @click="rename">重命名</button>
      <button class="item" @click="move">移动到…</button>
      <button class="item" @click="download">下载此文件夹</button>
      <div class="sep"></div>
      <button class="item danger" @click="remove">删除此文件夹</button>
    </template>
  </div>
</template>

<style scoped>
.menu {
  position: fixed;
  z-index: 110;
  min-width: 210px;
  padding: 4px 0;
  background: #202020;
}

.item {
  display: block;
  width: 100%;
  padding: 7px 14px;
  background: transparent;
  border: none;
  text-align: left;
  color: var(--text);
  font-size: 12.5px;
}

.item:hover {
  background: var(--control-hover);
}

.item.danger {
  color: #eb6d70;
}

.item.danger:hover {
  background: var(--danger-soft);
}

.sep {
  height: 1px;
  margin: 4px 0;
  background: var(--border);
}
</style>
