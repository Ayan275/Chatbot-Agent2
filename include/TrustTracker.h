/*
 * TrustTracker.h — Self-Monitoring Reliability Score for the Self-Accountable AI system.
 *
 * The TrustTracker maintains a floating-point reliability score in [0, 100].
 * The score starts at 100 and decays on user-reported errors; it recovers
 * (with a smaller delta) on user confirmation of correct answers.
 *
 * Score deltas (see TrustTracker.cpp for exact constants):
 *   markWrong()   → −7.0  (clamped at 0.0)
 *   markCorrect() → +2.5  (clamped at 100.0)
 *
 * Thresholds for advisory messages:
 *   score < 70  → moderate advisory prepended to AI reply
 *   score < 40  → critical advisory prepended to AI reply
 *
 * Persistence:
 *   State is saved to audit_logs/<user>_trust.json after every mutation.
 *   On construction it loads a previously saved score so trust persists
 *   across sessions for the same user.
 */

#ifndef TRUST_TRACKER_H
#define TRUST_TRACKER_H

#include <string>

class TrustTracker {
public:
    /*
     * Construct a TrustTracker for the given user.
     * Loads a persisted score from disk if one exists; otherwise starts at 100.
     *
     * @param userName  Used to derive the per-user persistence filename.
     */
    explicit TrustTracker(const std::string& userName);

    // Non-copyable (owns file-path state, semantically a singleton per user)
    TrustTracker(const TrustTracker&)            = delete;
    TrustTracker& operator=(const TrustTracker&) = delete;

    // -----------------------------------------------------------------------
    // Mutators — both persist the new score immediately after update
    // -----------------------------------------------------------------------

    /*
     * Decrease trust because a response was marked wrong.
     * Prints a formatted notification to stdout.
     */
    void markWrong();

    /*
     * Increase trust because a response was confirmed correct.
     * Prints a formatted notification to stdout.
     */
    void markCorrect();

    // -----------------------------------------------------------------------
    // Queries
    // -----------------------------------------------------------------------

    /* Current trust score in [0.0, 100.0]. */
    double getScore() const;

    /*
     * Returns a non-empty advisory string if trust is low enough to warrant
     * a user warning before an AI response; returns "" otherwise.
     * Callers should prepend this to the response display when non-empty.
     */
    std::string getWarningIfLow() const;

    /*
     * Returns a multi-line, human-readable trust summary for the /trust command.
     * Includes current score, rating label, and interpretation.
     */
    std::string getSummary() const;

    /*
     * Short one-line label for the current score level:
     *   ≥ 90 → "Excellent"
     *   ≥ 70 → "Good"
     *   ≥ 50 → "Moderate"
     *   ≥ 30 → "Low"
     *    < 30 → "Critical"
     */
    std::string getRating() const;

private:
    double      score_;       // Current trust score
    std::string persistPath_; // Full path to the JSON persistence file
    int         totalFeedback_;
    int         wrongFeedback_;

    // Clamp score_ to [0.0, 100.0] and write to disk
    void applyAndPersist(double newScore);

    // Write current state to persistPath_ (fails silently on I/O error)
    void save() const;

    // Load persisted state from persistPath_; no-op if file is absent/invalid
    void load();
};

#endif // TRUST_TRACKER_H
