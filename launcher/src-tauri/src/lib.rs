pub mod actions;
pub mod commands;
pub mod docker;
pub mod settings;
pub mod setup;
pub mod state;

use commands::LauncherApp;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_opener::init())
    .manage(LauncherApp::default())
    .invoke_handler(tauri::generate_handler![
      commands::status,
      commands::run_stack,
      commands::stop_stack,
      commands::update_stack,
      commands::setup_defaults,
      commands::validate_storage,
      commands::validate_setup_port,
      commands::run_first_setup,
      commands::apply_settings,
    ])
    .setup(|app| {
      if cfg!(debug_assertions) {
        app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        )?;
      }
      Ok(())
    })
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
