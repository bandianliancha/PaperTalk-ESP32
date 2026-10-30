#include "../main/wifi_form.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static void check(const char *text, bool expected, const char *ssid, const char *pass) {
    char body[512], s[33], p[64];
    snprintf(body, sizeof(body), "%s", text);
    bool valid = wifi_form_parse(body, s, p);
    assert(valid == expected);
    if (valid) { assert(strcmp(s, ssid) == 0); assert(strcmp(p, pass) == 0); }
}
int main(void) {
    check("ssid=Home+WiFi&pass=p%40ss%26%2B12", true, "Home WiFi", "p@ss&+12");
    check("ssid=%E5%AE%B6%E9%87%8C&pass=12345678", true, "家里", "12345678");
    check("ssid=abcdefghijklmnopqrstuvwx12345678&pass=", true, "abcdefghijklmnopqrstuvwx12345678", "");
    check("ssid=abcdefghijklmnopqrstuvwx123456789&pass=12345678", false, NULL, NULL);
    check("ssid=home&pass=short", false, NULL, NULL);
    check("ssid=home%00x&pass=12345678", false, NULL, NULL);
    check("ssid=home%2&pass=12345678", false, NULL, NULL);
    check("ssid=home&ssid=other&pass=12345678", false, NULL, NULL);
    check("ssid=home", false, NULL, NULL);
    puts("Wi-Fi form tests passed");
}
