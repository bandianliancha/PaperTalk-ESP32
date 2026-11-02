#include "../main/wifi_form.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static void check(const char *text, bool expected, const char *ssid, const char *pass, const char *host, unsigned port) {
    char body[512], s[33], p[64], h[64]; unsigned n = 0;
    snprintf(body, sizeof(body), "%s", text);
    bool valid = wifi_form_parse(body, s, p, h, &n);
    assert(valid == expected);
    if (valid) { assert(strcmp(s, ssid) == 0); assert(strcmp(p, pass) == 0); assert(strcmp(h, host) == 0); assert(n == port); }
}
int main(void) {
    check("ssid=Home+WiFi&pass=p%40ss%26%2B12&server=example.com&port=8080", true, "Home WiFi", "p@ss&+12", "example.com", 8080);
    check("ssid=%E5%AE%B6%E9%87%8C&pass=12345678&server=192.168.1.2&port=80", true, "家里", "12345678", "192.168.1.2", 80);
    check("ssid=abcdefghijklmnopqrstuvwx12345678&pass=&server=&port=", true, "abcdefghijklmnopqrstuvwx12345678", "", "", 0);
    check("ssid=abcdefghijklmnopqrstuvwx123456789&pass=12345678", false, NULL, NULL, NULL, 0);
    check("ssid=home&pass=short", false, NULL, NULL, NULL, 0);
    check("ssid=home%00x&pass=12345678", false, NULL, NULL, NULL, 0);
    check("ssid=home%2&pass=12345678", false, NULL, NULL, NULL, 0);
    check("ssid=home&ssid=other&pass=12345678", false, NULL, NULL, NULL, 0);
    check("ssid=home", false, NULL, NULL, NULL, 0);
    check("ssid=home&pass=12345678&server=http%3A%2F%2Fexample.com", false, NULL, NULL, NULL, 0);
    check("ssid=home&pass=12345678&server=abc&port=65536", false, NULL, NULL, NULL, 0);
    check("ssid=home&pass=12345678&server=abc&port=0", false, NULL, NULL, NULL, 0);
    check("ssid=home&pass=12345678&server=abc&port=80&port=81", false, NULL, NULL, NULL, 0);
    puts("Wi-Fi and server form tests passed");
}
