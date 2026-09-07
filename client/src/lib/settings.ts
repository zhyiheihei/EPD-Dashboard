import { reactive, watch } from "vue";

const STORAGE_KEY = "food-keeper-settings";

export interface SettingsState {
  serverUrl: string;
  token: string;
  theme: "light" | "dark";
}

function load(): Partial<SettingsState> {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
  } catch {
    return {};
  }
}

export const settings = reactive<SettingsState>({
  serverUrl: "https://food.opi5p.zhyi.xin",
  token: "",
  theme: "light",
  ...load(),
});

watch(
  settings,
  (value) => localStorage.setItem(STORAGE_KEY, JSON.stringify(value)),
  { deep: true },
);

export function applyTheme() {
  document.documentElement.classList.toggle("dark", settings.theme === "dark");
}

export function resetSettings() {
  settings.serverUrl = "https://food.opi5p.zhyi.xin";
  settings.token = "";
  settings.theme = "light";
  localStorage.removeItem(STORAGE_KEY);
}
