#ifndef USER_H
#define USER_H
#include <cctype>
#include <string>
#include <string_view>

class User {
    std::string name;
public:
    void        setName(const std::string& n) { name = n; }
    std::string getName() const               { return name; }
};

/*
 * sanitizePathToken — strict allow-list sanitization for any string that will
 * be embedded in a file path (usernames, identifiers, etc.).
 *
 * Rules:
 *   - Input longer than maxLen is truncated before checking.
 *   - Any character outside [A-Za-z0-9], '-', '_' causes the ENTIRE input to
 *     be rejected (returns ""), not just that character.  This prevents
 *     partial-strip attacks where removing a bad character accidentally creates
 *     a valid path-traversal sequence.
 *   - Null bytes ('\0') are explicitly caught FIRST and cause immediate rejection,
 *     because std::string_view iteration may expose them before the isalnum check.
 *   - An empty or all-invalid input returns "".
 *
 * Path-traversal sequences (../, ..\, absolute /path, etc.) cannot survive
 * this filter because '/', '\', and '.' are all rejected.
 */
inline std::string sanitizePathToken(std::string_view input, size_t maxLen = 64) {
    if (input.empty()) return {};

    // Enforce maximum length before any character inspection
    if (input.size() > maxLen) input = input.substr(0, maxLen);

    std::string out;
    out.reserve(input.size());

    for (unsigned char c : input) {
        // Explicit null-byte check: reject the ENTIRE input, not just skip it.
        // A null byte in a path token is always malicious or malformed.
        if (c == '\0') return {};

        // Allow-list: alphanumeric, hyphen, underscore only.
        // '/', '\\', '.', '@', ' ', and every other character is rejected.
        if (std::isalnum(c) || c == '-' || c == '_') {
            out.push_back(static_cast<char>(c));
        } else {
            // Any disallowed character causes a full rejection — no partial output.
            return {};
        }
    }

    return out; // empty() is valid here (e.g. input was all digits that passed)
}

#endif // USER_H