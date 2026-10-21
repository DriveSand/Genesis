<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { resolvePrompt, store } from '../store'

const value = ref(store.prompt.value)
const input = ref<HTMLInputElement | null>(null)

function submit(): void {
  const text = value.value.trim()
  resolvePrompt(text || null)
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') resolvePrompt(null)
  else if (event.key === 'Enter') submit()
}

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  await nextTick()
  input.value?.focus()
  input.value?.select()
})

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="overlay" @mousedown.self="resolvePrompt(null)">
    <div class="dialog panel">
      <div class="panel-title">{{ store.prompt.title }}</div>
      <div class="content">
        <span class="form-label">{{ store.prompt.label }}</span>
        <input ref="input" v-model="value" class="field" @keydown.enter.stop="submit" />
      </div>
      <div class="footer">
        <button class="btn" @click="resolvePrompt(null)">取消</button>
        <button class="btn accent" @click="submit">确定</button>
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
