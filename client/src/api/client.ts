import axios, { type AxiosInstance } from "axios";
import { ElMessage } from "element-plus";
import { settings } from "../lib/settings";
import type {
  DeviceVersion,
  Food,
  FoodInput,
  Health,
  Meta,
  OtaPackage,
  OtaStatus,
  PreviewItem,
  PushStatus,
  Stats,
} from "./types";

/** 每次调用按当前设置新建实例，避免过期 baseURL/token。 */
function http(): AxiosInstance {
  const base = settings.serverUrl.trim().replace(/\/+$/, "");
  return axios.create({
    baseURL: `${base}/api`,
    timeout: 20000,
    headers: settings.token ? { Authorization: `Bearer ${settings.token}` } : {},
  });
}

export function errMsg(e: unknown): string {
  if (axios.isAxiosError(e)) {
    const detail = e.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const first = detail[0];
      return first?.msg ? `${first.loc?.slice(1).join(".")}: ${first.msg}` : "参数校验失败";
    }
    return e.message;
  }
  return String(e);
}

async function wrap<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    ElMessage.error(errMsg(e));
    throw e;
  }
}

export const api = {
  health: () => wrap<Health>(async () => (await http().get("/health")).data),
  meta: () => wrap<Meta>(async () => (await http().get("/meta")).data),

  listFoods: (status = "active") =>
    wrap<Food[]>(async () =>
      (await http().get("/foods", { params: { status_filter: status } })).data,
    ),
  stats: () => wrap<Stats>(async () => (await http().get("/foods/stats")).data),
  createFood: (body: FoodInput) =>
    wrap<Food>(async () => (await http().post("/foods", body)).data),
  updateFood: (id: number, patch: Partial<FoodInput>) =>
    wrap<Food>(async () => (await http().patch(`/foods/${id}`, patch)).data),
  consumeFood: (id: number) =>
    wrap<Food>(async () => (await http().delete(`/foods/${id}`)).data),
  restoreFood: (id: number) =>
    wrap<Food>(async () => (await http().post(`/foods/${id}/restore`)).data),
  deleteFoodHard: (id: number) =>
    wrap<Food>(async () => (await http().delete(`/foods/${id}`, { params: { hard: true } })).data),

  push: () =>
    wrap<{ started: boolean; busy: boolean }>(
      async () => (await http().post("/epd/push")).data,
    ),
  pushStatus: () => wrap<PushStatus>(async () => (await http().get("/epd/status")).data),
  preview: () => wrap<PreviewItem[]>(async () => (await http().get("/epd/preview")).data),

  otaList: () => wrap<OtaPackage[]>(async () => (await http().get("/ota/firmware")).data),
  otaUpload: (filename: string, content: ArrayBuffer | Blob) =>
    wrap<OtaPackage>(
      async () =>
        (
          await http().post(`/ota/firmware?filename=${encodeURIComponent(filename)}`, content, {
            headers: { "Content-Type": "application/zip" },
            timeout: 120000,
          })
        ).data,
    ),
  otaDelete: (id: number) =>
    wrap<{ deleted: boolean }>(async () => (await http().delete(`/ota/firmware/${id}`)).data),
  otaDeviceVersion: () =>
    wrap<DeviceVersion>(async () => (await http().get("/ota/device", { timeout: 30000 })).data),
  otaPush: (firmwareId?: number) =>
    wrap<{ started: boolean; busy: boolean; firmware: { id: number; app_version: number | null } }>(
      async () =>
        (await http().post("/ota/push", null, { params: firmwareId ? { firmware_id: firmwareId } : {} }))
          .data,
    ),
  otaStatus: () => wrap<OtaStatus>(async () => (await http().get("/ota/status")).data),
};
