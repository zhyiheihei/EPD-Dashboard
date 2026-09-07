<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { Refresh, Promotion } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "../api/client";
import type { PreviewItem, Stats } from "../api/types";
import EpdPreview from "../components/EpdPreview.vue";
import { settings } from "../lib/settings";

const router = useRouter();
const stats = ref<Stats | null>(null);
const preview = ref<PreviewItem[]>([]);
const loading = ref(false);
const pushing = ref(false);

async function loadAll() {
  loading.value = true;
  try {
    const [s, p] = await Promise.all([api.stats(), api.preview()]);
    stats.value = s;
    preview.value = p;
  } finally {
    loading.value = false;
  }
}

async function quickPush() {
  pushing.value = true;
  try {
    const result = await api.push();
    if (result.busy) {
      ElMessage.warning("已有推送在进行中");
    } else {
      ElMessage.success("已开始推送，可在「推送墨水屏」页查看进度");
      await router.push("/push");
    }
  } finally {
    pushing.value = false;
  }
}

onMounted(loadAll);
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <el-button :icon="Refresh" @click="loadAll">刷新</el-button>
      <el-button type="primary" :icon="Promotion" :loading="pushing" @click="quickPush">
        立即推送墨水屏
      </el-button>
      <span class="muted" style="margin-left: auto">服务器：{{ settings.serverUrl }}</span>
    </div>

    <el-row :gutter="14" style="margin-bottom: 16px">
      <el-col :span="6">
        <el-card class="stat-card" shadow="never">
          <div class="stat-value">{{ stats?.total_active ?? "—" }}</div>
          <div class="stat-label">在库食品</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card" shadow="never">
          <div class="stat-value" style="color: var(--el-color-danger)">
            {{ stats?.expiring_3d ?? "—" }}
          </div>
          <div class="stat-label">3 天内到期</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card" shadow="never">
          <div class="stat-value" style="color: var(--el-color-warning)">
            {{ stats?.expiring_7d ?? "—" }}
          </div>
          <div class="stat-label">7 天内到期</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card" shadow="never">
          <div class="stat-value">{{ stats?.expired ?? "—" }}</div>
          <div class="stat-label">已过期</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14">
      <el-col :span="14">
        <EpdPreview :items="preview" />
        <div class="muted" style="margin-top: 8px">
          上屏规则：未消耗食品按到期时间升序取前 4 条；每天 0 点由服务端自动刷新。
        </div>
      </el-col>
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>品类分布</template>
          <div v-if="!stats?.by_category?.length" class="muted">暂无在库食品</div>
          <div
            v-for="row in stats?.by_category ?? []"
            :key="row.category"
            style="display: flex; justify-content: space-between; padding: 6px 2px"
          >
            <span>{{ row.category }}</span>
            <el-tag size="small" round>{{ row.count }}</el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
