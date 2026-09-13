from __future__ import annotations


SUPPORTED_LANGUAGES = {"zh", "en"}


ENGLISH_ERROR_MESSAGES = {
    "already_setup": "Administrator setup has already been completed.",
    "automatic_task_finished": "The automatic task has already finished.",
    "automatic_task_lease_expired": "The automatic task execution lease has expired.",
    "automatic_task_not_found": "The automatic task was not found.",
    "automatic_task_owner_mismatch": "The automatic task is not owned by this OpenCode session.",
    "automatic_task_running": "A running automatic task cannot be changed.",
    "automatic_tasks_busy": "Another OpenCode session is running the automatic-task queue.",
    "authentication_required": "Authentication is required.",
    "cannot_delete_current_user": "The currently signed-in user cannot be deleted.",
    "cannot_delete_last_user": "The last user cannot be deleted.",
    "cmd_not_found": "Windows Command Processor was not found.",
    "database_error": "The persistent storage operation failed.",
    "direct_loopback_required": "Initial setup must use a direct loopback address.",
    "http_error": "The request failed.",
    "internal_error": "An internal server error occurred.",
    "invalid_action": "The requested action is invalid.",
    "invalid_credentials": "The username or password is invalid.",
    "invalid_csrf": "CSRF validation failed.",
    "invalid_description": "The description is invalid or too long.",
    "invalid_detailed_description": "The detailed scene instructions are invalid or too long.",
    "invalid_gpu": "The GPU label is too long.",
    "invalid_health_expect": "The health-response match text is invalid.",
    "invalid_health_url": "The health-check URL must use HTTP or HTTPS on local loopback.",
    "invalid_name": "The name is invalid.",
    "invalid_port": "The service port must be an integer from 1 to 65535.",
    "invalid_scene_order": "The scene order must contain every existing scene exactly once.",
    "active_scene_missing": "No active scene is available to restore after video generation.",
    "default_generation_scene_missing": "No default generation scene is configured.",
    "generation_scene_not_found": "The requested generation scene was not found.",
    "invalid_file_path": "The file path is invalid.",
    "file_path_outside_root": "The requested path is outside the file-service root.",
    "file_not_found": "The requested file or folder was not found.",
    "not_a_directory": "The requested path is not a folder.",
    "not_a_file": "The requested path is not a file.",
    "directory_access_denied": "Access to the requested folder was denied.",
    "directory_read_failed": "The requested folder could not be read.",
    "file_metadata_failed": "A folder entry could not be read.",
    "file_access_denied": "Access to the requested file was denied.",
    "file_read_failed": "The requested file could not be read.",
    "file_path_verification_failed": "The opened file path could not be verified.",
    "invalid_file_range": "The requested file range is invalid.",
    "invalid_file_sort": "The requested file sort field is invalid.",
    "invalid_file_sort_order": "The requested file sort order is invalid.",
    "invalid_rename_name": "The new file or folder name is invalid.",
    "rename_target_exists": "A file or folder with that name already exists and was not overwritten.",
    "rename_access_denied": "The file or folder could not be renamed because access was denied.",
    "rename_failed": "The file or folder could not be renamed.",
    "invalid_upload_name": "The upload file name is invalid.",
    "upload_file_exists": "A file with the same name already exists and was not overwritten.",
    "upload_access_denied": "Access to the upload destination was denied.",
    "upload_open_failed": "The upload file could not be created.",
    "upload_write_failed": "The upload file could not be written.",
    "original_scene_missing": "The original scene is no longer available for restoration.",
    "invalid_script": "The management script path or file type is invalid.",
    "invalid_services": "Scene services must be an ordered array of IDs.",
    "invalid_session": "The session is invalid or has expired.",
    "invalid_stored_state": "The service state could not be saved.",
    "invalid_desired_state": "The desired service state could not be saved.",
    "invalid_ui_url": "The UI address must be a complete HTTP or HTTPS URL.",
    "invalid_wsl_distro": "The WSL distribution name is invalid.",
    "invalid_wsl_listen_address": "The WSL port-forward listen address is invalid.",
    "invalid_wsl_portproxy_port": "The WSL port-forward port is invalid.",
    "loopback_required": "This operation is only allowed from the local computer.",
    "callback_loopback_required": "The callback address must use local loopback.",
    "idempotency_conflict": "This idempotency key belongs to a different video job.",
    "invalid_callback_url": "The callback URL is invalid.",
    "invalid_callback_directory": "The callback directory must be an absolute path.",
    "invalid_idempotency_key": "The video job idempotency key is invalid.",
    "invalid_output_path": "The video output path must be absolute.",
    "invalid_session_id": "The OpenCode session ID is invalid.",
    "invalid_workflow_path": "The workflow path must be an absolute JSON path.",
    "workflow_not_found": "The workflow JSON file was not found.",
    "manager_already_running": "Another manager instance is already using this database.",
    "manager_unlock_failed": "The manager instance lock could not be released.",
    "operation_busy": "A service or scene operation is already running.",
    "operation_finished": "The scene switch has already finished.",
    "operation_not_cancellable": "Only a scene switch can be cancelled.",
    "operation_not_found": "The operation record was not found.",
    "gpu_4090_leased": "RTX 4090 is exclusively leased by a video job.",
    "powershell_not_found": "PowerShell was not found.",
    "portproxy_sync_failed": "The managed WSL port forwarding could not be synchronized.",
    "rate_limited": "Too many failed sign-in attempts. Try again later.",
    "request_body_too_large": "The request body exceeds the allowed size.",
    "scene_conflict": "The scene was changed by another request. Refresh and try again.",
    "scene_not_found": "The scene was not found.",
    "script_launch_failed": "The management script could not be started.",
    "script_not_found": "The management script was not found.",
    "script_timeout": "The management script timed out.",
    "service_busy": "A service operation is already running.",
    "service_conflict": "The service was changed by another request. Refresh and try again.",
    "service_not_found": "The registered service was not found.",
    "setup_disabled": "Initial setup is disabled by the deployment configuration.",
    "setup_required": "The administrator is not configured. Access is limited to the local computer.",
    "user_not_found": "The user was not found.",
    "username_exists": "The username already exists.",
    "validation_error": "The request parameters are invalid.",
    "video_job_finished": "The video job has already finished.",
    "video_job_not_found": "The video job was not found.",
    "weak_password": "The password must contain at least 4 characters.",
}

STANDARD_HTTP_MESSAGES = {
    404: {"zh": "请求的资源不存在。", "en": "The requested resource was not found."},
    405: {"zh": "请求方法不允许。", "en": "The request method is not allowed."},
}


def normalize_language(accept_language: str | None) -> str:
    if not accept_language:
        return "zh"
    primary = accept_language.split(",", 1)[0].strip().lower()
    return "zh" if primary == "zh" or primary.startswith("zh-") else "en"


def localize_error(code: str, fallback: str, accept_language: str | None) -> tuple[str, str]:
    language = normalize_language(accept_language)
    if language == "zh":
        return fallback, language
    return ENGLISH_ERROR_MESSAGES.get(code, ENGLISH_ERROR_MESSAGES["http_error"]), language


def localize_http_error(status_code: int, accept_language: str | None) -> tuple[str, str]:
    language = normalize_language(accept_language)
    messages = STANDARD_HTTP_MESSAGES.get(
        status_code, {"zh": "请求失败。", "en": "The request failed."}
    )
    return messages[language], language
