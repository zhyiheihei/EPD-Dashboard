// 无边框窗口 + 自定义标题栏：窗口控制均由前端经 Tauri IPC 完成，
// Rust 侧无需注册任何命令。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
