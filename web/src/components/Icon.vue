<script setup lang="ts">
import { computed } from 'vue'

/**
 * 内联 SVG 图标。
 *
 * SVG 内的 fill/stroke 统一使用 currentColor, 因此颜色由 CSS 的 color
 * 属性动态控制 (例如 .app-icon { color: var(--accent) } 或父元素继承),
 * 缩放/悬停变色不再需要替换图片文件。
 */
const props = defineProps<{
  name: string
  /** 可访问名称; 省略时视为装饰性图标 */
  label?: string
}>()

const icons = import.meta.glob('../../public/icons/*.svg', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const markup = computed(
  () => icons[`../../public/icons/${props.name}.svg`] ?? '',
)
</script>

<template>
  <span class="app-icon" :class="`app-icon-${name}`"
        :role="label ? 'img' : undefined"
        :aria-label="label"
        :aria-hidden="label ? undefined : 'true'"
        v-html="markup"></span>
</template>
