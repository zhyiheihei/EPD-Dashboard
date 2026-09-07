<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { Plus, Refresh, Search } from "@element-plus/icons-vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api/client";
import type { Food, Meta } from "../api/types";
import ExpiryTag from "../components/ExpiryTag.vue";
import { formatDate, humanShelfLife } from "../lib/format";

const foods = ref<Food[]>([]);
const meta = ref<Meta | null>(null);
const loading = ref(false);

const filters = reactive({ search: "", category: "", status: "active" });

const visibleFoods = computed(() =>
  foods.value.filter((food) => {
    if (filters.search && !food.name.toLowerCase().includes(filters.search.toLowerCase()))
      return false;
    if (filters.category && food.category !== filters.category) return false;
    return true;
  }),
);

async function load() {
  loading.value = true;
  try {
    const [list, m] = await Promise.all([api.listFoods(filters.status), meta.value ? Promise.resolve(meta.value) : api.meta()]);
    foods.value = list;
    meta.value = m;
  } finally {
    loading.value = false;
  }
}

// ---------- 新增 / 编辑 ----------

const dialog = reactive({
  visible: false,
  editingId: null as number | null,
  name: "",
  category: "食品",
  productionDate: "",
  amount: 1,
  unit: "日" as "日" | "月" | "年",
});

const UNIT_DAYS: Record<string, number> = { 日: 1, 月: 30, 年: 365 };
const formRef = ref();
const formRules = {
  name: [{ required: true, message: "请输入食品名称", trigger: "blur" }],
  category: [{ required: true, message: "请选择品类型", trigger: "change" }],
  productionDate: [{ required: true, message: "请选择生产日期", trigger: "change" }],
};

function openCreate() {
  dialog.editingId = null;
  dialog.name = "";
  dialog.category = "食品";
  dialog.productionDate = new Date().toISOString().slice(0, 10);
  dialog.amount = 1;
  dialog.unit = "日";
  dialog.visible = true;
}

function openEdit(food: Food) {
  dialog.editingId = food.id;
  dialog.name = food.name;
  dialog.category = food.category;
  dialog.productionDate = food.production_date;
  if (food.shelf_life_days > 0 && food.shelf_life_days % 365 === 0) {
    dialog.unit = "年";
    dialog.amount = food.shelf_life_days / 365;
  } else if (food.shelf_life_days > 0 && food.shelf_life_days % 30 === 0) {
    dialog.unit = "月";
    dialog.amount = food.shelf_life_days / 30;
  } else {
    dialog.unit = "日";
    dialog.amount = food.shelf_life_days;
  }
  dialog.visible = true;
}

async function submit() {
  await formRef.value?.validate().catch(() => Promise.reject(new Error("cancel")));
  const shelfLifeDays = Math.round(dialog.amount * UNIT_DAYS[dialog.unit]);
  const body = {
    name: dialog.name.trim(),
    category: dialog.category.trim(),
    production_date: dialog.productionDate,
    shelf_life_days: shelfLifeDays,
    quantity: 1,
  };
  if (dialog.editingId === null) {
    await api.createFood(body);
    ElMessage.success("已添加，稍后自动推送到墨水屏");
  } else {
    await api.updateFood(dialog.editingId, body);
    ElMessage.success("已更新，稍后自动推送到墨水屏");
  }
  dialog.visible = false;
  await load();
}

// ---------- 操作 ----------

async function consume(food: Food) {
  await ElMessageBox.confirm(`确认「${food.name}」已吃完？`, "标记吃完", { type: "warning" });
  await api.consumeFood(food.id);
  ElMessage.success("已标记吃完");
  await load();
}

async function restore(food: Food) {
  await api.restoreFood(food.id);
  ElMessage.success("已恢复");
  await load();
}

async function removeHard(food: Food) {
  await ElMessageBox.confirm(`永久删除「${food.name}」？该操作不可恢复。`, "删除记录", {
    type: "error",
    confirmButtonText: "永久删除",
  });
  await api.deleteFoodHard(food.id);
  ElMessage.success("已删除");
  await load();
}

onMounted(load);
</script>

<template>
  <div>
    <div class="toolbar">
      <el-button type="primary" :icon="Plus" @click="openCreate">新增食品</el-button>
      <el-input
        v-model="filters.search"
        :prefix-icon="Search"
        placeholder="搜索名称"
        clearable
        style="width: 220px"
      />
      <el-select v-model="filters.category" placeholder="全部品类" clearable style="width: 140px">
        <el-option v-for="c in meta?.categories ?? []" :key="c" :label="c" :value="c" />
      </el-select>
      <el-radio-group v-model="filters.status" @change="load">
        <el-radio-button value="active">在库</el-radio-button>
        <el-radio-button value="consumed">已吃完</el-radio-button>
        <el-radio-button value="all">全部</el-radio-button>
      </el-radio-group>
      <el-button :icon="Refresh" circle @click="load" style="margin-left: auto" />
    </div>

    <el-table :data="visibleFoods" v-loading="loading" stripe>
      <el-table-column label="名称" min-width="160">
        <template #default="{ row }">
          <b>{{ row.name }}</b>
        </template>
      </el-table-column>
      <el-table-column label="品类型" width="100">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">{{ row.category }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="生产日期" width="110">
        <template #default="{ row }">{{ formatDate(row.production_date) }}</template>
      </el-table-column>
      <el-table-column label="保质期" width="90">
        <template #default="{ row }">{{ humanShelfLife(row.shelf_life_days) }}</template>
      </el-table-column>
      <el-table-column label="到期日" width="110">
        <template #default="{ row }">{{ formatDate(row.expiry_date) }}</template>
      </el-table-column>
      <el-table-column label="剩余" width="110">
        <template #default="{ row }">
          <ExpiryTag :days="row.days_remaining" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <template v-if="!row.consumed_at">
            <el-button link type="warning" @click="consume(row)">吃完了</el-button>
          </template>
          <el-button v-else link type="success" @click="restore(row)">恢复</el-button>
          <el-button link type="danger" @click="removeHard(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无记录，点击右上角「新增食品」开始记录" :image-size="90" />
      </template>
    </el-table>

    <el-dialog
      v-model="dialog.visible"
      :title="dialog.editingId === null ? '新增食品' : '编辑食品'"
      width="460px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="dialog" :rules="formRules" label-width="82px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="dialog.name" maxlength="32" placeholder="如：鲜牛奶" />
        </el-form-item>
        <el-form-item label="品类型" prop="category">
          <el-select
            v-model="dialog.category"
            filterable
            allow-create
            default-first-option
            placeholder="选择或输入"
            style="width: 100%"
          >
            <el-option v-for="c in meta?.categories ?? []" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="生产日期" prop="productionDate">
          <el-date-picker
            v-model="dialog.productionDate"
            type="date"
            value-format="YYYY-MM-DD"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="保质期">
          <el-input-number v-model="dialog.amount" :min="1" :max="9999" controls-position="right" />
          <el-radio-group v-model="dialog.unit" style="margin-left: 10px">
            <el-radio-button value="日">日</el-radio-button>
            <el-radio-button value="月">月</el-radio-button>
            <el-radio-button value="年">年</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>
