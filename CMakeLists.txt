cmake_minimum_required(VERSION 3.24)
project(gba_link LANGUAGES C CXX)
set(CMAKE_C_STANDARD 11)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
include(FetchContent)
foreach(feature BUILD_QT BUILD_SDL BUILD_GL BUILD_GLES2 BUILD_GLES3
        BUILD_SHARED BUILD_LIBRETRO ENABLE_DEBUGGERS ENABLE_SCRIPTING
        USE_FFMPEG USE_ZLIB USE_MINIZIP USE_PNG USE_LIBZIP USE_SQLITE3
        USE_ELF USE_LUA USE_JSON_C USE_FREETYPE USE_LZMA USE_DISCORD_RPC USE_EPOXY)
    set(${feature} OFF CACHE BOOL "" FORCE)
endforeach()
set(BUILD_STATIC ON CACHE BOOL "" FORCE)
set(M_CORE_GB OFF CACHE BOOL "" FORCE)
FetchContent_Declare(mgba
    GIT_REPOSITORY https://github.com/mgba-emu/mgba.git
    GIT_TAG 1d201b22a86d31dfb3bc75145403711f6762015f
    PATCH_COMMAND ${CMAKE_COMMAND}
        "-DPATCH_FILE=${CMAKE_CURRENT_SOURCE_DIR}/native/mgba-lockstep.patch"
        -P ${CMAKE_CURRENT_SOURCE_DIR}/native/patch-mgba.cmake)
FetchContent_MakeAvailable(mgba)
find_package(Threads REQUIRED)
add_library(gba_link SHARED native/link.c)
target_link_libraries(gba_link PRIVATE mgba Threads::Threads)
target_compile_definitions(gba_link PRIVATE $<TARGET_PROPERTY:mgba,COMPILE_DEFINITIONS>)
target_compile_options(gba_link PRIVATE -Wall -Wextra -Werror)
