<script setup lang="ts">
import { onMounted } from 'vue'
import { initStandalone, store } from './store'
import ImageViewer from './components/ImageViewer.vue'
import EditInfoModal from './components/EditInfoModal.vue'
import ToastHost from './components/ToastHost.vue'

const props = defineProps<{ imagePath: string }>()

onMounted(() => {
  void initStandalone(props.imagePath)
})
</script>

<template>
  <div class="standalone">
    <div v-if="!store.viewerOpen" class="loading">正在加载图片…</div>
    <ImageViewer v-else />
    <EditInfoModal v-if="store.editOpen" />
    <ToastHost />
  </div>
</template>

<style scoped>
.standalone {
  height: 100%;
  background: #0b0b0b;
}

.loading {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-dim);
}
</style>
