<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import { resolveChoice, store } from '../store'

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') resolveChoice(null)
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="overlay" @mousedown.self="resolveChoice(null)">
    <div class="dialog panel">
      <div class="panel-title">{{ store.choice.title }}</div>
      <div class="content">{{ store.choice.message }}</div>
      <div class="footer">
        <button class="btn" @click="resolveChoice(null)">取消</button>
        <button v-for="option in store.choice.options" :key="option.value"
                class="btn accent" @click="resolveChoice(option.value)">
          {{ option.label }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  z-index: 145;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
}

.dialog {
  width: min(500px, calc(100vw - 60px));
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
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}
</style>
