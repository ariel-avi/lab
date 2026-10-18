# Coverage helpers: gcov instrumentation (--coverage) and a gcovr report/gate target.
#
# enable_coverage_for(<target>)
#     Compiles <target> with --coverage -O0 -g (GCC or Clang) and links it with --coverage. The link option is
#     PUBLIC, so executables that link an instrumented library also link the gcov runtime.
#
# add_coverage_target(NAME <name>
#                     [ROOT <dir>]                       source root, default: CMAKE_SOURCE_DIR
#                     [OBJECT_DIRECTORY <dir>]           build tree (.gcno/.gcda), default: CMAKE_BINARY_DIR
#                     [OUTPUT_DIRECTORY <dir>]           report directory, default: CMAKE_BINARY_DIR/<name>
#                     [FILTERS <regex>...]               gcovr --filter, relative to ROOT (default: all of ROOT)
#                     [EXCLUDES <regex>...]              gcovr --exclude, relative to ROOT
#                     [FAIL_UNDER_LINE <percent>]
#                     [FAIL_UNDER_BRANCH <percent>]
#                     [GCOV_EXECUTABLE <exe> [<arg>...]] default: gcov-<major> or gcov (GCC),
#                                                        llvm-cov-<major> gcov or llvm-cov gcov (Clang)
#                     [CTEST_ARGS <arg>...]
#                     [DEPENDS <target>...])
#     The target deletes stale .gcda files, runs ctest in CMAKE_BINARY_DIR and then gcovr, which prints a summary and
#     writes summary.txt, index.html (details) and coverage.xml (Cobertura) to OUTPUT_DIRECTORY. It fails when a test
#     fails or a threshold is missed. gcovr is looked up as GCOVR_EXE (pre-set it to override).

function(enable_coverage_for TARGET_NAME)
    if (NOT TARGET ${TARGET_NAME})
        message(FATAL_ERROR "enable_coverage_for: target '${TARGET_NAME}' not found")
    endif ()
    if (NOT CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
        message(WARNING "Coverage needs GCC or Clang. '${TARGET_NAME}' is not instrumented.")
        return()
    endif ()

    target_compile_options(${TARGET_NAME} PRIVATE --coverage -O0 -g)
    target_link_options(${TARGET_NAME} PUBLIC --coverage)
endfunction()

function(add_coverage_target)
    cmake_parse_arguments(COV
            ""
            "NAME;ROOT;OBJECT_DIRECTORY;OUTPUT_DIRECTORY;FAIL_UNDER_LINE;FAIL_UNDER_BRANCH"
            "FILTERS;EXCLUDES;GCOV_EXECUTABLE;CTEST_ARGS;DEPENDS"
            ${ARGN})

    if (NOT COV_NAME)
        message(FATAL_ERROR "add_coverage_target: NAME is required")
    endif ()
    if (NOT COV_ROOT)
        set(COV_ROOT "${CMAKE_SOURCE_DIR}")
    endif ()
    if (NOT COV_OBJECT_DIRECTORY)
        set(COV_OBJECT_DIRECTORY "${CMAKE_BINARY_DIR}")
    endif ()
    if (NOT COV_OUTPUT_DIRECTORY)
        set(COV_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/${COV_NAME}")
    endif ()

    # find_program keeps a pre-set value and searches again while the cached value is NOTFOUND.
    find_program(GCOVR_EXE NAMES gcovr)
    if (NOT GCOVR_EXE)
        message(WARNING "gcovr not found. '${COV_NAME}' target will be skipped.")
        return()
    endif ()

    # The gcov tool must match the compiler that produced the .gcno/.gcda files.
    if (COV_GCOV_EXECUTABLE)
        set(_gcov_cmd ${COV_GCOV_EXECUTABLE})
    else ()
        string(REGEX MATCH "^[0-9]+" _major "${CMAKE_CXX_COMPILER_VERSION}")
        get_filename_component(_compiler_dir "${CMAKE_CXX_COMPILER}" DIRECTORY)
        if (CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
            find_program(GCOV_EXE NAMES gcov-${_major} gcov HINTS "${_compiler_dir}")
            set(_gcov_cmd "${GCOV_EXE}")
        elseif (CMAKE_CXX_COMPILER_ID MATCHES "Clang")
            find_program(LLVM_COV_EXE NAMES llvm-cov-${_major} llvm-cov HINTS "${_compiler_dir}")
            set(_gcov_cmd "${LLVM_COV_EXE}" gcov)
        endif ()
    endif ()
    if (NOT _gcov_cmd OR _gcov_cmd MATCHES "NOTFOUND")
        message(WARNING "No gcov tool matching the compiler found. '${COV_NAME}' target will be skipped.")
        return()
    endif ()
    list(JOIN _gcov_cmd " " _gcov_cmd)

    set(_gcovr_args
            --root "${COV_ROOT}"
            --object-directory "${COV_OBJECT_DIRECTORY}"
            --gcov-executable "${_gcov_cmd}"
            --exclude-unreachable-branches
            --exclude-throw-branches)
    foreach (_filter IN LISTS COV_FILTERS)
        list(APPEND _gcovr_args --filter "${_filter}")
    endforeach ()
    foreach (_exclude IN LISTS COV_EXCLUDES)
        list(APPEND _gcovr_args --exclude "${_exclude}")
    endforeach ()
    if (DEFINED COV_FAIL_UNDER_LINE)
        list(APPEND _gcovr_args --fail-under-line ${COV_FAIL_UNDER_LINE})
    endif ()
    if (DEFINED COV_FAIL_UNDER_BRANCH)
        list(APPEND _gcovr_args --fail-under-branch ${COV_FAIL_UNDER_BRANCH})
    endif ()
    # The trailing search path limits gcovr to this build tree (by default it also scans ROOT, i.e. every build tree
    # inside the source tree).
    list(APPEND _gcovr_args
            --print-summary
            --txt "${COV_OUTPUT_DIRECTORY}/summary.txt"
            --html-details "${COV_OUTPUT_DIRECTORY}/index.html"
            --xml "${COV_OUTPUT_DIRECTORY}/coverage.xml"
            "${COV_OBJECT_DIRECTORY}")

    # Counters left by earlier runs would be added to this run: delete them first.
    set(_clean_script "${CMAKE_CURRENT_BINARY_DIR}/${COV_NAME}_clean_gcda.cmake")
    file(CONFIGURE OUTPUT "${_clean_script}" CONTENT [[
file(GLOB_RECURSE _gcda_files "@COV_OBJECT_DIRECTORY@/*.gcda")
if (_gcda_files)
    file(REMOVE ${_gcda_files})
endif ()
]] @ONLY)

    set(_ctest_cmd ${CMAKE_CTEST_COMMAND} --test-dir "${CMAKE_BINARY_DIR}")
    get_property(_multi_config GLOBAL PROPERTY GENERATOR_IS_MULTI_CONFIG)
    if (_multi_config)
        list(APPEND _ctest_cmd -C $<CONFIG>)
    endif ()

    # gcovr resolves relative --filter/--exclude against its working directory, so it runs in ROOT.
    add_custom_target(${COV_NAME}
            COMMAND ${CMAKE_COMMAND} -P "${_clean_script}"
            COMMAND ${_ctest_cmd} ${COV_CTEST_ARGS}
            COMMAND ${CMAKE_COMMAND} -E make_directory "${COV_OUTPUT_DIRECTORY}"
            COMMAND ${GCOVR_EXE} ${_gcovr_args}
            WORKING_DIRECTORY "${COV_ROOT}"
            COMMENT "Running tests and gcovr coverage report '${COV_NAME}'"
            VERBATIM)
    if (COV_DEPENDS)
        add_dependencies(${COV_NAME} ${COV_DEPENDS})
    endif ()
endfunction()
