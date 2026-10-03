/*
 * memory.cpp — Persists chat history per user with automatic file rotation.
 * Uses localtime_s / localtime_r for thread-safe time formatting.
 */

#include "memory.h"
#include "logger.h"
#include "user.h"
#include <chrono>
#include <ctime>
#include <fstream>
#include <iostream>
#include <sstream>
#include <iomanip>
#include <filesystem>

using namespace std;

static string makeFilename(const string& user) {
    auto token = sanitizePathToken(user);
    if (token.empty()) token = "user";
    return "chat_" + token + ".txt";
}

static string fmtTime(const char* fmt) {
    auto t = chrono::system_clock::to_time_t(chrono::system_clock::now());
    tm buf{};
#ifdef _WIN32
    localtime_s(&buf, &t);
#else
    localtime_r(&t, &buf);
#endif
    ostringstream ss; ss << put_time(&buf, fmt); return ss.str();
}

Memory::Memory(const string& user, size_t maxBytes)
    : filename(makeFilename(user)), userName(user), maxFileBytes(maxBytes) {}

void Memory::setUserName(const string& user) {
    userName = user; filename = makeFilename(user);
}

void Memory::saveChat(string msg) {
    if (msg.length() > 50000) { Logger::log("WARN", "Message truncated for storage"); msg = msg.substr(0, 50000); }

    // Rotate file if it exceeds size limit
    bool shouldRotate = false;
    {
        if (ifstream chk(filename, ios::ate | ios::binary); chk.is_open()) {
            if (static_cast<size_t>(chk.tellg()) >= maxFileBytes) {
                shouldRotate = true;
            }
        }
    }

    if (shouldRotate) {
        string stamp = fmtTime("%Y-%m-%d_%H%M%S");
        std::filesystem::path file_path(filename);
        std::filesystem::path rotated_path = file_path;
        std::string stem = file_path.stem().string();
        std::string ext = file_path.extension().string();
        rotated_path.replace_filename(stem + "_" + stamp + ext);
        
        std::error_code ec;
        std::filesystem::rename(file_path, rotated_path, ec);
        if (!ec) {
            Logger::log("INFO", "History rotated to: " + rotated_path.string());
        } else {
            Logger::log("ERROR", "Could not rotate history file: " + ec.message());
        }
    }

    ofstream file(filename, ios::app);
    if (!file.is_open()) { Logger::log("ERROR", "Cannot write history: " + filename); return; }
    file << "[" << fmtTime("%Y-%m-%d %H:%M:%S") << "] " << msg << "\n";
    if (!file.good()) Logger::log("ERROR", "Write failed: " + filename);
}

void Memory::showHistory() const {
    ifstream file(filename);
    if (!file.is_open()) { cout << "No local history found.\n"; return; }
    cout << "\n--- Local Chat History (" << filename << ") ---\n";
    for (string line; getline(file, line);) cout << line << "\n";
    cout << "--- End of History ---\n\n";
}

void Memory::clearHistoryFile() {
    if (ofstream f(filename, ios::trunc); !f.is_open())
        Logger::log("ERROR", "Cannot clear history: " + filename);
    else
        Logger::log("INFO", "History cleared: " + filename);
}