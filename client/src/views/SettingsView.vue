<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { Connection } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "../api/client";
import type { Meta } from "../api/types";
import { applyTheme, resetSettings, settings } from "../lib/settings";

const testing = ref(false);
const meta = ref<Meta | null>(null);
const hint = reactive({ saved: false });

function onThemeChange() {
  applyTheme();
  hint.saved = true;
}

async function testConnection() {
  testing.value = true;
  try {
    const [health, m] = await Promise.all([api.health(), api.meta()]);
    meta.value = m;
    if (health.ok) {
      ElMessage.success("连接成功，数据库正常");
    } else {
      ElMessage.warning(`服务可达，但数据库异常：${health.error ?? "未知"}`);
    }
  } catch {
    /* 错误已由 api 层提示 */
  } finally {
    testing.value = false;
  }
}

function resetAll() {
  resetSettings();
  applyTheme();
  ElMessage.success("已恢复默认设置");
}

onMounted(() => {
  api.meta().then((m) => (meta.value = m)).catch(() => undefined);
});
</script>

<template>
  <el-row :gutter="14">
    <el-col :span="12">
      <el-card shadow="never">
        <template #header>服务端连接</template>
        <el-form label-width="90px">
          <el-form-item label="服务器">
            <el-input
              v-model="settings.serverUrl"
              placeholder="https://food.opi5p.zhyi.xin"
              clearable
            />
          </el-form-item>
          <el-form-item label="访问令牌">
            <el-input
              v-model="settings.token"
              type="password"
              show-password
              placeholder="服务端 EPD_FOOD_API_TOKEN"
            />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :icon="Connection" :loading="testing" @click="testConnection">
              测试连接
            </el-button>
            <span class="muted" style="margin-left: 10px">设置实时生效并保存在本机</span>
          </el-form-item>
        </el-form>
        <el-alert
          v-if="meta && !meta.font_available"
          type="warning"
          :closable="false"
          title="服务端未找到中文字体，名称位图将无法渲染（检查部署模块的 fontPackage）"
        />
        <el-alert
          v-if="meta"
          type="info"
          :closable="false"
          :title="`服务端字体：${meta.font_path ?? '未找到'}`"
        />
      </el-card>
    </el-col>
    <el-col :span="12">
      <el-card shadow="never">
        <template #header>外观</template>
        <el-form label-width="90px">
          <el-form-item label="主题">
            <el-radio-group v-model="settings.theme" @change="onThemeChange">
              <el-radio-button value="light">浅色</el-radio-button>
              <el-radio-button value="dark">深色</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item>
            <el-button link type="danger" @click="resetAll">恢复默认设置</el-button>
          </el-form-item>
        </el-form>
        <div class="muted">
          客户端只与本服务端通信（HTTPS + Bearer Token）；数据库与蓝牙均由服务端托管。
        </div>
      </el-card>
    </el-col>
  </el-row>
</template>
