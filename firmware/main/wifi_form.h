#pragma once
#include <stdbool.h>
#include <stddef.h>

// Parse an application/x-www-form-urlencoded body without truncation.
bool wifi_form_parse(char *body, char ssid[33], char password[64]);
