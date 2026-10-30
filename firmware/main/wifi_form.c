#include "wifi_form.h"
#include <string.h>

static int hex_value(unsigned char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static bool decode_value(const char *source, char *dest, size_t capacity) {
    size_t n = 0;
    while (*source) {
        unsigned char c = (unsigned char)*source++;
        if (c == '+') c = ' ';
        else if (c == '%') {
            if (!source[0] || !source[1]) return false;
            int hi = hex_value((unsigned char)source[0]);
            int lo = hex_value((unsigned char)source[1]);
            if (hi < 0 || lo < 0) return false;
            c = (unsigned char)((hi << 4) | lo);
            source += 2;
        }
        if (!c || n + 1 >= capacity) return false;
        dest[n++] = (char)c;
    }
    dest[n] = '\0';
    return true;
}

bool wifi_form_parse(char *body, char ssid[33], char password[64]) {
    bool have_ssid = false, have_password = false;
    ssid[0] = password[0] = '\0';
    char *save = NULL;
    for (char *part = strtok_r(body, "&", &save); part; part = strtok_r(NULL, "&", &save)) {
        char *equal = strchr(part, '=');
        if (!equal) return false;
        *equal++ = '\0';
        if (strcmp(part, "ssid") == 0) {
            if (have_ssid || !decode_value(equal, ssid, 33)) return false;
            have_ssid = true;
        } else if (strcmp(part, "pass") == 0) {
            if (have_password || !decode_value(equal, password, 64)) return false;
            have_password = true;
        }
    }
    size_t pass_len = strlen(password);
    return have_ssid && have_password && ssid[0] && (!pass_len || pass_len >= 8);
}
