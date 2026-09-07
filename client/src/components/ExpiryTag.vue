<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ days: number }>();

const state = computed(() => {
  const d = props.days;
  if (d < 0) return { type: "danger" as const, text: `已过期 ${-d} 天` };
  if (d === 0) return { type: "danger" as const, text: "今天到期" };
  if (d <= 3) return { type: "danger" as const, text: `剩 ${d} 天` };
  if (d <= 7) return { type: "warning" as const, text: `剩 ${d} 天` };
  return { type: "success" as const, text: `剩 ${d} 天` };
});
</script>

<template>
  <el-tag :type="state.type" effect="light" round>{{ state.text }}</el-tag>
</template>
