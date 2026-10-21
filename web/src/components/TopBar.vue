<script setup lang="ts">
import Icon from './Icon.vue'
import { ref } from 'vue'
import {
  exitSearch,
  goUp,
  navigate,
  refresh,
  runBasicSearch,
  store,
} from '../store'

const query = ref('')

function submitSearch(): void {
  void runBasicSearch(query.value)
}

function toggleAdvanced(): void {
  store.searchPanelOpen = !store.searchPanelOpen
  if (store.searchPanelOpen) store.quickOpen = false
}

function openUpload(): void {
  store.uploadOpen = true
}

function toggleQuick(): void {
  store.quickOpen = !store.quickOpen
  if (store.quickOpen) store.searchPanelOpen = false
}

function clearSearch(): void {
  query.value = ''
  exitSearch()
}
</script>

<template>
  <header class="topbar">
    <div class="main-row">
      <div class="brand">Genesis</div>

      <div class="search-wrap">
        <input v-model="query" class="field search-input" placeholder="搜索图片"
               @keydown.enter="submitSearch" />
        <button class="icon-btn search-btn" title="搜索" @click="submitSearch">
          <Icon name="search" label="search" />
        </button>
        <button class="icon-btn" data-panel="search"
                :class="{ toggled: store.searchPanelOpen }"
                title="高级搜索" @click="toggleAdvanced">
          <Icon name="doc-search" label="advanced" />
        </button>
      </div>

      <div class="actions">
        <span v-if="store.readOnly" class="readonly-tag">只读模式</span>
        <button class="icon-btn" title="上传" @click="openUpload">
          <Icon name="upload" label="upload" />
        </button>
        <button class="icon-btn" data-panel="quick"
                :class="{ toggled: store.quickOpen }"
                title="快速调整" @click="toggleQuick">
          <Icon name="preview-open" label="quick" />
        </button>
        <button class="icon-btn" title="全局设置" @click="store.settingsOpen = true">
          <Icon name="setting" label="settings" />
        </button>
      </div>
    </div>

    <div class="nav-row">
      <button class="btn ghost small" :disabled="store.path === '/' && !store.inSearch"
              @click="goUp">返回上级</button>
      <button class="btn ghost small" @click="refresh">刷新</button>
      <nav class="crumbs">
        <template v-if="store.inSearch">
          <span class="crumb active">搜索结果 · {{ store.searchSummary }}</span>
          <button class="btn ghost small" @click="clearSearch">退出搜索</button>
        </template>
        <template v-else>
          <template v-for="(crumb, index) in store.breadcrumbs" :key="crumb.path">
            <span v-if="index" class="sep">/</span>
            <span class="crumb"
                  :class="{ active: index === store.breadcrumbs.length - 1 }"
                  @click="navigate(crumb.path)">{{ crumb.name }}</span>
          </template>
        </template>
      </nav>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  flex: none;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border);
  user-select: none;
}

.main-row {
  display: grid;
  grid-template-columns: minmax(200px, 1fr) minmax(320px, 640px) minmax(220px, 1fr);
  align-items: center;
  gap: 20px;
  height: 58px;
  padding: 0 26px;
}

.brand {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: #f2f2f2;
  white-space: nowrap;
}

.search-wrap {
  display: flex;
  align-items: center;
  gap: 4px;
  background: var(--control);
  border: 1px solid var(--border);
  padding-right: 4px;
}

.search-wrap:focus-within {
  border-color: var(--accent);
}

.search-input {
  height: 34px;
  background: transparent;
  border: none;
}

.search-input:focus {
  border: none;
}

.icon-btn.toggled {
  background: var(--accent-soft);
  border-color: var(--accent);
}

.actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
}

.readonly-tag {
  margin-right: 6px;
  padding: 2px 8px;
  font-size: 11px;
  color: var(--warn);
  border: 1px solid #5c4a1f;
  background: rgba(240, 185, 77, 0.1);
}

.nav-row {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 40px;
  padding: 0 26px;
  border-top: 1px solid var(--border);
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: 10px;
  overflow: hidden;
  white-space: nowrap;
}

.crumb {
  color: var(--text-dim);
  cursor: pointer;
  padding: 2px 4px;
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
</style>
