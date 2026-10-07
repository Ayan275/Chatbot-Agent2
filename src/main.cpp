/*
 * main.cpp — Chatbot entry point.
 *
 * Supports /help, /clear, /history, /export, /exit commands (original).
 *
 * Self-Accountable AI extensions (2026):
 *   /trust            — Display current AI reliability score
 *   /feedback correct — Confirm last answer was correct  (+2.5% trust)
 *   /feedback wrong   — Report last answer was wrong     (-7.0% trust)
 *   /wrong            — Alias for /feedback wrong
 *   /wrong <severity> — Mark wrong with severity (low|medium|high)
 *   /audit last       — Show latest audit record
 *   /audit <id>       — Show specific audit record by message ID
 *   /audit verify     — Verify the tamper-evident hash chain
 *
 * Agent mode (WhatsApp front-end, 2026):
 *   chatbot --agent <username>   skips the interactive login for an EXISTING user and prints
 *   "<<READY>>" instead of the "You: " prompt, so a parent process can drive the full
 *   chatbot (all commands, trust, audit, self-correction) over stdin/stdout.
 *
 * Architecture:
 *   main() owns unique_ptr<AuditLogger> and unique_ptr<TrustTracker>.
 *   Non-owning raw pointers are injected into the Chatbot via
 *   setAuditComponents(), matching the existing memory ownership pattern.
 */

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <cctype>
#include <chrono>
#include <ctime>
#include <iomanip>
#include <memory>
#include <vector>
#include <filesystem>
#include <functional>
#include <unordered_map>
#include <nlohmann/json.hpp>
#include "logger.h"
#include "user.h"
#include "chatbot.h"
#include "memory.h"
#include "AuditLogger.h"
#include "TrustTracker.h"
#include "auth.h"

namespace {

struct Config {
    std::string model    = "gemini-3.5-flash-lite";
    int maxTurns         = 1000, maxHistoryMessages = 10, maxStringHistory = 20;
    int maxMessageLength = 2000, maxNameLength = 50;
    size_t maxFileBytes  = 1048576;
    int auditRetentionDays = 90;
};

Config loadConfig(const std::string& path = "config.json") {
    Config cfg;
    const std::vector<std::filesystem::path> candidates = {
        std::filesystem::path(path),
        std::filesystem::path("../") / path
    };

    std::filesystem::path usedPath;
    for (const auto& candidate : candidates) {
        if (std::filesystem::exists(candidate) && std::filesystem::is_regular_file(candidate)) {
            usedPath = candidate;
            break;
        }
    }

    if (usedPath.empty()) {
        std::cout << "[System Warning]: 'config.json' not found or corrupted. Initializing chatbot with default system fail-safes.\n\n";
        Logger::log("WARN", "config.json not found in current working directory or parent directory. Using defaults.");
        return cfg;
    }

    try {
        std::ifstream f(usedPath);
        if (!f.is_open()) {
            std::cout << "[System Warning]: 'config.json' not found or corrupted. Initializing chatbot with default system fail-safes.\n\n";
            Logger::log("WARN", "Could not open config file: " + usedPath.string() + ". Using defaults.");
            return cfg;
        }

        nlohmann::json j;
        f >> j;
        auto get = [&](const char* k, auto& v) {
            if (j.contains(k) && j[k].is_number_integer()) v = j[k];
        };
        if (j.contains("model") && j["model"].is_string()) cfg.model = j["model"];
        get("maxTurns",           cfg.maxTurns);
        get("maxHistoryMessages", cfg.maxHistoryMessages);
        get("maxStringHistory",   cfg.maxStringHistory);
        get("maxMessageLength",   cfg.maxMessageLength);
        get("maxNameLength",      cfg.maxNameLength);
        get("auditRetentionDays", cfg.auditRetentionDays);
        if (j.contains("maxFileBytes") && j["maxFileBytes"].is_number_integer())
            cfg.maxFileBytes = j["maxFileBytes"].get<size_t>();
        Logger::log("INFO", "Config loaded from " + usedPath.string());
    } catch (const nlohmann::json::parse_error& e) {
        std::cout << "[System Warning]: 'config.json' not found or corrupted. Initializing chatbot with default system fail-safes.\n\n";
        Logger::log("WARN", std::string("Config JSON parse failed in ") + usedPath.string() + ": " + e.what() + ". Using defaults.");
    } catch (const std::exception& e) {
        std::cout << "[System Warning]: 'config.json' not found or corrupted. Initializing chatbot with default system fail-safes.\n\n";
        Logger::log("WARN", std::string("Config load failed for ") + usedPath.string() + ": " + e.what() + ". Using defaults.");
    }
    return cfg;
}

auto sanitize(std::string_view in, int maxLen, bool nameMode) {
    std::string s(in.substr(0, static_cast<size_t>(maxLen))), out;
    out.reserve(s.size());
    for (unsigned char c : s) {
        bool ok = nameMode ? (std::isalnum(c) || c == ' ' || c == '-')
                           : (c == '\t' || c == '\n' || c == '\r' || c >= 32);
        if (ok) out += static_cast<char>(c);
    }
    auto f = out.find_first_not_of(" \t\n\r"), l = out.find_last_not_of(" \t\n\r");
    return (f == std::string::npos) ? std::string{} : out.substr(f, l - f + 1);
}

std::string timestamp(const char* fmt = "%Y%m%d_%H%M%S") {
    auto t = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
    std::tm buf{};
#ifdef _WIN32
    localtime_s(&buf, &t);
#else
    localtime_r(&t, &buf);
#endif
    std::ostringstream ss; ss << std::put_time(&buf, fmt); return ss.str();
}

void exportSession(const std::string& userName, const Chatbot& bot) {
    std::string fname = "export_" + userName + "_" + timestamp() + ".md";
    std::ofstream out(fname);
    if (!out.is_open()) { std::cout << "Error: could not create export file.\n"; return; }
    out << "# Chat Session — " << userName << "\n\n_Exported: " << timestamp("%Y-%m-%d %H:%M:%S") << "_\n\n---\n\n";
    for (const auto& line : bot.getStringHistory())
        out << (line.rfind("User:", 0) == 0 ? "**" + line + "**" : line) << "\n\n";
    std::cout << "Session exported to: " << fname << "\n";
    Logger::log("INFO", "Session exported");
}

void showCorrections() {
    std::filesystem::path path = "corrections.json";
    if (!std::filesystem::exists(path)) {
        path = std::filesystem::path("..") / "corrections.json";
    }
    if (!std::filesystem::exists(path)) {
        std::cout << "No active corrections.\n";
        return;
    }

    std::ifstream in(path);
    if (!in.is_open()) {
        std::cout << "No active corrections.\n";
        return;
    }

    try {
        nlohmann::json j;
        in >> j;
        if (j.empty()) {
            std::cout << "No active corrections.\n";
            return;
        }
        std::cout << "\n========== ACTIVE CONSTRAINTS ==========\n";
        int idx = 1;
        for (auto& [query, value] : j.items()) {
            std::cout << idx++ << ". Query: " << query << "\n";
            if (value.is_string()) {
                std::cout << "   Timestamp: N/A\n"
                          << "   Severity:  medium\n"
                          << "   Constraint: [Legacy format]\n";
            } else if (value.is_object()) {
                std::cout << "   Timestamp: " << value.value("timestamp", "N/A") << "\n"
                          << "   Severity:  " << value.value("severity", "medium") << "\n";
            }
            std::cout << "---------------------------------------\n";
        }
    } catch (...) {
        std::cout << "No active corrections (failed to parse corrections.json).\n";
    }
}

// ---------------------------------------------------------------------------
// Help text — updated to include Self-Accountable AI commands
// ---------------------------------------------------------------------------
constexpr std::string_view HELP_TEXT =
    "\n========== CHATBOT COMMANDS ==========\n"
    "  /help              - Show this help menu\n"
    "  /clear             - Clear conversation history\n"
    "  /history           - Show saved session history\n"
    "  /export            - Export session as Markdown file\n"
    "  /exit              - Quit the chatbot\n"
    "\n"
    "  --- Self-Accountable AI ---\n"
    "  /trust             - Show AI reliability score\n"
    "  /changepassword    - Change your current password\n"
    "  /feedback correct  - Confirm last answer was correct\n"
    "  /feedback wrong    - Report last answer was wrong\n"
    "  /wrong             - Alias: mark last answer as wrong\n"
    "  /wrong <severity>  - Mark wrong with severity (low|medium|high)\n"
    "  /corrections       - Show active correction constraints\n"
    "  /audit last        - Show the latest audit record\n"
    "  /audit <id>        - Show audit record by ID (e.g. msg_0001)\n"
    "  /audit list [N]    - List all (or last N) audit records\n"
    "  /audit verify      - Verify the tamper-evident hash chain\n"
    "======================================\n\n";

} // namespace

#ifdef _WIN32
#include <windows.h>
#endif

int main(int argc, char** argv) {
#ifdef _WIN32
    SetConsoleOutputCP(CP_UTF8); SetConsoleCP(CP_UTF8);
#endif
    bool agentMode = false;
    std::string agentUser;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--agent" && i + 1 < argc) { agentMode = true; agentUser = argv[++i]; }
    }
    try {
        auto cfg    = loadConfig();
        auto user   = std::make_unique<User>();
        auto bot    = std::make_unique<Chatbot>(cfg.model, cfg.maxHistoryMessages, cfg.maxStringHistory);
        auto memory = std::make_unique<Memory>("temp", cfg.maxFileBytes);
        Auth auth;

        std::string name;
        if (agentMode) {
            // Trusted local front-end (WhatsApp agent): provision missing profile without prompting.
            agentUser = sanitize(agentUser, cfg.maxNameLength, true);
            if (agentUser.empty() || !auth.ensureAgentUser(agentUser)) {
                std::cerr << "Agent mode: unable to initialize user profile.\n";
                return 1;
            }
            name = agentUser;
            auto safeName = sanitizePathToken(name);
            if (!safeName.empty()) {
                user->setName(safeName);
                memory = std::make_unique<Memory>(safeName, cfg.maxFileBytes);
            }
            std::cout << "Agent mode: logged in as " << user->getName() << "\n";
        }
        while (!agentMode) {
            std::cout << "==============================\n"
                      << "  AI Chatbot\n"
                      << "==============================\n"
                      << "[1] Login\n"
                      << "[2] Register\n"
                      << "[3] Exit\n"
                      << "Enter choice: ";
            std::string choice;
            if (!std::getline(std::cin, choice)) break;
            choice.erase(0, choice.find_first_not_of(" \t\n\r"));
            if (auto p = choice.find_last_not_of(" \t\n\r"); p != std::string::npos) choice.erase(p + 1);

            if (choice == "1") {
                while (true) {
                    std::cout << "Username: ";
                    std::string username;
                    if (!std::getline(std::cin, username)) return 1;
                    username = sanitize(username, cfg.maxNameLength, true);
                    if (username.empty()) {
                        std::cout << "Invalid username. Use letters, numbers, or hyphens.\n";
                        continue;
                    }

                    std::string password = getPasswordInput("Password: ");
                    if (auth.loginUser(username, password)) {
                        name = username;
                        auto safeName = sanitizePathToken(name);
                        if (!safeName.empty()) {
                            user->setName(safeName);
                            memory = std::make_unique<Memory>(safeName, cfg.maxFileBytes);
                        }
                        std::cout << "Login successful. Welcome back, " << user->getName() << "!\n";
                        break;
                    }
                    std::cout << "Incorrect username or password. Please try again.\n";
                }
                break;
            }

            if (choice == "2") {
                while (true) {
                    std::cout << "Choose username: ";
                    std::string username;
                    if (!std::getline(std::cin, username)) return 1;
                    username = sanitize(username, cfg.maxNameLength, true);
                    if (username.empty()) {
                        std::cout << "Invalid username. Use letters, numbers, or hyphens.\n";
                        continue;
                    }
                    if (auth.userExists(username)) {
                        std::cout << "Username already taken. Try another.\n";
                        continue;
                    }

                    std::string password;
                    while (true) {
                        password = getPasswordInput("Password: ");
                        if (password.size() < 6 || password.size() > 64) {
                            std::cout << "Password must be 6-64 characters long.\n";
                            continue;
                        }
                        std::string confirm = getPasswordInput("Confirm password: ");
                        if (password == confirm) break;
                        std::cout << "Passwords do not match. Try again.\n";
                    }

                    if (auth.registerUser(username, password)) {
                        name = username;
                        auto safeName = sanitizePathToken(name);
                        if (!safeName.empty()) {
                            user->setName(safeName);
                            memory = std::make_unique<Memory>(safeName, cfg.maxFileBytes);
                        }
                        std::cout << "Account created successfully. You are now logged in.\n";
                        break;
                    }
                    std::cout << "Registration failed. Please try again.\n";
                }
                break;
            }

            if (choice == "3") {
                std::cout << "Goodbye.\n";
                return 0;
            }

            std::cout << "Invalid choice. Please enter 1, 2, or 3.\n";
        }

        if (name.empty()) {
            std::cout << "No valid username provided. Exiting.\n";
            return 1;
        }

        // -----------------------------------------------------------------------
        // Self-Accountable AI: instantiate audit subsystems
        // Audit log path: audit_logs/user_audit.jsonl (in the working directory)
        // Trust state:    audit_logs/<user>_trust.json
        // -----------------------------------------------------------------------
        auto auditLogger  = std::make_unique<AuditLogger>((std::filesystem::path("audit_logs") / "user_audit.jsonl").string(), name, cfg.auditRetentionDays);
        auto trustTracker = std::make_unique<TrustTracker>(name);

        // Inject non-owning pointers into the Chatbot
        bot->setAuditComponents(auditLogger.get(), trustTracker.get());

        Logger::log("INFO", "Session started user=" + name);
        std::cout << "\n==============================\n  Welcome, " << user->getName()
                  << "!\n  Type /help for commands.\n==============================\n\n";

        std::string msg;
        int turnCount = 0;

        // -----------------------------------------------------------------------
        // Command dispatch table — original + Self-Accountable AI commands
        // -----------------------------------------------------------------------
        std::unordered_map<std::string, std::function<void()>> commands = {

            // --- Original commands (unchanged) ---
            {"/help",    [&]{ std::cout << HELP_TEXT; }},
            {"/clear",   [&]{ bot->clearHistory(); memory->clearHistoryFile();
                              std::cout << "Conversation history cleared.\n";
                              turnCount = 0; Logger::log("INFO", "History cleared"); }},
            {"/history", [&]{ memory->showHistory(); }},
            {"/export",  [&]{ exportSession(user->getName(), *bot); }},

            // --- Self-Accountable AI: trust commands ---
            {"/trust",            [&]{ std::cout << trustTracker->getSummary(); }},
            {"/corrections",      [&]{ showCorrections(); }},
            {"/changepassword",   [&]{
                std::cout << "Current password: ";
                std::string currentPassword = getPasswordInput();
                std::cout << "New password: ";
                std::string newPassword = getPasswordInput();
                std::cout << "Confirm new password: ";
                std::string confirmPassword = getPasswordInput();
                if (newPassword.size() < 6 || newPassword.size() > 64) {
                    std::cout << "Password must be 6-64 characters long.\n";
                } else if (newPassword != confirmPassword) {
                    std::cout << "Passwords do not match. Try again.\n";
                } else if (auth.changePassword(name, currentPassword, newPassword)) {
                    std::cout << "Password changed successfully.\n";
                } else {
                    std::cout << "Failed to change password. Please check your current password.\n";
                }
            }},
            {"/feedback correct", [&]{ bot->applyFeedback(true); }},
            {"/feedback wrong",   [&]{ bot->applyFeedback(false); }},
            {"/wrong",            [&]{ bot->applyFeedback(false); }},  // convenience alias

            // --- Self-Accountable AI: audit commands ---
            {"/audit last",       [&]{ std::cout << bot->getAuditEntry("last"); }},
            {"/audit verify",     [&]{ bot->verifyAuditChain(); }},
        };

        while (true) {
            if (agentMode) std::cout << "\n<<READY>>\n" << std::flush;
            else std::cout << "You: ";
            if (!std::getline(std::cin, msg)) break;
            msg.erase(0, msg.find_first_not_of(" \t\n\r"));
            if (auto p = msg.find_last_not_of(" \t\n\r"); p != std::string::npos) msg.erase(p + 1);
            if (msg.empty()) continue;
            if (msg == "/exit" || msg == "exit") break;

            // -----------------------------------------------------------------
            // Dynamic /audit <id> parsing — not in the dispatch table because
            // the ID argument is variable. Handled before the table lookup.
            // -----------------------------------------------------------------
            if (msg.rfind("/audit ", 0) == 0) {
                std::string auditArg = msg.substr(7); // everything after "/audit "
                // trim whitespace
                auto af = auditArg.find_first_not_of(" \t");
                auto al = auditArg.find_last_not_of(" \t");
                auditArg = (af == std::string::npos) ? "" : auditArg.substr(af, al - af + 1);
                if (auditArg.empty()) {
                    std::cout << "  Usage: /audit last   OR   /audit list [N]   OR   /audit <message_id>   OR   /audit verify\n";
                } else if (auditArg == "verify") {
                    bot->verifyAuditChain();
                } else if (auditArg.rfind("list", 0) == 0) {
                    int limit = -1;
                    if (auditArg.length() > 4) {
                        try { limit = std::stoi(auditArg.substr(4)); } catch (...) {}
                    }
                    std::cout << bot->getAuditList(limit);
                } else {
                    std::cout << bot->getAuditEntry(auditArg);
                }
                continue;
            }
            
            // -----------------------------------------------------------------
            // Dynamic /wrong <severity> parsing
            // -----------------------------------------------------------------
            if (msg.rfind("/wrong ", 0) == 0) {
                std::string sev = msg.substr(7);
                auto af = sev.find_first_not_of(" \t");
                auto al = sev.find_last_not_of(" \t");
                sev = (af == std::string::npos) ? "" : sev.substr(af, al - af + 1);
                if (sev == "low" || sev == "medium" || sev == "high") {
                    bot->applyFeedback(false, sev);
                } else {
                    std::cout << "  [Error] Invalid severity. Use: /wrong low | /wrong medium | /wrong high\n";
                }
                continue;
            }

            // Look up fixed-string commands
            if (auto it = commands.find(msg); it != commands.end()) { it->second(); continue; }

            // Regular message — sanitize and send
            msg = sanitize(msg, cfg.maxMessageLength, false);
            if (msg.empty()) { std::cout << "Message was empty after filtering. Please try again.\n"; continue; }

            if (turnCount >= cfg.maxTurns) { std::cout << "Session turn limit reached. Use /exit to close.\n"; continue; }

            std::cout << "\n[Turn " << ++turnCount << "]\n";
            try {
                memory->saveChat("User: " + msg);

                // Use getReplyWithAudit() — includes transparency footer
                auto reply = bot->getReplyWithAudit(msg);
                std::cout << "Bot: " << reply << "\n";
                std::cout.flush();

                // Save plain reply to memory (strip transparency footer for file storage)
                // The footer begins with "\n  ┌" — find and exclude it from the saved text
                std::string plainReply = reply;
                auto footerPos = plainReply.find("\n  \xe2\x94\x8c"); // UTF-8 for ┌
                if (footerPos != std::string::npos) {
                    plainReply = plainReply.substr(0, footerPos);
                }
                memory->saveChat("Bot: " + plainReply);

                Logger::log("INFO", "Turn " + std::to_string(turnCount));
            } catch (const std::exception& e) {
                Logger::log("ERROR", std::string("Turn exception: ") + e.what());
                std::cout << "Something went wrong. Please try again.\n";
            }
        }

        Logger::log("INFO", "Session ended user=" + name + " turns=" + std::to_string(turnCount));
        std::cout << "\n==============================\n  Goodbye, " << user->getName() << "!\n==============================\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "Fatal error: An unexpected system failure occurred. Please contact the administrator.\n";
        Logger::log("ERROR", std::string("Fatal: ") + e.what());
        return 1;
    }
}
