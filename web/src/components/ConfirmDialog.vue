<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import { resolveConfirm, store } from '../store'

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') resolveConfirm(false)
  else if (event.key === 'Enter') resolveConfirm(true)
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="overlay" @mousedown.self="resolveConfirm(false)">
    <div class="dialog panel">
      <div class="panel-title">{{ store.confirm.title }}</div>
      <div class="content">{{ store.confirm.message }}</div>
      <div class="footer">
        <button class="btn" @click="resolveConfirm(false)">取消</button>
        <button :class="['btn', store.confirm.danger ? 'danger-solid' : 'accent']"
                @click="resolveConfirm(true)">确定</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  z-index: 140;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
}

.dialog {
  width: min(460px, calc(100vw - 60px));
  background: #1e1e1e;
}

.content {
  padding: 18px 16px;
  color: var(--text-dim);
  line-height: 1.6;
  white-space: pre-wrap;
}

.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}
</style>
