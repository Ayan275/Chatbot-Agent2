/*
 * AuditLogger.h — Decision Audit Trail for the Self-Accountable AI system.
 *
 * Every user↔AI exchange is recorded as a structured AuditRecord and
 * appended as a single JSON line to audit_logs/user_audit.jsonl.
 *
 * Key design choices:
 *  - JSONL (newline-delimited JSON) lets every record be parsed independently;
 *    a partial write at the end does not corrupt earlier records.
 *  - A monotonically-increasing "msg_XXXX" ID is assigned per session,
 *    resuming from the highest ID found in the file at startup.
 *  - Reads are done by linear scan (files are small; no index needed).
 *  - All I/O is guarded so a disk failure never crashes the chatbot.
 */

#ifndef AUDIT_LOGGER_H
#define AUDIT_LOGGER_H

#include <string>
#include <cstdint>
#include <atomic>
#include <nlohmann/json.hpp>

// ---------------------------------------------------------------------------
// AuditRecord — one complete record for a single AI turn
// ---------------------------------------------------------------------------
struct AuditRecord {
    std::string id;               // Sequential identifier: "msg_0001"
    std::string timestamp;        // ISO-8601 local time: "2026-06-30 22:49:56"
    std::string userName;         // Active session user
    std::string query;            // Sanitized user message that reached the model
    std::string response;         // Final AI reply shown to the user
    std::string modelName;        // Model used, e.g. "openai/gpt-4o-mini"
    double      responseTimeMs;   // Wall-clock milliseconds for getReply()
    double      confidenceScore;  // Heuristic confidence [0.0 – 1.0]
    double      trustScore;       // Snapshot of TrustTracker score at log time
    int         promptTokens;     // token usage counters from OpenRouter
    int         completionTokens; // token usage counters from OpenRouter
    int         totalTokens;      // token usage counters from OpenRouter
    std::string feedbackStatus;   // "pending" | "correct" | "wrong"
    bool        corrected;        // true once /feedback wrong has been applied
    std::string severity;         // "low" | "medium" | "high" | "" (empty if unset)
    std::string prev_hash;        // SHA-256 of the previous record
    std::string hash;             // SHA-256 of this record

    // Default-initialise to safe values so partial construction is safe
    AuditRecord()
        : responseTimeMs(0.0), confidenceScore(0.0), trustScore(100.0),
          promptTokens(0), completionTokens(0), totalTokens(0),
          feedbackStatus("pending"), corrected(false),
          severity(""), prev_hash(""), hash("") {}
};

// ---------------------------------------------------------------------------
// AuditLogger — writes, reads, and manages the JSONL audit log
// ---------------------------------------------------------------------------
class AuditLogger {
public:
    /*
     * Construct an AuditLogger.
     *
     * @param logFilePath  Full path to the JSONL file (created if absent).
     * @param userName     Name of the current session user.
     * @param retentionDays Max age in days for audit records (prunes older on startup).
     */
    explicit AuditLogger(const std::string& logFilePath,
                         const std::string& userName,
                         int retentionDays = 90);

    // Non-copyable; move is fine (std::atomic is not copyable)
    AuditLogger(const AuditLogger&)            = delete;
    AuditLogger& operator=(const AuditLogger&) = delete;

    /*
     * Validates the integrity of the hash chain in the entire log file.
     * Prints the result to stdout. Returns true if valid, false if tampered.
     */
    bool verifyChain() const;

    /*
     * Append one AuditRecord to the JSONL file.
     * The record's id and timestamp fields are set here if they are empty.
     * Returns the assigned message ID (e.g. "msg_0042").
     */
    std::string writeRecord(AuditRecord& record);

    /*
     * Update the feedbackStatus, corrected flag, and severity of an existing record.
     * Rewrites the entire JSONL file to retroactively recompute all subsequent hashes.
     * Returns false if the id was not found or the file could not be updated.
     */
    bool updateFeedback(const std::string& id,
                        const std::string& feedbackStatus,
                        bool corrected,
                        const std::string& severity = "");

    /*
     * Read the last record appended to the log.
     * Returns a default-constructed AuditRecord (with empty id) if the log
     * is empty or unreadable.
     */
    AuditRecord readLast() const;

    /*
     * Read a specific record by its message ID (e.g. "msg_0042").
     * Returns a default-constructed AuditRecord (with empty id) if not found.
     */
    AuditRecord readById(const std::string& id) const;

    /*
     * Read all records from the log.
     */
    std::vector<AuditRecord> readAll() const;

    /*
     * Format a list of records into a short summary table (newest first).
     */
    static std::string formatRecordList(const std::vector<AuditRecord>& records, int limit = -1);

    /*
     * The message ID of the most recently written record.
     * Empty string if no record has been written this session.
     */
    std::string lastId() const;

private:
    std::string            logPath_;   // Full path to the .jsonl file
    std::string            userName_;  // Current user (stored in each record)
    std::atomic<uint64_t>  counter_;   // Monotonic msg counter (thread-safe)
    std::string            lastHash_;  // Hash of the last written/verified record

    // Generate the next sequential ID string, e.g. "msg_0043"
    std::string nextId();

    // Scan the existing JSONL file, returning highest ID and finding lastHash_.
    uint64_t loadState();

    // Prune records older than `maxDays` from the JSONL file.
    void pruneOldRecords(int maxDays);

    // Build the current local timestamp string "YYYY-MM-DD HH:MM:SS"
    static std::string currentTimestamp();
    
    // Hash a JSON record (excluding its "hash" field).
    static std::string computeRecordHash(const nlohmann::json& j);
};

#endif // AUDIT_LOGGER_H
