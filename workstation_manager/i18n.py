from __future__ import annotations


SUPPORTED_LANGUAGES = {"zh", "en"}


ENGLISH_ERROR_MESSAGES = {
    "already_setup": "Administrator setup has already been completed.",
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
    "invalid_script": "The management script path or file type is invalid.",
    "invalid_services": "Scene services must be an ordered array of IDs.",
    "invalid_session": "The session is invalid or has expired.",
    "invalid_stored_state": "The service state could not be saved.",
    "invalid_desired_state": "The desired service state could not be saved.",
    "invalid_ui_url": "The UI address must be a complete HTTP or HTTPS URL.",
    "loopback_required": "This operation is only allowed from the local computer.",
    "manager_already_running": "Another manager instance is already using this database.",
    "manager_unlock_failed": "The manager instance lock could not be released.",
    "operation_busy": "A service or scene operation is already running.",
    "operation_finished": "The scene switch has already finished.",
    "operation_not_cancellable": "Only a scene switch can be cancelled.",
    "operation_not_found": "The operation record was not found.",
    "powershell_not_found": "PowerShell was not found.",
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
