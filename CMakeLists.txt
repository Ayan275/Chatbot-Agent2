cmake_minimum_required(VERSION 3.16)
project(ChatbotCPP VERSION 2.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Dependency Management: FetchContent for nlohmann/json
include(FetchContent)
set(JSON_BuildTests OFF CACHE INTERNAL "")
FetchContent_Declare(
    json
    URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
)
FetchContent_MakeAvailable(json)

set(SHARED_SOURCES
    src/api.cpp
    src/chatbot.cpp
    src/memory.cpp
    src/auth.cpp
    src/AuditLogger.cpp   # Self-Accountable AI: Decision Audit Trail
    src/TrustTracker.cpp  # Self-Accountable AI: Trust Degradation Protocol
    src/sha256.cpp        # Self-Accountable AI: Hash Chain
)

add_executable(chatbot src/main.cpp ${SHARED_SOURCES})
target_include_directories(chatbot PRIVATE include)

# Link against nlohmann_json
target_link_libraries(chatbot PRIVATE nlohmann_json::nlohmann_json)

# Enable strict compilation warnings
if(MSVC)
    target_compile_options(chatbot PRIVATE /W4)
else()
    target_compile_options(chatbot PRIVATE -Wall -Wextra -Wpedantic)
endif()

configure_file(api.py ${CMAKE_BINARY_DIR}/api.py COPYONLY)
configure_file(${CMAKE_SOURCE_DIR}/config.json ${CMAKE_BINARY_DIR}/config.json COPYONLY)
configure_file(self_correction_chatbot.py ${CMAKE_BINARY_DIR}/self_correction_chatbot.py COPYONLY)

message(STATUS "Chatbot build configured with FetchContent")
