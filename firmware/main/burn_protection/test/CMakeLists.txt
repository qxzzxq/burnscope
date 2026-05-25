cmake_minimum_required(VERSION 3.16)
project(burn_idle_host_tests C)

# Host-only build for the pure-logic idle state machine. Run with:
#   cmake -S firmware/main/burn_protection/test -B build/burn_idle_host
#   cmake --build build/burn_idle_host
#   ctest --test-dir build/burn_idle_host --output-on-failure

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)

add_compile_options(-Wall -Wextra -Wpedantic -Werror)

option(BURN_IDLE_COVERAGE "Emit gcov instrumentation" OFF)
if(BURN_IDLE_COVERAGE)
    add_compile_options(--coverage -O0 -g)
    add_link_options(--coverage)
endif()

enable_testing()

add_executable(burn_idle_test
    burn_idle_test.c
    ../burn_idle.c
)
target_include_directories(burn_idle_test PRIVATE ../..)

add_test(NAME burn_idle_unit COMMAND burn_idle_test)

add_test(
    NAME burn_idle_purity
    COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/check_purity.sh
                 ${CMAKE_CURRENT_SOURCE_DIR}/../burn_idle.c
)

if(BURN_IDLE_COVERAGE)
    add_custom_target(coverage
        COMMAND ${CMAKE_COMMAND} -E echo "=== gcov report ==="
        COMMAND gcov -b
                ${CMAKE_CURRENT_BINARY_DIR}/CMakeFiles/burn_idle_test.dir/__/burn_idle.c.gcno
        DEPENDS burn_idle_test
        WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        COMMENT "Run gcov against burn_idle.c"
    )
endif()
