<script setup lang="ts">
import { store } from '../store'
</script>

<template>
  <div class="toasts">
    <transition-group name="toast">
      <div v-for="item in store.toasts" :key="item.id" class="toast" :class="item.kind">
        {{ item.text }}
      </div>
    </transition-group>
  </div>
</template>

<style scoped>
.toasts {
  position: fixed;
  /* 尺寸/边距随分辨率缩放: 1080p 下约 15px 字号, 4K 下封顶 */
  right: clamp(18px, 1.8vw, 48px);
  bottom: clamp(18px, 2.2vh, 48px);
  z-index: 200;
  display: flex;
  flex-direction: column;
  gap: clamp(6px, 0.7vh, 12px);
  max-width: min(560px, calc(100vw - 32px));
}

.toast {
  padding: clamp(11px, 1vh, 18px) clamp(16px, 1.1vw, 26px);
  background: rgba(32, 32, 32, 0.97);
  border: 1px solid var(--border-strong);
  border-left: clamp(4px, 0.28vw, 7px) solid var(--accent);
  color: var(--text);
  font-size: clamp(13.5px, calc(0.55vw + 0.45vh), 19px);
  line-height: 1.45;
  font-weight: 500;
  min-width: clamp(240px, 15vw, 420px);
  box-shadow: 0 12px 36px rgba(0, 0, 0, 0.68);
  word-break: break-word;
}

.toast.success {
  border-left-color: var(--ok);
}

.toast.error {
  border-left-color: var(--danger);
}

.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateX(20px);
}
</style>
