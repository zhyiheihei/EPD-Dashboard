<script setup lang="ts">
import ExpiryTag from "./ExpiryTag.vue";
import { DEVICE_TYPE_LABEL } from "../lib/format";
import type { PreviewItem } from "../api/types";

defineProps<{ items: PreviewItem[]; loading?: boolean }>();
</script>

<template>
  <div class="epd-preview" v-loading="loading">
    <h4>墨水屏 · 食品面板预览</h4>
    <div v-if="items.length === 0" class="epd-empty">（空 — 屏幕食品面板将显示为空白）</div>
    <div v-for="item in items" :key="item.slot" class="epd-row">
      <span class="epd-type">{{ DEVICE_TYPE_LABEL[item.device_type] ?? "食品" }}</span>
      <span class="epd-name">{{ item.name }}</span>
      <ExpiryTag :days="item.days_remaining" />
    </div>
  </div>
</template>
