import { createRouter, createWebHashHistory } from "vue-router";

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", name: "dashboard", component: () => import("./views/DashboardView.vue") },
    { path: "/foods", name: "foods", component: () => import("./views/FoodsView.vue") },
    { path: "/push", name: "push", component: () => import("./views/PushView.vue") },
    { path: "/ota", name: "ota", component: () => import("./views/OtaView.vue") },
    { path: "/settings", name: "settings", component: () => import("./views/SettingsView.vue") },
  ],
});

export default router;
