#ifndef API_H
#define API_H
#include <string>
#include <nlohmann/json.hpp>

struct TokenUsage {
    int promptTokens = 0;
    int completionTokens = 0;
    int totalTokens = 0;
};

/* Core API bridge — sends history to Gemini via the Python subprocess. */
std::string callAPI(nlohmann::json& history, const std::string& model, TokenUsage& usage);

/*
 * runWrongCommand — invokes self_correction_chatbot.py --wrong as a
 * subprocess.  The Python script reads runtime_state.json (written by
 * api.py after every successful turn) and pushes the last query/response
 * into corrections.json so that the next similar query receives a
 * negatively-constrained API payload.
 *
 * Call this from Chatbot::applyFeedback(false, ...) so that a /wrong
 * command triggers BOTH the C++ trust-score update AND the Python
 * correction registration in a single atomic operation.
 */
void runWrongCommand(const std::string& severity = "medium");

#endif