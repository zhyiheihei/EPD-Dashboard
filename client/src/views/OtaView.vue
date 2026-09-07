<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { Promotion, Refresh, UploadFilled } from "@element-plus/icons-vue";
import { ElMessage, ElMessageBox, genFileId } from "element-plus";
import type { UploadFile, UploadRawFile } from "element-plus";
import { api } from "../api/client";
import type { DeviceVersion, OtaPackage, OtaStatus } from "../api/types";
import { formatDateTime } from "../lib/format";

const packages = ref<OtaPackage[]>([]);
const device = ref<DeviceVersion | null>(null);
const status = ref<OtaStatus | null>(null);
const deviceLoading = ref(false);
const uploading = ref(false);
const pushing = ref(false);
let timer: number | undefined;

const inProgress = computed(() => !!status.value?.in_progress);
const percent = computed(() => {
  const s = status.value;
  if (!s?.total_bytes) return 0;
  return Math.min(100, Math.round(((s.sent_bytes || 0) / s.total_bytes) * 100));
});

async function loadPackages() {
  packages.value = await api.otaList();
}

async function refreshDevice() {
  deviceLoading.value = true;
  try {
    device.value = await api.otaDeviceVersion();
  } catch {
    device.value = null; // 错误已由 api 层提示（设备不在附近等）
  } finally {
    deviceLoading.value = false;
  }
}

async function refreshStatus() {
  status.value = await api.otaStatus();
  schedule();
}

function schedule() {
  window.clearTimeout(timer);
  timer = window.setTimeout(refreshStatus, inProgress.value ? 2000 : 15000);
}

async function onChange(file: UploadFile) {
  uploading.value = true;
  try {
    const meta = await api.otaUpload(file.name, file.raw as Blob);
    ElMessage.success(`已上传固件 v0x${(meta.app_version ?? 0).toString(16).toUpperCase()}`);
    await loadPackages();
  } catch {
    /* 错误已提示 */
  } finally {
    uploading.value = false;
  }
}

function handleExceed(files: File[]) {
  const file = files[0] as UploadRawFile;
  file.uid = genFileId();
}

async function upgrade(pkg?: OtaPackage) {
  const target = pkg?.filename ?? packages.value[0]?.filename;
  if (!pkg && !target) {
    ElMessage.warning("请先上传 OTA 包");
    return;
  }
  await ElMessageBox.confirm(
    `确定将「${target}」推送到墨水屏升级？升级期间设备将进入 bootloader，请勿断电。`,
    "固件升级",
    { type: "warning", confirmButtonText: "开始升级" },
  );
  pushing.value = true;
  try {
    const result = await api.otaPush(pkg?.id);
    if (result.busy) {
      ElMessage.warning("已有升级在进行中");
    } else {
      ElMessage.success("升级已开始");
      await refreshStatus();
    }
  } finally {
    pushing.value = false;
  }
}

async function removePackage(pkg: OtaPackage) {
  await ElMessageBox.confirm(`删除固件包「${pkg.filename}」？`, "删除", { type: "warning" });
  await api.otaDelete(pkg.id);
  await loadPackages();
}

function fmtSize(n: number): string {
  return n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${(n / 1024).toFixed(1)} KB`;
}

function fmtVersion(v: number | null): string {
  return v == null ? "—" : `0x${v.toString(16).toUpperCase().padStart(2, "0")}`;
}

onMounted(async () => {
  await Promise.all([loadPackages(), refreshStatus(), refreshDevice()]);
});
onBeforeUnmount(() => window.clearTimeout(timer));
</script>

<template>
  <el-row :gutter="14">
    <el-col :span="14">
      <el-card shadow="never">
        <template #header>
          <div style="display: flex; align-items: center; gap: 10px">
            <span>设备固件</span>
            <el-tag v-if="device" type="success" effect="light">{{ device.version_hex }}</el-tag>
            <el-tag v-else type="info" effect="light">未知（设备不在范围内？）</el-tag>
            <el-button
              :icon="Refresh"
              circle
              size="small"
              :loading="deviceLoading"
              style="margin-left: auto"
              @click="refreshDevice"
            />
          </div>
        </template>

        <div class="toolbar">
          <el-button
            type="primary"
            :icon="Promotion"
            :loading="pushing || inProgress"
            :disabled="packages.length === 0"
            @click="upgrade()"
          >
            升级到最新版本
          </el-button>
          <span class="muted">升级通过蓝牙 Secure DFU 进行，全程约 1–3 分钟。</span>
        </div>

        <el-table :data="packages" size="small" stripe>
          <el-table-column label="固件包" min-width="200">
            <template #default="{ row }">
              {{ row.filename }}
              <el-tag v-if="device && row.app_version === device.version" size="small" style="margin-left: 6px">
                当前版本
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="版本" width="70">
            <template #default="{ row }">{{ fmtVersion(row.app_version) }}</template>
          </el-table-column>
          <el-table-column label="大小" width="90">
            <template #default="{ row }">{{ fmtSize(row.size) }}</template>
          </el-table-column>
          <el-table-column label="上传时间" width="150">
            <template #default="{ row }">{{ formatDateTime(row.uploaded_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="120">
            <template #default="{ row }">
              <el-button link type="primary" @click="upgrade(row)">升级</el-button>
              <el-button link type="danger" @click="removePackage(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="还没有固件包，从右上角上传 *-ota.zip" :image-size="80" />
          </template>
        </el-table>

        <el-upload
          drag
          :auto-upload="false"
          :show-file-list="false"
          accept=".zip"
          :limit="1"
          :on-exceed="handleExceed"
          :on-change="onChange"
          style="margin-top: 12px"
        >
          <el-icon style="font-size: 28px; color: var(--el-text-color-secondary)"><UploadFilled /></el-icon>
          <div class="el-upload__text">
            拖拽 <b>*-ota.zip</b> 到此处，或 <em>点击选择</em>
          </div>
          <template #tip>
            <div class="el-upload__tip muted">
              包由 EPD-nRF5 仓库构建：<code>make -f Makefile.firmware ota</code>
            </div>
          </template>
        </el-upload>
      </el-card>
    </el-col>

    <el-col :span="10">
      <el-card shadow="never">
        <template #header>
          <div style="display: flex; align-items: center; gap: 10px">
            <span>升级状态</span>
            <el-tag v-if="inProgress" type="warning" effect="light">传输中</el-tag>
            <el-tag v-else-if="status?.ok === true" type="success" effect="light">上次成功</el-tag>
            <el-tag v-else-if="status?.ok === false" type="danger" effect="light">上次失败</el-tag>
          </div>
        </template>
        <el-progress
          v-if="inProgress"
          :percentage="percent"
          :stroke-width="16"
          striped
          striped-flow
        />
        <el-descriptions :column="1" border style="margin-top: 8px">
          <el-descriptions-item label="目标设备">{{ status?.device ?? "—" }}</el-descriptions-item>
          <el-descriptions-item label="固件包">{{ status?.firmware ?? "—" }}</el-descriptions-item>
          <el-descriptions-item label="传输进度">
            <template v-if="status?.total_bytes">
              {{ status.sent_bytes }} / {{ status.total_bytes }} 字节
            </template>
            <template v-else>—</template>
          </el-descriptions-item>
          <el-descriptions-item label="开始时间">
            {{ formatDateTime(status?.started_at ?? null) }}
          </el-descriptions-item>
          <el-descriptions-item v-if="status?.error" label="错误">
            <span style="color: var(--el-color-danger)">{{ status.error }}</span>
          </el-descriptions-item>
        </el-descriptions>
        <div class="muted" style="margin-top: 10px">
          流程：设备进入 bootloader → 传输签名固件包 → 自动重启新固件。升级后「设备固件」版本应变为目标版本。
        </div>
      </el-card>
    </el-col>
  </el-row>
</template>
