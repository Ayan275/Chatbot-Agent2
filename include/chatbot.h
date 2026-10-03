/*
 * chatbot.h — Conversation engine with Self-Accountable AI integration.
 *
 * Extended to support:
 *   - getReplyWithAudit()  : wraps getReply() with timing, audit logging,
 *                            trust advisory, and transparency footer.
 *   - applyFeedback()      : routes /feedback commands to TrustTracker and
 *                            updates the last audit record accordingly.
 *   - getAuditEntry()      : retrieves a formatted audit record for /audit.
 *   - setAuditComponents() : injects AuditLogger and TrustTracker (non-owning
 *                            pointers; ownership stays in main()).
 */

#ifndef CHATBOT_H
#define CHATBOT_H

#include <string>
#include <vector>
#include <optional>
#include <nlohmann/json.hpp>
#include "AuditLogger.h"
#include "TrustTracker.h"
#include "api.h"

std::optional<std::string> sanitizeInput(const std::string& input);

class Chatbot {
private:
    nlohmann::json           history;
    std::vector<std::string> stringHistory;
    std::string              model;
    size_t                   maxHistory, maxStringHistory;

    // Non-owning pointers to audit subsystems (set via setAuditComponents)
    AuditLogger*  auditLogger_  = nullptr;
    TrustTracker* trustTracker_ = nullptr;

    // The message ID of the most recently sent reply (used by applyFeedback)
    std::string   lastAuditId_;

    /*
     * computeConfidence — heuristic confidence score [0.05, 0.99].
     * Penalizes short/error responses and low trust scores.
     */
    static double computeConfidence(const std::string& response, double trustScore);

    /*
     * buildTransparencyFooter — formats the box-drawing footer shown after
     * every AI reply in the terminal.
     */
    static std::string buildTransparencyFooter(const AuditRecord& rec);

public:
    Chatbot(const std::string& model  = "openai/gpt-4o-mini",
            size_t maxHistory         = 20,
            size_t maxStringHistory   = 40);

    // -----------------------------------------------------------------------
    // Core conversation
    // -----------------------------------------------------------------------

    /* Original method — calls the API and returns the plain reply text. */
    std::string getReply(const std::string& message, TokenUsage* usage = nullptr);

    /*
     * Extended method — calls getReply() then:
     *   1. Prepends trust advisory if trust is low
     *   2. Appends Transparency Report footer to the displayed text
     *   3. Logs a full AuditRecord to the JSONL audit log
     * Returns the full decorated string to display in the terminal.
     * Falls back to plain getReply() if audit components are not injected.
     */
    std::string getReplyWithAudit(const std::string& message);

    // -----------------------------------------------------------------------
    // Self-Accountable AI controls
    // -----------------------------------------------------------------------

    /*
     * Inject audit subsystem components (non-owning).
     * Must be called before getReplyWithAudit() or applyFeedback().
     */
    void setAuditComponents(AuditLogger* al, TrustTracker* tt);

    /*
     * Apply user feedback to the most recent reply.
     *   correct = true  → /feedback correct → trust +2.5
     *   correct = false → /feedback wrong   → trust −7.0
     * Also updates the audit record's feedback field in the JSONL log, optionally with severity.
     */
    void applyFeedback(bool correct, const std::string& severity = "");

    /*
     * Verify the integrity of the audit log's hash chain.
     */
    void verifyAuditChain() const;

    /*
     * Retrieve and format a single audit record for display.
     * Use "last" to get the most recent record.
     */
    std::string getAuditEntry(const std::string& id) const;

    /*
     * Retrieve and format a list of all audit records for display.
     * Use limit > 0 to restrict the number of returned records.
     */
    std::string getAuditList(int limit = -1) const;

    // -----------------------------------------------------------------------
    // History management
    // -----------------------------------------------------------------------

    void clearHistory();
    void pruneHistory();
    const std::vector<std::string>& getStringHistory() const;
};

#endif // CHATBOT_H