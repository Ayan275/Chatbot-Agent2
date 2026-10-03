#ifndef MEMORY_H
#define MEMORY_H
#include <string>

class Memory {
    std::string filename, userName;
    size_t      maxFileBytes;
public:
    explicit Memory(const std::string& user = "anonymous", size_t maxBytes = 1048576);
    void        saveChat(std::string msg);
    void        showHistory() const;
    void        clearHistoryFile();
    void        setUserName(const std::string& user);
    std::string getHistoryFile() const { return filename; }
};

#endif