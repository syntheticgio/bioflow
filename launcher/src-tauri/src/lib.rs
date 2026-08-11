pub mod actions;
pub mod commands;
pub mod docker;
pub mod migrate;
pub mod optional_tools;
pub mod remote;
pub mod settings;
pub mod setup;
pub mod state;
pub mod update_check;

use commands::LauncherApp;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_opener::init())
    .manage(LauncherApp::default())
    .invoke_handler(tauri::generate_handler![
      commands::status,
      commands::docker_ready,
      commands::run_stack,
      commands::stop_stack,
      commands::update_stack,
      commands::setup_defaults,
      commands::validate_storage,
      commands::validate_setup_port,
      commands::run_first_setup,
      commands::apply_settings,
      commands::check_for_update,
      commands::fetch_optional_tools,
      commands::install_optional_tool,
      commands::start_storage_migration,
      commands::migration_progress,
      commands::finish_storage_migration,
      commands::current_settings,
      commands::discover_node_connection,
      commands::install_node_local,
      commands::test_ssh_connection,
      commands::install_node_remote,
      commands::run_node,
      commands::stop_node,
      commands::node_status,
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
