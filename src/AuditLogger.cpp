/*
 * AuditLogger.cpp — Implementation of the Decision Audit Trail with Hash Chain.
 *
 * Atomicity guarantee:
 *   All writes (writeRecord, updateFeedback, pruneOldRecords) follow the
 *   write-to-temp-then-rename pattern.  std::filesystem::rename() is atomic
 *   on the same filesystem (POSIX rename(2); Windows ReplaceFileA).  This
 *   means a crash mid-write leaves either the old file or the complete new
 *   file intact — never a partial update.
 *
 * lastHash_ safety:
 *   lastHash_ is only updated in memory AFTER a successful atomic rename.
 *   If the rename fails, lastHash_ is left unchanged so the next writeRecord
 *   can retry with a consistent chain state.
 *
 * Error handling:
 *   - All file I/O is wrapped in try/catch.
 *   - A failed audit write never crashes the main chat flow; failures are
 *     loudly logged via Logger::log("ERROR", ...) but not re-thrown.
 *   - Internal paths and raw exception messages are never shown to the end
 *     user; only clean, generic messages are forwarded to chat output.
 */

#include "AuditLogger.h"
#include "logger.h"
#include "sha256.h"
#include <nlohmann/json.hpp>
#include <fstream>
#include <sstream>
#include <iomanip>
#include <filesystem>
#include <iostream>
#include <chrono>
#include <ctime>
#include <system_error>

namespace fs = std::filesystem;
using json   = nlohmann::json;

static const std::string GENESIS_HASH(64, '0');

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

AuditLogger::AuditLogger(const std::string& logFilePath,
                         const std::string& userName,
                         int retentionDays)
    : logPath_(logFilePath), userName_(userName), counter_(0), lastHash_(GENESIS_HASH)
{
    // Ensure the parent directory exists
    try {
        fs::path dir = fs::path(logFilePath).parent_path();
        if (!dir.empty() && !fs::exists(dir)) {
            fs::create_directories(dir);
            Logger::log("INFO", "AuditLogger: created directory " + dir.string());
        }
    } catch (const fs::filesystem_error& e) {
        Logger::log("WARN", std::string("AuditLogger: filesystem error creating log directory: ") + e.what());
    } catch (const std::exception& e) {
        Logger::log("WARN", std::string("AuditLogger: could not create log directory: ") + e.what());
    }

    if (retentionDays > 0) {
        pruneOldRecords(retentionDays);
    }

    // loadState() reads the existing log to restore counter_ and lastHash_.
    // Wrap in try/catch so a corrupt log at startup never crashes the app.
    try {
        counter_.store(loadState());
    } catch (const std::exception& e) {
        Logger::log("WARN", std::string("AuditLogger: loadState failed, starting fresh: ") + e.what());
        counter_.store(0);
        lastHash_ = GENESIS_HASH;
    }

    Logger::log("INFO", "AuditLogger: initialised, next id=" + std::to_string(counter_.load() + 1));
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

std::string AuditLogger::nextId() {
    uint64_t n = ++counter_;
    std::ostringstream ss;
    ss << "msg_" << std::setw(4) << std::setfill('0') << n;
    return ss.str();
}

uint64_t AuditLogger::loadState() {
    std::ifstream f(logPath_);
    if (!f.is_open()) return 0;

    uint64_t highest = 0;
    std::string line;
    std::string lastH = GENESIS_HASH;

    while (std::getline(f, line)) {
        if (line.empty()) continue;
        try {
            auto j = json::parse(line);
            if (j.contains("id") && j["id"].is_string()) {
                std::string id = j["id"].get<std::string>();
                if (id.rfind("msg_", 0) == 0) {
                    uint64_t num = std::stoull(id.substr(4));
                    if (num > highest) highest = num;
                }
            }
            if (j.contains("hash") && j["hash"].is_string()) {
                lastH = j["hash"].get<std::string>();
            }
        } catch (...) {
            // Skip malformed lines during startup — verifyChain() will catch them
        }
    }
    lastHash_ = lastH;
    return highest;
}

std::string AuditLogger::currentTimestamp() {
    auto t = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
    std::tm buf{};
#ifdef _WIN32
    localtime_s(&buf, &t);
#else
    localtime_r(&t, &buf);
#endif
    std::ostringstream ss;
    ss << std::put_time(&buf, "%Y-%m-%d %H:%M:%S");
    return ss.str();
}

std::string AuditLogger::computeRecordHash(const json& j) {
    json copy = j;
    copy.erase("hash"); // Exclude the hash field from its own digest
    return SHA256::hashString(copy.dump());
}

// ---------------------------------------------------------------------------
// Retention Policy
// ---------------------------------------------------------------------------

void AuditLogger::pruneOldRecords(int maxDays) {
    std::ifstream in(logPath_);
    if (!in.is_open()) return;

    auto now = std::chrono::system_clock::now();
    std::vector<std::string> linesToKeep;
    bool prunedAny = false;

    std::string line;
    while (std::getline(in, line)) {
        if (line.empty()) continue;
        try {
            auto j = json::parse(line);
            if (j.contains("timestamp") && j["timestamp"].is_string()) {
                std::string tsStr = j["timestamp"].get<std::string>();
                std::tm tm = {};
                std::istringstream ss(tsStr);
                ss >> std::get_time(&tm, "%Y-%m-%d %H:%M:%S");
                if (!ss.fail()) {
                    auto timePt = std::chrono::system_clock::from_time_t(std::mktime(&tm));
                    auto ageDays = std::chrono::duration_cast<std::chrono::hours>(now - timePt).count() / 24;
                    if (ageDays > maxDays) {
                        prunedAny = true;
                        continue;
                    }
                }
            }
        } catch (...) {}
        linesToKeep.push_back(line);
    }
    in.close();

    if (prunedAny) {
        std::string tmpPath = logPath_ + ".tmp";
        try {
            std::ofstream out(tmpPath);
            if (out.is_open()) {
                for (const auto& l : linesToKeep) out << l << "\n";
                out.close();
                std::error_code ec;
                fs::rename(tmpPath, logPath_, ec);
                if (ec) {
                    std::error_code removeEc;
                    fs::remove(tmpPath, removeEc);
                    Logger::log("ERROR", "AuditLogger: pruneOldRecords rename failed: " + ec.message());
                } else {
                    Logger::log("INFO", "AuditLogger: pruned records older than " +
                                std::to_string(maxDays) + " days.");
                }
            }
        } catch (const fs::filesystem_error& e) {
            Logger::log("ERROR", std::string("AuditLogger: pruneOldRecords filesystem error: ") + e.what());
            std::error_code ec;
            fs::remove(tmpPath, ec);
        } catch (const std::exception& e) {
            Logger::log("ERROR", std::string("AuditLogger: pruneOldRecords failed: ") + e.what());
            std::error_code ec;
            fs::remove(tmpPath, ec);
        }
    }
}

// ---------------------------------------------------------------------------
// Hash Chain Verification
//
// Edge cases handled:
//   - Missing file        → "No log file found" (returns true — nothing to verify)
//   - Empty file (0 bytes) → count==0 → "valid, no entries" (returns true)
//   - Single-entry file  → prev_hash must equal GENESIS_HASH (verified correctly)
//   - Malformed JSON line → reports exact line number as "malformed" (returns false)
//   - Truncated last line → detected by checking if the file ended without '\n'
//                           on a line that fails json::parse (reported distinctly)
//   - Tampered hash      → hash recomputation mismatch reported with line + id
//   - Broken chain link  → prev_hash mismatch reported with line + id
// ---------------------------------------------------------------------------

bool AuditLogger::verifyChain() const {
    std::error_code ec;
    if (!fs::exists(logPath_, ec)) {
        std::cout << "  [Audit] No log file found to verify.\n";
        return true;
    }

    std::ifstream in(logPath_);
    if (!in.is_open()) {
        std::cout << "  [!] ERROR: Could not open audit log for verification.\n";
        return false;
    }

    std::cout << "\n  [Audit] Verifying tamper-evident hash chain...\n";
    std::string expectedPrev = GENESIS_HASH;
    bool firstRecord = true;
    std::string line;
    int count = 0;
    int lineNumber = 0;

    while (std::getline(in, line)) {
        ++lineNumber;

        if (line.empty()) continue;

        // --------------- Parse the JSON line ---------------
        json j;
        try {
            j = json::parse(line);
        } catch (const json::parse_error& e) {
            // Check if this is likely a truncated last line by seeing if we are
            // at EOF now (stream exhausted right after this failed getline).
            bool atEof = in.eof() || in.peek() == std::ifstream::traits_type::eof();
            if (atEof) {
                std::cout << "  [!] WARNING: Possible truncation detected — last line ("
                          << lineNumber << ") is incomplete or corrupt.\n"
                          << "              This may indicate a crash during write, not deliberate tampering.\n"
                          << "              Details: " << e.what() << "\n\n";
            } else {
                std::cout << "  [!] ERROR: Malformed JSON at line " << lineNumber
                          << " (mid-file): " << e.what() << "\n\n";
            }
            return false;
        } catch (const std::exception& e) {
            std::cout << "  [!] ERROR: Unexpected parse error at line " << lineNumber
                      << ": " << e.what() << "\n\n";
            return false;
        }

        if (!j.is_object()) {
            std::cout << "  [!] ERROR: Malformed JSON (not an object) at line " << lineNumber << ".\n\n";
            return false;
        }

        std::string id   = j.value("id", "unknown");
        std::string prev = j.value("prev_hash", "");
        std::string hash = j.value("hash", "");

        // --------------- Verify chain linkage ---------------
        if (firstRecord) {
            // The first record's prev_hash must be the genesis hash (all zeros).
            // If the file was written correctly, this is always the case.
            if (!prev.empty() && prev != GENESIS_HASH) {
                std::cout << "  [!] ERROR: First record at line " << lineNumber
                          << " (" << id << ") has unexpected prev_hash.\n\n";
                return false;
            }
            expectedPrev = GENESIS_HASH;
            firstRecord = false;
        } else {
            if (prev != expectedPrev) {
                std::cout << "  [!] ERROR: Chain broken at line " << lineNumber
                          << " (" << id << ") — prev_hash does not match hash of preceding record.\n\n";
                return false;
            }
        }

        // --------------- Verify canonical serialization ---------------
        // nlohmann::json::dump() produces a canonical (deterministic) JSON string.
        // If the stored line differs from the canonical form of the parsed object,
        // the record was modified after it was written.
        std::string canonical = j.dump();
        if (line != canonical) {
            std::cout << "  [!] ERROR: Tampering detected at line " << lineNumber
                      << " (" << id << ") — stored bytes differ from canonical JSON.\n\n";
            return false;
        }

        // --------------- Verify hash integrity ---------------
        std::string recomputed = computeRecordHash(j);
        if (hash != recomputed) {
            std::cout << "  [!] ERROR: Hash mismatch at line " << lineNumber
                      << " (" << id << ") — record content has been altered.\n\n";
            return false;
        }

        expectedPrev = hash;
        ++count;
    }

    if (count == 0) {
        std::cout << "  [OK] Audit log is valid. No entries found (empty log).\n\n";
        return true;
    }

    std::cout << "  [OK] Hash chain is intact. " << count
              << " record" << (count == 1 ? "" : "s") << " verified successfully.\n\n";
    return true;
}

// ---------------------------------------------------------------------------
// Write
//
// Atomicity: entries are written to a .tmp file then atomically renamed.
// lastHash_ is only updated AFTER a successful rename to ensure the in-memory
// chain state is never ahead of what was actually persisted to disk.
// ---------------------------------------------------------------------------

std::string AuditLogger::writeRecord(AuditRecord& record) {
    if (record.id.empty())        record.id        = nextId();
    if (record.timestamp.empty()) record.timestamp = currentTimestamp();
    if (record.userName.empty())  record.userName  = userName_;

    json j;
    j["id"]                = record.id;
    j["timestamp"]         = record.timestamp;
    j["user"]              = record.userName;
    j["user_query"]        = record.query;
    j["chatbot_response"]  = record.response;
    j["query"]             = record.query;   // backward-compatible key
    j["response"]          = record.response; // backward-compatible key
    j["model"]             = record.modelName;
    j["response_ms"]       = record.responseTimeMs;
    j["confidence"]        = record.confidenceScore;
    j["trust_score"]       = record.trustScore;
    j["prompt_tokens"]     = record.promptTokens;
    j["completion_tokens"] = record.completionTokens;
    j["total_tokens"]      = record.totalTokens;
    j["feedback"]          = record.feedbackStatus;
    j["corrected"]    = record.corrected;
    if (!record.severity.empty()) j["severity"] = record.severity;

    // Capture the prev_hash and compute this record's hash BEFORE touching lastHash_.
    // lastHash_ is only written after the disk rename succeeds.
    std::string capturedPrevHash = lastHash_;
    j["prev_hash"]   = capturedPrevHash;
    std::string h    = computeRecordHash(j);
    j["hash"]        = h;

    // Update the record struct fields so the caller can inspect them
    record.prev_hash = capturedPrevHash;
    record.hash      = h;

    try {
        // Read existing entries
        std::vector<std::string> existing;
        {
            std::ifstream in(logPath_);
            if (in.is_open()) {
                std::string line;
                while (std::getline(in, line)) {
                    if (!line.empty()) existing.push_back(line);
                }
            }
        }
        existing.push_back(j.dump());

        // Write everything to a temp file first
        fs::path tmpPath = fs::path(logPath_).string() + ".tmp";
        {
            std::ofstream out(tmpPath);
            if (!out.is_open()) {
                Logger::log("ERROR", "AuditLogger: could not create temp file for " + record.id +
                            " — audit write skipped, chat continues");
                // lastHash_ is NOT updated — chain state stays consistent
                return record.id;
            }
            for (const auto& l : existing) out << l << "\n";
            if (!out.good()) {
                Logger::log("ERROR", "AuditLogger: temp file write failed for " + record.id);
                std::error_code ec;
                fs::remove(tmpPath, ec);
                return record.id;
            }
        } // out closed here

        // Atomic rename: only update lastHash_ if this succeeds
        std::error_code ec;
        fs::rename(tmpPath, logPath_, ec);
        if (ec) {
            std::error_code removeEc;
            fs::remove(tmpPath, removeEc);
            Logger::log("ERROR", "AuditLogger: atomic rename failed for " + record.id +
                        " — chain state preserved: " + ec.message());
            // lastHash_ deliberately NOT updated: next write will retry with the
            // correct previous hash.
            return record.id;
        }

        // Success — now safe to update in-memory chain state
        lastHash_ = h;

    } catch (const fs::filesystem_error& e) {
        Logger::log("ERROR", "AuditLogger: filesystem error writing " + record.id +
                    " — audit write skipped, chat continues: " + std::string(e.what()));
        return record.id;
    } catch (const std::exception& e) {
        Logger::log("ERROR", "AuditLogger: write failed for " + record.id +
                    " — audit write skipped, chat continues: " + std::string(e.what()));
        return record.id;
    }

    Logger::log("INFO", "AuditLogger: wrote " + record.id);
    return record.id;
}

// ---------------------------------------------------------------------------
// Update feedback on an existing record (rewrites the whole file atomically)
// ---------------------------------------------------------------------------

bool AuditLogger::updateFeedback(const std::string& id,
                                 const std::string& feedbackStatus,
                                 bool corrected,
                                 const std::string& severity) {
    std::vector<json> records;
    bool found = false;
    {
        std::ifstream in(logPath_);
        if (!in.is_open()) return false;
        std::string line;
        while (std::getline(in, line)) {
            if (line.empty()) continue;
            try { records.push_back(json::parse(line)); } catch (...) {}
        }
    }

    // Walk the records, updating the target and rehashing all subsequent entries
    std::string currentPrevHash = GENESIS_HASH;
    if (!records.empty()) currentPrevHash = records[0].value("prev_hash", GENESIS_HASH);

    for (size_t i = 0; i < records.size(); ++i) {
        auto& rec = records[i];
        if (rec.value("id", "") == id) {
            rec["feedback"]  = feedbackStatus;
            rec["corrected"] = corrected;
            if (!severity.empty()) rec["severity"] = severity;
            found = true;
        }

        if (found) {
            // Rehash this record and all that follow it
            rec["prev_hash"] = currentPrevHash;
            std::string h = computeRecordHash(rec);
            rec["hash"] = h;
            currentPrevHash = h;
        } else {
            currentPrevHash = rec.value("hash", "");
        }
    }

    if (!found) return false;

    // Compute the new lastHash_ only after we know the rename will likely succeed
    std::string newLastHash = records.empty() ? GENESIS_HASH : records.back().value("hash", GENESIS_HASH);

    fs::path tmpPath = fs::path(logPath_).string() + ".tmp";
    try {
        {
            std::ofstream out(tmpPath);
            if (!out.is_open()) {
                Logger::log("ERROR", "AuditLogger: updateFeedback could not create temp file for " + id);
                return false;
            }
            for (const auto& rec : records) out << rec.dump() << "\n";
            if (!out.good()) {
                Logger::log("ERROR", "AuditLogger: updateFeedback temp write failed for " + id);
                std::error_code ec;
                fs::remove(tmpPath, ec);
                return false;
            }
        } // out closed here

        std::error_code ec;
        fs::rename(tmpPath, logPath_, ec);
        if (ec) {
            std::error_code removeEc;
            fs::remove(tmpPath, removeEc);
            Logger::log("ERROR", "AuditLogger: updateFeedback atomic rename failed for " + id +
                        ": " + ec.message());
            return false;
        }

        // Rename succeeded — update in-memory lastHash_
        lastHash_ = newLastHash;

    } catch (const fs::filesystem_error& e) {
        Logger::log("ERROR", std::string("AuditLogger: updateFeedback filesystem error for ") +
                    id + ": " + e.what());
        std::error_code ec;
        fs::remove(tmpPath, ec);
        return false;
    } catch (const std::exception& e) {
        Logger::log("ERROR", std::string("AuditLogger: updateFeedback failed for ") +
                    id + ": " + e.what());
        std::error_code ec;
        fs::remove(tmpPath, ec);
        return false;
    }

    Logger::log("INFO", "AuditLogger: feedback updated and chain rehashed for " + id);
    return true;
}

// ---------------------------------------------------------------------------
// Read helpers
// ---------------------------------------------------------------------------

AuditRecord AuditLogger::readLast() const {
    AuditRecord result;
    std::ifstream f(logPath_);
    if (!f.is_open()) return result;

    std::string line, lastValid;
    while (std::getline(f, line)) {
        if (!line.empty()) {
            // Assign to a named variable to satisfy [[nodiscard]] on json::parse.
            // We only need to know if parse succeeded (no exception); the value
            // itself is discarded — we keep the raw line string for later re-parse.
            try { auto parsed = json::parse(line); (void)parsed; lastValid = line; } catch (...) {}
        }
    }
    if (lastValid.empty()) return result;

    try {
        auto j = json::parse(lastValid);
        result.id              = j.value("id",          "");
        result.timestamp       = j.value("timestamp",   "");
        result.userName        = j.value("user",         "");
        result.query           = j.value("user_query",   j.value("query",        ""));
        result.response        = j.value("chatbot_response", j.value("response",     ""));
        result.modelName       = j.value("model",        "");
        result.responseTimeMs  = j.value("response_ms",  0.0);
        result.confidenceScore = j.value("confidence",   0.0);
        result.trustScore      = j.value("trust_score",  100.0);
        result.promptTokens    = j.value("prompt_tokens",     0);
        result.completionTokens= j.value("completion_tokens", 0);
        result.totalTokens     = j.value("total_tokens",      0);
        result.feedbackStatus  = j.value("feedback",     "pending");
        result.corrected       = j.value("corrected",    false);
        result.severity        = j.value("severity",     "");
        result.prev_hash       = j.value("prev_hash",    "");
        result.hash            = j.value("hash",         "");
    } catch (...) {}
    return result;
}

AuditRecord AuditLogger::readById(const std::string& id) const {
    AuditRecord result;
    std::ifstream f(logPath_);
    if (!f.is_open()) return result;

    std::string line;
    while (std::getline(f, line)) {
        if (line.empty()) continue;
        try {
            auto j = json::parse(line);
            if (j.value("id", "") == id) {
                result.id              = j.value("id",          "");
                result.timestamp       = j.value("timestamp",   "");
                result.userName        = j.value("user",         "");
                result.query           = j.value("user_query",   j.value("query",        ""));
                result.response        = j.value("chatbot_response", j.value("response",     ""));
                result.modelName       = j.value("model",        "");
                result.responseTimeMs  = j.value("response_ms",  0.0);
                result.confidenceScore = j.value("confidence",   0.0);
                result.trustScore      = j.value("trust_score",  100.0);
                result.promptTokens    = j.value("prompt_tokens",     0);
                result.completionTokens= j.value("completion_tokens", 0);
                result.totalTokens     = j.value("total_tokens",      0);
                result.feedbackStatus  = j.value("feedback",     "pending");
                result.corrected       = j.value("corrected",    false);
                result.severity        = j.value("severity",     "");
                result.prev_hash       = j.value("prev_hash",    "");
                result.hash            = j.value("hash",         "");
                return result;
            }
        } catch (...) {}
    }
    return result;
}

// ---------------------------------------------------------------------------
// Read all records
// ---------------------------------------------------------------------------

std::vector<AuditRecord> AuditLogger::readAll() const {
    std::vector<AuditRecord> results;
    std::ifstream f(logPath_);
    if (!f.is_open()) return results;

    std::string line;
    while (std::getline(f, line)) {
        if (line.empty()) continue;
        try {
            auto j = json::parse(line);
            AuditRecord r;
            r.id              = j.value("id",          "");
            r.timestamp       = j.value("timestamp",   "");
            r.userName        = j.value("user",         "");
            r.query           = j.value("user_query",   j.value("query",        ""));
            r.response        = j.value("chatbot_response", j.value("response",     ""));
            r.modelName       = j.value("model",        "");
            r.responseTimeMs  = j.value("response_ms",  0.0);
            r.confidenceScore = j.value("confidence",   0.0);
            r.trustScore      = j.value("trust_score",  100.0);
            r.promptTokens    = j.value("prompt_tokens",     0);
            r.completionTokens= j.value("completion_tokens", 0);
            r.totalTokens     = j.value("total_tokens",      0);
            r.feedbackStatus  = j.value("feedback",     "pending");
            r.corrected       = j.value("corrected",    false);
            r.severity        = j.value("severity",     "");
            r.prev_hash       = j.value("prev_hash",    "");
            r.hash            = j.value("hash",         "");
            results.push_back(r);
        } catch (...) {}
    }
    return results;
}

std::string AuditLogger::lastId() const {
    auto r = readLast();
    return r.id;
}

std::string AuditLogger::formatRecordList(const std::vector<AuditRecord>& records, int limit) {
    if (records.empty()) return "  No audit records found yet.\n";

    std::ostringstream ss;
    ss << "\n  [Audit Log — " << records.front().userName << "]\n";
    ss << "  " << std::left << std::setw(22) << "Timestamp"
       << std::setw(12) << "Audit ID"
       << std::setw(8)  << "Trust"
       << std::setw(10) << "Tokens"
       << std::setw(14) << "Status"
       << "Query\n";
    ss << "  " << std::string(100, '-') << "\n";

    int count = 0;
    for (auto it = records.rbegin(); it != records.rend(); ++it) {
        if (limit > 0 && count >= limit) break;

        const auto& r = *it;
        std::string status = (r.trustScore >= 70.0 && r.confidenceScore >= 0.70) ? "Verified" : "Low Conf";
        if (r.corrected) status = "Corrected";
        if (!r.severity.empty()) status += " (" + r.severity + ")";

        std::string shortQ = r.query;
        std::replace(shortQ.begin(), shortQ.end(), '\n', ' ');
        if (shortQ.length() > 40) shortQ = shortQ.substr(0, 37) + "...";

        int trustPct = static_cast<int>(r.trustScore + 0.5);
        std::string trustStr = std::to_string(trustPct) + "%";
        std::string tokenSummary = std::to_string(r.totalTokens);

        ss << "  " << std::left
           << std::setw(22) << r.timestamp
           << std::setw(12) << r.id
           << std::setw(8)  << trustStr
           << std::setw(10) << tokenSummary
           << std::setw(14) << status
           << shortQ << "\n";
        count++;
    }
    ss << "\n";
    return ss.str();
}
