<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import { closeContext, init, store } from './store'
import TopBar from './components/TopBar.vue'
import MainView from './components/MainView.vue'
import QuickPanel from './components/QuickPanel.vue'
import SearchPanel from './components/SearchPanel.vue'
import ImageViewer from './components/ImageViewer.vue'
import EditInfoModal from './components/EditInfoModal.vue'
import SettingsModal from './components/SettingsModal.vue'
import UploadPanel from './components/UploadPanel.vue'
import MoveDialog from './components/MoveDialog.vue'
import ContextMenu from './components/ContextMenu.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import PromptDialog from './components/PromptDialog.vue'
import ToastHost from './components/ToastHost.vue'

function onWindowClick(event: MouseEvent): void {
  closeContext()
  const target = event.target as HTMLElement | null
  // 点击弹窗与触发按钮之外的空白区域时, 关闭快速调整与高级搜索
  if (!target?.closest('.quick') && !target?.closest('[data-panel="quick"]')) {
    store.quickOpen = false
  }
  if (!target?.closest('.search-panel') && !target?.closest('[data-panel="search"]')) {
    store.searchPanelOpen = false
  }
}

onMounted(() => {
  window.addEventListener('click', onWindowClick)
  void init()
})

onBeforeUnmount(() => {
  window.removeEventListener('click', onWindowClick)
})
</script>

<template>
  <div class="app">
    <TopBar />
    <MainView />
    <QuickPanel v-if="store.quickOpen" />
    <SearchPanel v-show="store.searchPanelOpen" />
    <ImageViewer v-if="store.viewerOpen" />
    <EditInfoModal v-if="store.editOpen" />
    <SettingsModal v-if="store.settingsOpen" />
    <UploadPanel v-if="store.uploadOpen" />
    <MoveDialog v-if="store.moveOpen" />
    <ContextMenu v-if="store.context.open" />
    <ConfirmDialog v-if="store.confirm.open" />
    <PromptDialog v-if="store.prompt.open" />
    <ToastHost />
  </div>
</template>

<style scoped>
.app {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--bg);
}
</style>
