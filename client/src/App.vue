<script setup lang="ts">
import { useRoute } from "vue-router";
import { Apple, Odometer, Promotion, Setting, Upload } from "@element-plus/icons-vue";
import TitleBar from "./components/TitleBar.vue";

const route = useRoute();
</script>

<template>
  <div class="app-shell">
    <TitleBar />
    <el-menu
      mode="horizontal"
      router
      :default-active="route.path"
      class="app-nav"
      :ellipsis="false"
    >
      <el-menu-item index="/">
        <el-icon><Odometer /></el-icon>仪表盘
      </el-menu-item>
      <el-menu-item index="/foods">
        <el-icon><Apple /></el-icon>食品管理
      </el-menu-item>
      <el-menu-item index="/push">
        <el-icon><Promotion /></el-icon>推送墨水屏
      </el-menu-item>
      <el-menu-item index="/ota">
        <el-icon><Upload /></el-icon>固件升级
      </el-menu-item>
      <el-menu-item index="/settings">
        <el-icon><Setting /></el-icon>设置
      </el-menu-item>
    </el-menu>
    <main class="app-content">
      <router-view />
    </main>
  </div>
</template>
