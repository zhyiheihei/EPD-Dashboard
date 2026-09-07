<script setup lang="ts">
import { onMounted, ref } from "vue";
import { Minus, Close, FullScreen } from "@element-plus/icons-vue";

// 无边框窗口：拖拽区 + 自定义最小化/最大化/关闭。
// 浏览器里开发时（无 Tauri 环境）按钮退化为占位。
const isTauri = ref(false);
let win: {
  minimize: () => Promise<void>;
  toggleMaximize: () => Promise<void>;
  close: () => Promise<void>;
} | null = null;

onMounted(async () => {
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    win = getCurrentWindow();
    isTauri.value = true;
  } catch {
    isTauri.value = false;
  }
});

function safe(fn?: () => Promise<void>) {
  fn?.().catch(() => undefined);
}
</script>

<template>
  <header class="titlebar" data-tauri-drag-region>
    <div class="tb-title" data-tauri-drag-region>
      <img src="/app-icon.png" alt="" style="width: 20px; height: 20px" />
      <span data-tauri-drag-region>食品管家</span>
      <span class="muted" style="font-size: 12px">家庭食品存储看板</span>
    </div>
    <div class="win-controls" v-if="isTauri">
      <div class="win-btn" title="最小化" @click="safe(() => win!.minimize())">
        <el-icon><Minus /></el-icon>
      </div>
      <div class="win-btn" title="最大化/还原" @click="safe(() => win!.toggleMaximize())">
        <el-icon><FullScreen /></el-icon>
      </div>
      <div class="win-btn close" title="关闭" @click="safe(() => win!.close())">
        <el-icon><Close /></el-icon>
      </div>
    </div>
  </header>
</template>
