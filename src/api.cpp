/*
 * api.cpp — Gemini API bridge via Python subprocess with UUID-based IPC.
 *
 * Security notes:
 *   - Linux/Mac: uses fork()+execvp() with an explicit argv[] — NO shell involved.
 *     The path strings are stored as std::string locals so their c_str() pointers
 *     remain valid for the entire argv[] lifetime.
 *   - Windows: uses CreateProcessA() directly — lpApplicationName=nullptr, but the
 *     command string starts with the bare executable name ("python"), which Windows
 *     resolves via PATH. No cmd.exe or any shell interpreter is invoked.
 *     Arguments containing spaces are wrapped with std::quoted.
 *
 * Per-call UUID filenames prevent concurrent-session collisions.
 * All temp files are isolated under ./temp/ (created on startup).
 */

#include "api.h"
#include "logger.h"
#include <array>
#include <chrono>
#include <fstream>
#include <sstream>
#include <iomanip>
#include <random>
#include <filesystem>
#include <vector>
#include <cstdlib>

#ifdef _WIN32
  #include <windows.h>
#else
  #include <unistd.h>
  #include <sys/wait.h>
  #include <signal.h>
  #include <time.h>
#endif

namespace fs = std::filesystem;
static constexpr int TIMEOUT_SECS = 25;

namespace {

// Returns (and lazily creates) the isolated temp directory ./temp/.
// On POSIX, permissions are set to owner-only (0700) to restrict access.
fs::path tempDir() {
    fs::path dir = fs::path("temp");
    std::error_code ec;
    fs::create_directories(dir, ec);
    if (ec || !fs::exists(dir)) {
#ifndef _WIN32
        dir = fs::path("/tmp");
#else
        const char* tmpEnv = std::getenv("TEMP");
        if (!tmpEnv) tmpEnv = std::getenv("TMP");
        if (tmpEnv) dir = fs::path(tmpEnv);
#endif
        Logger::log("WARN", "Using fallback temp directory: " + dir.string());
    }
    return dir;
}

void ensureRestrictivePermissions(const fs::path& path) {
#ifdef _WIN32
    (void)path; // Windows ACL management is beyond fs::permissions scope
#else
    std::error_code ec;
    fs::permissions(path,
                    fs::perms::owner_read | fs::perms::owner_write | fs::perms::owner_exec,
                    fs::perm_options::replace, ec);
    if (ec) Logger::log("WARN", "Could not set permissions on temp dir: " + ec.message());
#endif
}

void removeTempFile(const fs::path& path) {
    std::error_code ec;
    fs::remove(path, ec);
    if (ec) Logger::log("WARN", "Could not remove IPC file " + path.string() + ": " + ec.message());
}

std::string makeToken() {
    static thread_local std::mt19937_64 rng(std::random_device{}());
    std::array<std::uint8_t, 16> b;
    for (auto& x : b) x = rng() & 0xFF;
    b[6] = (b[6] & 0x0F) | 0x40;
    b[8] = (b[8] & 0x3F) | 0x80;
    std::ostringstream ss;
    ss << std::hex << std::setfill('0');
    for (size_t i = 0; i < b.size(); ++i) {
        ss << std::setw(2) << static_cast<int>(b[i]);
        if (i == 3 || i == 5 || i == 7 || i == 9) ss << '-';
    }
    return ss.str();
}

static std::string getPythonExecutable() {
    const char* env = std::getenv("PYTHON_EXECUTABLE");
    if (env && *env) {
        return std::string(env);
    }
#ifdef _WIN32
    return "python";
#else
    return "python3";
#endif
}

} // namespace

// ---------------------------------------------------------------------------
// runPython — spawn api.py without a shell on either platform.
//
// POSIX: fork()+execvp with explicit argv[] — no shell.
//   The req/res path strings are stored as local std::strings so that the
//   c_str() pointers remain valid for the entire duration of the child exec.
//
// Windows: CreateProcessA with a quoted command line — no cmd.exe.
//   std::quoted wraps each path argument in double-quotes; arguments with
//   embedded double-quotes are escaped per Windows conventions.
// ---------------------------------------------------------------------------
static bool runPython(const fs::path& req, const fs::path& res, int timeout = TIMEOUT_SECS) {
    const fs::path runtimeDir = tempDir();
    ensureRestrictivePermissions(runtimeDir);

    fs::path scriptPath = fs::current_path() / "api.py";
    if (!fs::exists(scriptPath)) {
        scriptPath = fs::current_path().parent_path() / "api.py";
    }
    if (!fs::exists(scriptPath)) {
        Logger::log("ERROR", "runPython: cannot locate api.py");
        return false;
    }

    std::string pythonExe = getPythonExecutable();

#ifdef _WIN32
    // Build the command line safely: "<pythonExe>" "<absolute_script_path>" "req" "res"
    // std::quoted ensures embedded spaces/quotes in paths are correctly escaped.
    // CreateProcessA resolves pythonExe via PATH or explicit binary path; no shell is spawned.
    std::ostringstream cmdLineStream;
    if (pythonExe.find(' ') != std::string::npos && pythonExe.front() != '"') {
        cmdLineStream << "\"" << pythonExe << "\" ";
    } else {
        cmdLineStream << pythonExe << " ";
    }
    cmdLineStream << std::quoted(scriptPath.string()) << " "
                  << std::quoted(req.string()) << " "
                  << std::quoted(res.string());
    std::string cmdLine = cmdLineStream.str();

    STARTUPINFOA si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};

    // CreateProcessA requires a mutable buffer for the command line
    std::vector<char> cmdBuf(cmdLine.begin(), cmdLine.end());
    cmdBuf.push_back('\0');

    if (!CreateProcessA(
            nullptr,          // lpApplicationName — resolved from command line
            cmdBuf.data(),    // lpCommandLine (mutable)
            nullptr, nullptr, // process/thread security
            FALSE,            // no handle inheritance
            CREATE_NO_WINDOW, // prevent a console window for the Python helper
            nullptr, nullptr, // environment / current directory (inherit)
            &si, &pi)) {
        Logger::log("ERROR", "CreateProcess failed: python subprocess could not be started (" + pythonExe + ")");
        return false;
    }

    DWORD waitResult = WaitForSingleObject(pi.hProcess, static_cast<DWORD>(timeout) * 1000);
    if (waitResult == WAIT_TIMEOUT) {
        TerminateProcess(pi.hProcess, 1);
        Logger::log("WARN", "Python subprocess timed out after " + std::to_string(timeout) + "s");
    }
    DWORD exitCode = 1;
    if (waitResult == WAIT_OBJECT_0 && !GetExitCodeProcess(pi.hProcess, &exitCode)) {
        Logger::log("ERROR", "Could not read Python subprocess exit code");
    }
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    if (waitResult == WAIT_OBJECT_0 && exitCode != 0) {
        Logger::log("ERROR", "Python subprocess exited with code " + std::to_string(exitCode));
    }
    return waitResult == WAIT_OBJECT_0 && exitCode == 0;

#else
    // Store the path strings as std::string locals so their c_str() pointers
    // remain valid for the full lifetime of the argv[] array and the execvp call.
    // Using the raw fs::path::c_str() directly would be unsafe if the path object
    // were a temporary expression.
    std::string reqStr = req.string();
    std::string resStr = res.string();
    std::string scriptStr = scriptPath.string();

    // Build an explicit argv[] — execvp does NOT invoke a shell.
    // argv[0] = pythonExe (execvp searches PATH or absolute path for this name)
    // argv[1] = absolute api.py path, argv[2] = req path, argv[3] = res path
    std::vector<char*> argv;
    argv.push_back(const_cast<char*>(pythonExe.c_str()));
    argv.push_back(const_cast<char*>(scriptStr.c_str()));
    argv.push_back(const_cast<char*>(reqStr.c_str()));
    argv.push_back(const_cast<char*>(resStr.c_str()));
    argv.push_back(nullptr);

    pid_t pid = fork();
    if (pid < 0) {
        Logger::log("ERROR", "fork() failed — cannot spawn Python subprocess");
        return false;
    }
    if (pid == 0) {
        // Child process: replace image with python — no shell is used.
        execvp(pythonExe.c_str(), argv.data());
        // execvp returns only on error; exit immediately so we don't duplicate
        // parent state or run destructors in the child.
        _exit(127);
    }

    // Parent: poll with a deadline instead of blocking waitpid to honour timeout.
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout);
    int status = 0;
    bool exited = false;

    while (std::chrono::steady_clock::now() < deadline) {
        pid_t r = waitpid(pid, &status, WNOHANG);
        if (r == pid)  { exited = true; break; }
        if (r < 0)     { break; }
        // Keep subprocess completion detection responsive without busy-waiting.
        struct timespec ts{0, 25'000'000L};
        nanosleep(&ts, nullptr);
    }

    if (!exited) {
        kill(pid, SIGKILL);
        waitpid(pid, &status, 0);
        Logger::log("WARN", "Python subprocess timed out after " + std::to_string(timeout) + "s — killed");
        return false;
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
#endif
}

// ---------------------------------------------------------------------------
// callAPI — public entry point
// ---------------------------------------------------------------------------
std::string callAPI(nlohmann::json& history, const std::string& model, TokenUsage& usage) {
    if (history.empty()) {
        Logger::log("WARN", "callAPI: empty history");
        return "Error: Empty message history.";
    }

    auto token   = makeToken();
    auto reqFile = tempDir() / ("request_"  + token + ".json");
    auto resFile = tempDir() / ("response_" + token + ".json");

    // --- Write request file ---
    {
        std::ofstream out(reqFile);
        if (!out.is_open()) {
            Logger::log("ERROR", "Cannot create request file in temp/");
            return "System Error: Local workspace directory is write-protected.";
        }
        try {
            out << nlohmann::json{{"model", model}, {"messages", history}}.dump();
            if (!out.good()) {
                Logger::log("ERROR", "Request file write failed");
                out.close();
                removeTempFile(reqFile);
                return "System Error: Local workspace directory is write-protected.";
            }
        } catch (const std::exception& e) {
            Logger::log("ERROR", "Request JSON serialization failed: " + std::string(e.what()));
            out.close();
            removeTempFile(reqFile);
            return "System Error: Local workspace directory is write-protected.";
        }
    }

    // --- Spawn Python subprocess (no shell) ---
    bool processOk = runPython(reqFile, resFile);
    removeTempFile(reqFile);

    if (!processOk) {
        removeTempFile(resFile);
        Logger::log("ERROR", "Python subprocess failed or timed out");
        return "Error: Python API bridge failed or timed out. Check Python, dependencies, and api.log.";
    }

    // --- Verify response file exists before reading ---
    std::error_code responseEc;
    if (!fs::exists(resFile, responseEc) || responseEc) {
        Logger::log("ERROR", "Response file was not created by subprocess");
        return "Error: Python API bridge did not create a response. Check Python, dependencies, and api.log.";
    }

    // --- Read and parse response ---
    nlohmann::json res;
    {
        std::ifstream in(resFile);
        if (!in.is_open()) {
            Logger::log("ERROR", "Cannot open response file");
            removeTempFile(resFile);
            return "Error: Python API bridge response could not be read. Check api.log.";
        }
        try {
            in >> res;
            if (in.fail()) {
                Logger::log("ERROR", "Response file read failed");
                in.close();
                removeTempFile(resFile);
                return "Error: Python API bridge returned an unreadable response. Check api.log.";
            }
        } catch (const nlohmann::json::parse_error& e) {
            Logger::log("ERROR", "JSON parse error in response: " + std::string(e.what()));
            in.close();
            removeTempFile(resFile);
            return "Error: Python API bridge returned invalid JSON. Check api.log.";
        }
    }
    removeTempFile(resFile);

    // --- Check for API-level error ---
    if (res.contains("error")) {
        auto err = res["error"].is_string()
                   ? res["error"].get<std::string>()
                   : res["error"].dump();
        Logger::log("WARN", "API error: " + err);
        return "Error: " + err;
    }

    // --- Extract token usage from the response JSON ---
    if (auto usageObj = res.find("usage"); usageObj != res.end() && usageObj->is_object()) {
        usage.promptTokens     = usageObj->value("prompt_tokens", 0);
        usage.completionTokens = usageObj->value("completion_tokens", 0);
        usage.totalTokens      = usageObj->value("total_tokens", 0);
    } else {
        usage.promptTokens = 0;
        usage.completionTokens = 0;
        usage.totalTokens = 0;
    }

    // --- Extract assistant content ---
    if (auto ch = res.find("choices");
        ch != res.end() && ch->is_array() && !ch->empty()) {
        if (auto msg = ch->at(0).find("message");
            msg != ch->at(0).end() && msg->contains("content")) {
            Logger::log("INFO", "callAPI success");
            return msg->at("content").get<std::string>();
        }
    }

    Logger::log("ERROR", "Unexpected response structure from API");
    return "Error: Invalid API response format.";
}

// ---------------------------------------------------------------------------
// runWrongCommand — spawn self_correction_chatbot.py --wrong (no shell).
//
// This bridges the C++ /wrong command to the Python correction registry.
// The Python script reads runtime_state.json (written by api.py after every
// successful turn) and appends the correction to corrections.json.
//
// Script resolution strategy (handles both dev and installed layouts):
//   1. Same directory as the running executable  (build/)
//   2. Parent of the running executable's directory (project root)
//   3. Current working directory
// The first path where self_correction_chatbot.py is found wins.
// ---------------------------------------------------------------------------
void runWrongCommand(const std::string& severity) {
    // --- Locate self_correction_chatbot.py ---
    const std::string scriptName = "self_correction_chatbot.py";
    fs::path scriptPath;

    // Probe candidate directories in preference order.
    auto probe = [&](const fs::path& dir) -> bool {
        fs::path candidate = dir / scriptName;
        std::error_code ec;
        if (fs::exists(candidate, ec) && fs::is_regular_file(candidate, ec)) {
            scriptPath = candidate;
            return true;
        }
        return false;
    };

    if (!probe(fs::current_path()) && !probe(fs::current_path().parent_path())) {
        std::error_code ec;
        fs::path workspaceRoot = fs::current_path();
        while (!workspaceRoot.empty()) {
            if (probe(workspaceRoot)) break;
            workspaceRoot = workspaceRoot.parent_path();
        }
    }

#ifdef _WIN32
    // On Windows, GetModuleFileNameA gives us the absolute path of the .exe.
    std::vector<char> exeBuf(MAX_PATH, '\0');
    DWORD exeLen = GetModuleFileNameA(nullptr, exeBuf.data(), static_cast<DWORD>(exeBuf.size()));
    if (exeLen > 0) {
        fs::path exeDir = fs::path(std::string(exeBuf.data(), exeLen)).parent_path();
        probe(exeDir) || probe(exeDir.parent_path());
    }
#else
    // On POSIX, /proc/self/exe (Linux) or argv[0]-based resolution.
    {
        std::error_code ec;
        fs::path exeDir = fs::read_symlink("/proc/self/exe", ec).parent_path();
        if (!ec) probe(exeDir) || probe(exeDir.parent_path());
    }
#endif

    // Fallback: current working directory.
    if (scriptPath.empty()) {
        probe(fs::current_path());
    }

    if (scriptPath.empty()) {
        Logger::log("ERROR", "runWrongCommand: cannot locate " + scriptName);
        std::cout << "  [Error] Cannot locate " << scriptName
                  << " — correction was NOT registered.\n"
                  << "  Trust score has been updated but corrections.json was not written.\n";
        return;
    }

    Logger::log("INFO", "runWrongCommand: using script at " + scriptPath.string());

#ifdef _WIN32
    // Build command line: <pythonExe> "<absolute_path_to_script>" --wrong --severity "<severity>"
    std::string pyExe = getPythonExecutable();
    std::ostringstream cmdStream;
    if (pyExe.find(' ') != std::string::npos && pyExe.front() != '"') {
        cmdStream << "\"" << pyExe << "\" ";
    } else {
        cmdStream << pyExe << " ";
    }
    cmdStream << std::quoted(scriptPath.string()) << " --wrong --severity " << std::quoted(severity);
    std::string cmdLine = cmdStream.str();

    std::vector<char> cmdBuf(cmdLine.begin(), cmdLine.end());
    cmdBuf.push_back('\0');

    STARTUPINFOA si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};

    if (!CreateProcessA(nullptr, cmdBuf.data(), nullptr, nullptr,
                        FALSE, CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        Logger::log("ERROR", "runWrongCommand: CreateProcess failed");
        std::cout << "  [Error] Could not spawn correction handler process.\n";
        return;
    }

    // Wait up to 15 seconds for the correction script to finish.
    DWORD result = WaitForSingleObject(pi.hProcess, 15'000);
    if (result == WAIT_TIMEOUT) {
        TerminateProcess(pi.hProcess, 1);
        Logger::log("WARN", "runWrongCommand: Python correction handler timed out");
        std::cout << "  [Warning] Correction handler timed out.\n";
    } else {
        DWORD exitCode = 0;
        GetExitCodeProcess(pi.hProcess, &exitCode);
        if (exitCode != 0) {
            Logger::log("WARN", "runWrongCommand: Python handler exited with code "
                        + std::to_string(exitCode));
        } else {
            Logger::log("INFO", "runWrongCommand: correction registered successfully");
        }
    }
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);

#else
    // POSIX: fork()+execvp without a shell.
    std::string pyExeP = getPythonExecutable();
    std::string scriptStr = scriptPath.string();
    std::string sevStr = severity;
    std::vector<char*> argv;
    argv.push_back(const_cast<char*>(pyExeP.c_str()));
    argv.push_back(const_cast<char*>(scriptStr.c_str()));
    argv.push_back(const_cast<char*>("--wrong"));
    argv.push_back(const_cast<char*>("--severity"));
    argv.push_back(const_cast<char*>(sevStr.c_str()));
    argv.push_back(nullptr);

    pid_t pid = fork();
    if (pid < 0) {
        Logger::log("ERROR", "runWrongCommand: fork() failed");
        return;
    }
    if (pid == 0) {
        execvp(pyExeP.c_str(), argv.data());
        _exit(127);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
        Logger::log("INFO", "runWrongCommand: correction registered successfully");
    } else {
        Logger::log("WARN", "runWrongCommand: handler exited with " + std::to_string(WEXITSTATUS(status)));
    }
#endif
}
