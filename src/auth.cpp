#include "auth.h"
#include "sha256.h"
#include "logger.h"
#include <chrono>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <random>
#include <sstream>
#include <string>
#include <filesystem>
#include <cctype>
#include <nlohmann/json.hpp>

#ifdef _WIN32
#include <conio.h>
#else
#include <termios.h>
#include <unistd.h>
#endif

namespace {
std::filesystem::path projectRoot() {
    return std::filesystem::path(__FILE__).parent_path().parent_path();
}

std::string readPasswordInternal(const std::string& prompt) {
    std::string password;
#ifdef _WIN32
    std::cout << prompt;
    int ch;
    while ((ch = _getch()) != 13) {
        if (ch == 0 || ch == 224) {
            _getch(); // consume secondary character of function/special keys
        } else if (ch == 8 || ch == 127) {
            if (!password.empty()) {
                password.pop_back();
                std::cout << "\b \b";
            }
        } else if (ch == 3 || ch == 27) {
            password.clear();
            std::cout << "\n";
            return password;
        } else if (std::isprint(static_cast<unsigned char>(ch))) {
            password.push_back(static_cast<char>(ch));
            std::cout << '*';
        }
    }
    std::cout << "\n";
#else
    struct termios oldt{};
    tcgetattr(STDIN_FILENO, &oldt);
    struct termios newt = oldt;
    newt.c_lflag &= ~ECHO;
    tcsetattr(STDIN_FILENO, TCSANOW, &newt);

    std::cout << prompt;
    char ch = 0;
    while (read(STDIN_FILENO, &ch, 1) > 0) {
        if (ch == '\n' || ch == '\r') {
            std::cout << "\n";
            break;
        }
        if (ch == 127 || ch == 8) {
            if (!password.empty()) {
                password.pop_back();
                std::cout << "\b \b";
            }
        } else if (std::isprint(static_cast<unsigned char>(ch))) {
            password.push_back(ch);
            std::cout << '*';
        }
    }

    tcsetattr(STDIN_FILENO, TCSANOW, &oldt);
#endif
    return password;
}

nlohmann::json readUsersFile(const std::string& path) {
    std::ifstream in(path);
    if (!in.is_open()) return nlohmann::json::array();
    try {
        nlohmann::json j;
        in >> j;
        return j.is_array() ? j : nlohmann::json::array();
    } catch (const std::exception& e) {
        Logger::log("WARN", std::string("Failed to read users JSON: ") + e.what());
        return nlohmann::json::array();
    }
}

bool writeUsersFileAtomic(const std::string& path, const nlohmann::json& j) {
    try {
        std::filesystem::path target(path);
        auto tmp = target;
        tmp += ".tmp";
        {
            std::ofstream out(tmp, std::ios::trunc);
            if (!out.is_open()) return false;
            out << std::setw(2) << j << "\n";
            if (!out.good()) {
                out.close();
                std::filesystem::remove(tmp);
                return false;
            }
        }
        std::error_code ec;
        std::filesystem::rename(tmp, target, ec);
        if (ec) {
            std::filesystem::remove(tmp);
            Logger::log("ERROR", "Auth: atomic rename failed: " + ec.message());
            return false;
        }
        return true;
    } catch (const std::exception& e) {
        Logger::log("ERROR", std::string("Auth: write atomic failed: ") + e.what());
        return false;
    }
}
} // namespace

std::string getPasswordInput(const std::string& prompt) {
    return readPasswordInternal(prompt);
}

std::string Auth::usersFilePath() {
    return (projectRoot() / "users.json").string();
}

std::string Auth::timestampNow() {
    auto t = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
    std::tm buf{};
#ifdef _WIN32
    localtime_s(&buf, &t);
#else
    localtime_r(&t, &buf);
#endif
    std::ostringstream ss;
    ss << std::put_time(&buf, "%Y-%m-%dT%H:%M:%S");
    return ss.str();
}

std::string Auth::generateSalt() {
    static const std::string chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    std::random_device rd;
    std::mt19937 generator(rd());
    std::uniform_int_distribution<size_t> dist(0, chars.size() - 1);

    std::string salt;
    salt.reserve(16);
    for (int i = 0; i < 16; ++i) {
        salt.push_back(chars[dist(generator)]);
    }
    return salt;
}

std::string Auth::hashPassword(const std::string& salt, const std::string& password) {
    return SHA256::hashString(salt + password);
}

bool Auth::userExists(const std::string& username) {
    nlohmann::json j = readUsersFile(usersFilePath());
    for (const auto& entry : j) {
        if (entry.is_object() && entry.contains("username") && entry["username"].is_string()) {
            if (entry["username"].get<std::string>() == username) return true;
        }
    }
    return false;
}

std::string Auth::getStoredHash(const std::string& username) {
    nlohmann::json j = readUsersFile(usersFilePath());
    for (const auto& entry : j) {
        if (entry.is_object() && entry.contains("username") && entry["username"].is_string()) {
            if (entry["username"].get<std::string>() == username) {
                if (entry.contains("password_hash") && entry["password_hash"].is_string()) {
                    return entry["password_hash"].get<std::string>();
                }
            }
        }
    }
    return {};
}

bool Auth::registerUser(const std::string& username, const std::string& password) {
    if (username.empty() || password.size() < 6 || password.size() > 64) return false;
    if (userExists(username)) return false;

    const std::string path = usersFilePath();
    nlohmann::json users = readUsersFile(path);

    const std::string salt = generateSalt();
    nlohmann::json entry;
    entry["username"] = username;
    entry["password_hash"] = hashPassword(salt, password);
    entry["salt"] = salt;
    entry["created_at"] = timestampNow();
    entry["last_login"] = timestampNow();

    users.push_back(entry);

    return writeUsersFileAtomic(path, users);
}

bool Auth::loginUser(const std::string& username, const std::string& password) {
    const std::string path = usersFilePath();
    nlohmann::json j = readUsersFile(path);

    for (auto& entry : j) {
        if (!entry.is_object() || !entry.contains("username") || !entry["username"].is_string()) continue;
        if (entry["username"].get<std::string>() != username) continue;

        if (!entry.contains("salt") || !entry["salt"].is_string()) return false;
        if (!entry.contains("password_hash") || !entry["password_hash"].is_string()) return false;

        const std::string salt = entry["salt"].get<std::string>();
        const std::string expectedHash = entry["password_hash"].get<std::string>();
        const std::string actualHash = hashPassword(salt, password);
        if (actualHash == expectedHash) {
            entry["last_login"] = timestampNow();
            writeUsersFileAtomic(path, j);
            return true;
        }
        return false;
    }
    return false;
}

bool Auth::changePassword(const std::string& username, const std::string& currentPassword, const std::string& newPassword) {
    if (username.empty() || currentPassword.empty() || newPassword.size() < 6 || newPassword.size() > 64) return false;

    const std::string path = usersFilePath();
    nlohmann::json j = readUsersFile(path);

    bool updated = false;
    for (auto& entry : j) {
        if (!entry.is_object() || !entry.contains("username") || !entry["username"].is_string()) continue;
        if (entry["username"].get<std::string>() != username) continue;

        if (!entry.contains("salt") || !entry["salt"].is_string() || !entry.contains("password_hash") || !entry["password_hash"].is_string()) {
            return false;
        }

        const std::string salt = entry["salt"].get<std::string>();
        const std::string expectedHash = entry["password_hash"].get<std::string>();
        if (hashPassword(salt, currentPassword) != expectedHash) return false;

        const std::string newSalt = generateSalt();
        entry["salt"] = newSalt;
        entry["password_hash"] = hashPassword(newSalt, newPassword);
        entry["last_login"] = timestampNow();
        updated = true;
        break;
    }

    if (!updated) return false;

    return writeUsersFileAtomic(path, j);
}

void Auth::updateLastLogin(const std::string& username) {
    const std::string path = usersFilePath();
    nlohmann::json j = readUsersFile(path);

    for (auto& entry : j) {
        if (entry.is_object() && entry.contains("username") && entry["username"].is_string() &&
            entry["username"].get<std::string>() == username) {
            entry["last_login"] = timestampNow();
            writeUsersFileAtomic(path, j);
            break;
        }
    }
}
