#ifndef AUTH_H
#define AUTH_H

#include <string>

std::string getPasswordInput(const std::string& prompt = "Password: ");

class Auth {
public:
    bool registerUser(const std::string& username, const std::string& password);
    bool loginUser(const std::string& username, const std::string& password);
    bool changePassword(const std::string& username, const std::string& currentPassword, const std::string& newPassword);
    bool userExists(const std::string& username);
    std::string getStoredHash(const std::string& username);
    std::string generateSalt();
    std::string hashPassword(const std::string& salt, const std::string& password);
    void updateLastLogin(const std::string& username);

private:
    std::string usersFilePath();
    std::string timestampNow();
};

#endif // AUTH_H
