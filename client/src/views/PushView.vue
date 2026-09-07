<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { Promotion, Refresh } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "../api/client";
import type { PreviewItem, PushStatus } from "../api/types";
import EpdPreview from "../components/EpdPreview.vue";
import { formatDateTime } from "../lib/format";

const status = ref<PushStatus | null>(null);
const preview = ref<PreviewItem[]>([]);
const pushing = ref(false);
let timer: number | undefined;

async function refresh() {
  const [s, p] = await Promise.all([api.pushStatus(), api.preview().catch(() => [])]);
  status.value = s;
  preview.value = p;
  schedule();
}

function schedule() {
  window.clearTimeout(timer);
  if (status.value?.in_progress) {
    timer = window.setTimeout(refresh, 3000); // 推送中轮询
  } else {
    timer = window.setTimeout(refresh, 30000); // 常规低频刷新
  }
}

async function push() {
  pushing.value = true;
  try {
    const result = await api.push();
    if (result.busy) {
      ElMessage.warning("已有推送在进行中，请稍候");
    } else {
      ElMessage.success("推送已开始");
    }
  } finally {
    pushing.value = false;
    await refresh();
  }
}

onMounted(refresh);
onBeforeUnmount(() => window.clearTimeout(timer));
</script>

<template>
  <el-row :gutter="14">
    <el-col :span="14">
      <el-card shadow="never">
        <template #header>
          <div style="display: flex; align-items: center; gap: 10px">
            <span>推送状态</span>
            <el-tag v-if="status?.in_progress" type="warning" effect="light">推送中…</el-tag>
            <el-tag v-else-if="status?.ok === true" type="success" effect="light">上次成功</el-tag>
            <el-tag v-else-if="status?.ok === false" type="danger" effect="light">上次失败</el-tag>
            <el-tag v-else type="info" effect="light">尚未推送</el-tag>
          </div>
        </template>

        <div class="toolbar">
          <el-button
            type="primary"
            size="large"
            :icon="Promotion"
            :loading="pushing || !!status?.in_progress"
            @click="push"
          >
            {{ status?.in_progress ? "推送中…" : "立即推送" }}
          </el-button>
          <el-button :icon="Refresh" @click="refresh">刷新状态</el-button>
        </div>

        <el-descriptions :column="1" border>
          <el-descriptions-item label="设备">
            {{ status?.device ?? "—" }}
          </el-descriptions-item>
          <el-descriptions-item label="开始时间">
            {{ formatDateTime(status?.started_at ?? null) }}
          </el-descriptions-item>
          <el-descriptions-item label="耗时">
            {{ status?.duration_s != null ? `${status.duration_s} 秒` : "—" }}
          </el-descriptions-item>
          <el-descriptions-item label="上屏条目">
            <template v-if="status?.items?.length">
              <el-tag
                v-for="name in status.items"
                :key="name"
                size="small"
                style="margin-right: 6px"
                >{{ name }}</el-tag
              >
            </template>
            <span v-else class="muted">（空）</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="status?.error" label="错误信息">
            <span style="color: var(--el-color-danger)">{{ status.error }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <div class="muted" style="margin-top: 10px">
          每天 0 点服务端自动推送；增删食品后 10 秒内自动推送（可在服务端配置关闭）。
          三色屏全刷约需 15 秒，请耐心等待墨水屏完成刷新。
        </div>
      </el-card>
    </el-col>
    <el-col :span="10">
      <EpdPreview :items="preview" />
    </el-col>
  </el-row>
</template>
