execute_process(COMMAND git apply --reverse --check "${PATCH_FILE}"
    RESULT_VARIABLE applied ERROR_QUIET)
if(NOT applied EQUAL 0)
    execute_process(COMMAND git apply "${PATCH_FILE}" COMMAND_ERROR_IS_FATAL ANY)
endif()
