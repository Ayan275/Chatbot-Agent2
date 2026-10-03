/*
 * TrustTracker.cpp — Implementation of the Self-Monitoring Reliability Score.
 *
 * Score formula (all values clamped to [0.0, 100.0]):
 *   markWrong()   → score -= DELTA_WRONG   (7.0)
 *   markCorrect() → score += DELTA_CORRECT (2.5)
 *
 * Advisory thresholds:
 *   score < THRESHOLD_LOW      (70.0) → moderate advisory
 *   score < THRESHOLD_CRITICAL (40.0) → critical advisory
 *
 * Persistence file: audit_logs/<userName>_trust.json
 *   { "score": 85.5, "total_feedback": 10, "wrong_feedback": 2 }
 */

#include "TrustTracker.h"
#include "logger.h"
#include <nlohmann/json.hpp>
#include <fstream>
#include <sstream>
#include <iomanip>
#include <algorithm>
#include <iostream>
#include <filesystem>

namespace fs = std::filesystem;
using json   = nlohmann::json;

namespace {
    constexpr double INITIAL_SCORE      = 100.0;
    constexpr double DELTA_WRONG        =   7.0;
    constexpr double DELTA_CORRECT      =   2.5;
    constexpr double THRESHOLD_LOW      =  70.0;
    constexpr double THRESHOLD_CRITICAL =  40.0;
}

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

TrustTracker::TrustTracker(const std::string& userName)
    : score_(INITIAL_SCORE),
      persistPath_((fs::path("audit_logs") / (userName + "_trust.json")).string()),
      totalFeedback_(0),
      wrongFeedback_(0)
{
    // Ensure audit_logs/ directory exists
    try {
        fs::path dir = fs::path(persistPath_).parent_path();
        if (!dir.empty() && !fs::exists(dir)) {
            fs::create_directories(dir);
        }
    } catch (const std::exception& e) {
        Logger::log("WARN", std::string("TrustTracker: cannot create dir: ") + e.what());
    }

    load();
    Logger::log("INFO", "TrustTracker: loaded score=" + std::to_string(score_) + " for user=" + userName);
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

void TrustTracker::save() const {
    try {
        json j;
        j["score"]          = score_;
        j["total_feedback"] = totalFeedback_;
        j["wrong_feedback"] = wrongFeedback_;
        
        fs::path target(persistPath_);
        fs::path tmp = target;
        tmp += ".tmp";

        {
            std::ofstream out(tmp);
            if (!out.is_open()) {
                Logger::log("ERROR", "TrustTracker: cannot write to temp file: " + tmp.string());
                return;
            }
            out << j.dump(4) << "\n";
            if (!out.good()) {
                out.close();
                fs::remove(tmp);
                Logger::log("ERROR", "TrustTracker: stream write error on temp file: " + tmp.string());
                return;
            }
        }

        std::error_code ec;
        fs::rename(tmp, target, ec);
        if (ec) {
            fs::remove(tmp);
            Logger::log("ERROR", "TrustTracker: atomic rename failed for " + target.string() + ": " + ec.message());
        }
    } catch (const std::exception& e) {
        Logger::log("ERROR", std::string("TrustTracker: save failed: ") + e.what());
    }
}

void TrustTracker::load() {
    std::ifstream in(persistPath_);
    if (!in.is_open()) return; // First run — use defaults

    try {
        json j;
        in >> j;
        if (j.contains("score") && j["score"].is_number()) {
            double loaded = j["score"].get<double>();
            // Clamp to valid range in case of file corruption
            score_ = std::clamp(loaded, 0.0, 100.0);
        }
        if (j.contains("total_feedback") && j["total_feedback"].is_number_integer())
            totalFeedback_ = j["total_feedback"].get<int>();
        if (j.contains("wrong_feedback") && j["wrong_feedback"].is_number_integer())
            wrongFeedback_ = j["wrong_feedback"].get<int>();
    } catch (const std::exception& e) {
        Logger::log("WARN", std::string("TrustTracker: load parse error (using defaults): ") + e.what());
        score_ = INITIAL_SCORE;
        totalFeedback_ = wrongFeedback_ = 0;
    }
}

void TrustTracker::applyAndPersist(double newScore) {
    score_ = std::clamp(newScore, 0.0, 100.0);
    save();
}

// ---------------------------------------------------------------------------
// Mutators
// ---------------------------------------------------------------------------

void TrustTracker::markWrong() {
    double previous = score_;
    ++totalFeedback_;
    ++wrongFeedback_;
    applyAndPersist(score_ - DELTA_WRONG);

    // Print notification to stdout
    std::cout << "\n"
              << "  [Trust Update]\n"
              << "  Previous reliability score : " << std::fixed << std::setprecision(1) << previous << "%\n"
              << "  New reliability score      : " << score_ << "%\n"
              << "  Reason                     : Response marked as incorrect.\n"
              << "  Status                     : Command accepted successfully.\n\n";

    Logger::log("INFO", "TrustTracker: markWrong prev=" + std::to_string(previous) + " new=" + std::to_string(score_));
}

void TrustTracker::markCorrect() {
    double previous = score_;
    ++totalFeedback_;
    applyAndPersist(score_ + DELTA_CORRECT);

    // Print notification to stdout
    std::cout << "\n"
              << "  [Trust Update]\n"
              << "  Previous reliability score : " << std::fixed << std::setprecision(1) << previous << "%\n"
              << "  New reliability score      : " << score_ << "%\n"
              << "  Reason                     : Response confirmed correct. Thank you!\n\n";

    Logger::log("INFO", "TrustTracker: markCorrect prev=" + std::to_string(previous) + " new=" + std::to_string(score_));
}

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

double TrustTracker::getScore() const { return score_; }

std::string TrustTracker::getRating() const {
    if (score_ >= 90.0) return "Excellent";
    if (score_ >= 70.0) return "Good";
    if (score_ >= 50.0) return "Moderate";
    if (score_ >= 30.0) return "Low";
    return "Critical";
}

std::string TrustTracker::getWarningIfLow() const {
    if (score_ < THRESHOLD_CRITICAL) {
        return
            "  [!] CRITICAL RELIABILITY ADVISORY\n"
            "  My trust score has dropped significantly due to multiple corrected\n"
            "  responses. Confidence in my answers is substantially reduced.\n"
            "  Please independently verify ALL information I provide.\n\n";
    }
    if (score_ < THRESHOLD_LOW) {
        return
            "  [!] Reliability Advisory\n"
            "  My confidence in this topic is reduced because previous responses\n"
            "  required correction. Please verify critical information.\n\n";
    }
    return ""; // No advisory needed
}

std::string TrustTracker::getSummary() const {
    // Accuracy percentage (avoid division by zero)
    double accuracy = (totalFeedback_ > 0)
        ? (static_cast<double>(totalFeedback_ - wrongFeedback_) / totalFeedback_) * 100.0
        : 100.0;

    std::ostringstream ss;
    ss << std::fixed << std::setprecision(1);
    ss << "\n"
       << "  ╔══════════════════════════════════════╗\n"
       << "  ║      AI RELIABILITY TRUST SCORE      ║\n"
       << "  ╠══════════════════════════════════════╣\n"
       << "  ║  Current Score  : " << std::setw(6) << score_ << "%"
                                   << std::setw(14) << "" << "║\n"
       << "  ║  Rating         : " << std::setw(20) << getRating()
                                   << "║\n"
       << "  ║  Total Feedback : " << std::setw(20) << totalFeedback_
                                   << "║\n"
       << "  ║  Wrong Answers  : " << std::setw(20) << wrongFeedback_
                                   << "║\n"
       << "  ║  Accuracy       : " << std::setw(6) << accuracy << "%"
                                   << std::setw(14) << "" << "║\n"
       << "  ╠══════════════════════════════════════╣\n";

    // Interpretation line
    if (score_ >= 90.0)
        ss << "  ║  Status: Fully reliable — high confidence responses.  ║\n";
    else if (score_ >= 70.0)
        ss << "  ║  Status: Generally reliable — minor caution advised.  ║\n";
    else if (score_ >= 50.0)
        ss << "  ║  Status: Moderate trust — verify important answers.   ║\n";
    else if (score_ >= 30.0)
        ss << "  ║  Status: Low trust — independent verification needed. ║\n";
    else
        ss << "  ║  Status: Critical — treat all answers with caution.   ║\n";

    ss << "  ╚══════════════════════════════════════╝\n\n"
       << "  Use /feedback correct  to confirm a good answer  (+2.5%)\n"
       << "  Use /feedback wrong    to report a bad answer    (-7.0%)\n\n";

    return ss.str();
}
