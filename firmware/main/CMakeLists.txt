idf_component_register(SRCS "main.c" "mp3_decode.c" "wifi_form.c"
                       INCLUDE_DIRS "."
                       REQUIRES spiffs espressif__esp_websocket_client esp_http_client esp_http_server json esp_wifi nvs_flash driver espressif__esp_codec_dev esp_adc)
