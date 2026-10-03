#ifndef SHA256_H
#define SHA256_H

#include <string>
#include <cstdint>

class SHA256 {
public:
    SHA256();
    void update(const uint8_t *data, size_t length);
    void update(const std::string &data);
    uint8_t* digest();
    std::string toString(const uint8_t *digest);
    static std::string hashString(const std::string &input);

private:
    uint8_t data[64];
    uint32_t datalen;
    uint64_t bitlen;
    uint32_t state[8];
    void transform();
};

#endif // SHA256_H
