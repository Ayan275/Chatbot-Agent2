#ifndef LOGGER_H
#define LOGGER_H

#include <string_view>
#include <iostream>
#include <fstream>
#include <chrono>
#include <ctime>
#include <iomanip>
#include <mutex>

class Logger {
public:
    static void log(std::string_view level, std::string_view msg, std::string_view filename = "app.log") {
        static std::mutex logMutex;
        std::lock_guard<std::mutex> lock(logMutex);
        
        try {
            std::ofstream logFile(std::string(filename), std::ios::app);
            if (!logFile.is_open()) return;

            auto now = std::chrono::system_clock::now();
            auto t = std::chrono::system_clock::to_time_t(now);
            std::tm buf{};
#ifdef _WIN32
            localtime_s(&buf, &t);
#else
            localtime_r(&t, &buf);
#endif
            logFile << std::put_time(&buf, "%Y-%m-%dT%H:%M:%S") 
                    << " [" << level << "] " << msg << "\n";
        } catch (...) {
            // Fail silently to prevent app crash due to logging
        }
    }
};

#endif // LOGGER_H
