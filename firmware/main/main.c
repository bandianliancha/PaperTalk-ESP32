#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/spi_master.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_websocket_client.h"
#include "esp_http_client.h"
#include "cJSON.h"
#include "esp_http_server.h"
#include "prov_screen.h"
#include <nvs.h>
#include "esp_pm.h"
#include "esp_sleep.h"

static char g_wifi_ssid[64] = "DIXAI";
static char g_wifi_pass[64] = "dixchain@pass01!";
static char g_server_host[64] = "112.126.63.100";
static int g_server_port = 80;
static bool g_in_prov_mode = false;
static int g_wifi_retry_count = 0;

static const char *TAG = "FEISHU_VOICE";

// ── WiFi 配置 ──────────────────────────────────────────────────
#define WIFI_SSID      "DIXAI"
#define WIFI_PASS      "dixchain@pass01!"

// ── 服务器配置 ────────────────────────────────────────────────
#define DEFAULT_SERVER_HOST    "112.126.63.100"
#define DEFAULT_SERVER_PORT    80

// ── Note4 板级 GPIO ──────────────────────────────────────────
#define VBAT_PWR_PIN   GPIO_NUM_17  // 必须拉高自锁供电
#define AUDIO_PWR_PIN  GPIO_NUM_42  // 音频供电 + I2C上拉
#define PA_CTRL_PIN    GPIO_NUM_46  // 功放使能 (高=出声)
#define LED_PIN        GPIO_NUM_3   // 绿灯 (低电平亮)

#define KEY_ENTER_PIN  GPIO_NUM_0   // 中间按键 (低有效)
#define KEY_UP_PIN     GPIO_NUM_39  // 侧上键 (低有效, 音量+)
#define KEY_DOWN_PIN   GPIO_NUM_18  // 侧下键 (低有效, 音量-)

static int g_current_volume = 90; // 默认 90% (清脆适中，范围: 60% ~ 100%)

// I2C
#define I2C_SDA_PIN    GPIO_NUM_47
#define I2C_SCL_PIN    GPIO_NUM_48

// I2S
#define I2S_MCLK_PIN   GPIO_NUM_14
#define I2S_BCLK_PIN   GPIO_NUM_15
#define I2S_WS_PIN     GPIO_NUM_38
#define I2S_DOUT_PIN   GPIO_NUM_45  // 扬声器输出
#define I2S_DIN_PIN    GPIO_NUM_16  // 麦克风输入

#include <esp_adc/adc_oneshot.h>
#include <esp_adc/adc_cali.h>
#include <esp_adc/adc_cali_scheme.h>

#define CHARGE_DETECT_PIN  GPIO_NUM_2
#define CHARGE_FULL_PIN    GPIO_NUM_1

static adc_oneshot_unit_handle_t adc_handle = NULL;
static adc_cali_handle_t adc_cali_handle = NULL;

static void bsp_battery_init(void) {
    // 充电检测引脚
    gpio_config_t chg_cfg = {
        .pin_bit_mask = (1ULL << CHARGE_DETECT_PIN) | (1ULL << CHARGE_FULL_PIN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&chg_cfg);

    // ADC 初始化 (ADC1_CHANNEL_3 对应 GPIO4)
    adc_oneshot_unit_init_cfg_t init_cfg = {
        .unit_id = ADC_UNIT_1,
        .clk_src = ADC_RTC_CLK_SRC_DEFAULT,
    };
    adc_oneshot_new_unit(&init_cfg, &adc_handle);

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    adc_oneshot_config_channel(adc_handle, ADC_CHANNEL_3, &chan_cfg);

    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = ADC_UNIT_1,
        .chan = ADC_CHANNEL_3,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    adc_cali_create_scheme_curve_fitting(&cali_cfg, &adc_cali_handle);
    ESP_LOGI(TAG, "Battery ADC initialized");
}

static int read_battery_percent(bool *charging) {
    if (charging) {
        *charging = (gpio_get_level(CHARGE_DETECT_PIN) == 0);
    }
    int sum = 0, n = 0;
    for (int i = 0; i < 5; i++) {
        int raw = 0, mv = 0;
        if (adc_oneshot_read(adc_handle, ADC_CHANNEL_3, &raw) == ESP_OK) {
            if (adc_cali_raw_to_voltage(adc_cali_handle, raw, &mv) == ESP_OK) {
                sum += mv * 2;
                n++;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    if (n == 0) return 90;
    int avg = sum / n;
    int p = (-1 * avg * avg + 9016 * avg - 19189000) / 10000;
    if (p > 100) p = 100;
    if (p < 0) p = 0;
    return p;
}

// 4.2" EPD (SSD1683 / 400x300)
#define EPD_PWR_PIN    GPIO_NUM_6
#define EPD_BUSY_PIN   GPIO_NUM_8
#define EPD_RST_PIN    GPIO_NUM_9
#define EPD_DC_PIN     GPIO_NUM_10
#define EPD_CS_PIN     GPIO_NUM_11
#define EPD_SCK_PIN    GPIO_NUM_12
#define EPD_MOSI_PIN   GPIO_NUM_13

static EventGroupHandle_t wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

static esp_websocket_client_handle_t ws_client = NULL;
static i2s_chan_handle_t tx_handle = NULL;
static i2s_chan_handle_t rx_handle = NULL;
static esp_codec_dev_handle_t play_dev = NULL;
static esp_codec_dev_handle_t record_dev = NULL;
static spi_device_handle_t epd_spi = NULL;

static bool has_new_message = false;
static uint8_t *epd_framebuffer = NULL; // 15000 bytes (400*300/8)

// ── EPD 硬件 SPI 与底层指令 ─────────────────────────────────────
static void epd_wait_busy(const char *stage) {
    vTaskDelay(pdMS_TO_TICKS(25)); // 硬件芯片响应延迟：必须先给SSD1683留出拉低BUSY的时间
    int waited = 0;
    while (gpio_get_level(EPD_BUSY_PIN) == 0) {
        vTaskDelay(pdMS_TO_TICKS(10));
        waited += 10;
        if (waited > 6000) {
            ESP_LOGE(TAG, "EPD busy timeout at stage: %s", stage);
            break;
        }
    }
    ESP_LOGI(TAG, "EPD %s done (waited %d ms)", stage, waited);
}

static void epd_send_cmd(uint8_t cmd) {
    gpio_set_level(EPD_DC_PIN, 0);
    gpio_set_level(EPD_CS_PIN, 0);
    spi_transaction_t t = {
        .length = 8,
        .tx_buffer = &cmd,
    };
    spi_device_polling_transmit(epd_spi, &t);
    gpio_set_level(EPD_CS_PIN, 1);
}

static void epd_send_data(uint8_t data) {
    gpio_set_level(EPD_DC_PIN, 1);
    gpio_set_level(EPD_CS_PIN, 0);
    spi_transaction_t t = {
        .length = 8,
        .tx_buffer = &data,
    };
    spi_device_polling_transmit(epd_spi, &t);
    gpio_set_level(EPD_CS_PIN, 1);
}

static void epd_write_bytes(const uint8_t *data, size_t len) {
    gpio_set_level(EPD_DC_PIN, 1);
    gpio_set_level(EPD_CS_PIN, 0);
    spi_transaction_t t = {
        .length = 8 * len,
        .tx_buffer = data,
    };
    spi_device_polling_transmit(epd_spi, &t);
    gpio_set_level(EPD_CS_PIN, 1);
}

static void epd_bsp_init(void) {
    gpio_config_t epd_pins = {
        .pin_bit_mask = (1ULL << EPD_PWR_PIN) | (1ULL << EPD_RST_PIN) | (1ULL << EPD_DC_PIN) | (1ULL << EPD_CS_PIN),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&epd_pins);
    gpio_set_level(EPD_PWR_PIN, 1);
    gpio_set_level(EPD_CS_PIN, 1);

    gpio_config_t busy_pin = {
        .pin_bit_mask = (1ULL << EPD_BUSY_PIN),
        .mode = GPIO_MODE_INPUT,
    };
    gpio_config(&busy_pin);

    spi_bus_config_t buscfg = {
        .miso_io_num = -1,
        .mosi_io_num = EPD_MOSI_PIN,
        .sclk_io_num = EPD_SCK_PIN,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4000,
    };
    spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO);

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = 20 * 1000 * 1000,
        .mode = 0,
        .spics_io_num = -1, // 软件控制 CS
        .queue_size = 7,
    };
    spi_bus_add_device(SPI3_HOST, &devcfg, &epd_spi);

    epd_framebuffer = heap_caps_malloc(15000, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!epd_framebuffer) {
        epd_framebuffer = malloc(15000);
    }
    memset(epd_framebuffer, 0xFF, 15000);
    ESP_LOGI(TAG, "EPD SPI & Framebuffer initialized");
}

static void epd_power_init(void) {
    gpio_hold_dis(EPD_PWR_PIN);
    gpio_set_level(EPD_PWR_PIN, 1);
    gpio_hold_en(EPD_PWR_PIN);
    vTaskDelay(pdMS_TO_TICKS(20));

    gpio_set_level(EPD_RST_PIN, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(EPD_RST_PIN, 0);
    vTaskDelay(pdMS_TO_TICKS(30));
    gpio_set_level(EPD_RST_PIN, 1);
    vTaskDelay(pdMS_TO_TICKS(20));
    epd_wait_busy("init_rst");

    epd_send_cmd(0x00);
    epd_send_data(0x2F);
    epd_send_data(0x2E);
    epd_send_cmd(0xE9);
    epd_send_data(0x01);
    epd_wait_busy("init_reg");
}

static void pack_1bpp_to_ssd1683(uint8_t in, uint8_t *out0, uint8_t *out1) {
    uint16_t v = 0;
    for (int bit = 0; bit < 8; bit++) {
        int sb = 7 - bit;
        v |= ((uint16_t)((in >> sb) & 1)) << (2 * sb);
    }
    *out0 = (uint8_t)(v >> 8);
    *out1 = (uint8_t)(v & 0xFF);
}

// 刷新整个墨水屏
static void epd_refresh_display(const uint8_t *bitmap_1bpp) {
    ESP_LOGI(TAG, "Starting EPD refresh...");
    epd_power_init();

    // 写入温度偏置（室温偏置 0xF1 = 241，SSD1683 驱动电子纸全刷必需）
    epd_send_cmd(0xE0);
    epd_send_data(0x02);
    epd_send_cmd(0xE6);
    epd_send_data(241);

    // 0xA5 Master Activation LUT
    epd_send_cmd(0xA5);
    epd_wait_busy("lut");
    vTaskDelay(pdMS_TO_TICKS(10));

    epd_send_cmd(0x10);
    uint8_t line[100]; // 50 bytes * 2
    for (int y = 0; y < 300; y++) {
        const uint8_t *src = bitmap_1bpp + y * 50;
        for (int xb = 0; xb < 50; xb++) {
            pack_1bpp_to_ssd1683(src[xb], &line[2 * xb], &line[2 * xb + 1]);
        }
        epd_write_bytes(line, 100);
    }

    // Refresh & Power off
    epd_send_cmd(0x04); // power on
    epd_wait_busy("pwr_on");
    epd_send_cmd(0x12); // display refresh
    epd_send_data(0x00);
    epd_wait_busy("display_refresh"); // 等待物理电泳颗粒迁移翻转完成(~3s)
    epd_send_cmd(0x02); // power off
    epd_send_data(0x00);
    epd_wait_busy("pwr_off");

    gpio_hold_dis(EPD_PWR_PIN);
    gpio_set_level(EPD_PWR_PIN, 0); // 彻底断开 EPD 供电进入极低功耗
    gpio_hold_en(EPD_PWR_PIN);
    ESP_LOGI(TAG, "EPD refresh done!");
}

static void fetch_and_refresh_screen(void) {
    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d/api/screen/latest.bin", g_server_host, g_server_port);
    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 5000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_err_t err = esp_http_client_open(client, 0);
    if (err == ESP_OK) {
        int content_len = esp_http_client_fetch_headers(client);
        if (content_len == 15000) {
            int total_read = 0;
            while (total_read < 15000) {
                int r = esp_http_client_read(client, (char *)epd_framebuffer + total_read, 15000 - total_read);
                if (r <= 0) break;
                total_read += r;
            }
            if (total_read == 15000) {
                epd_refresh_display(epd_framebuffer);
            }
        }
    }
    esp_http_client_cleanup(client);
}

#include "local_canvas.h"

static int g_unread_count = 0;

static void refresh_screen_locally(void) {
    bool chg = false;
    int pct = read_battery_percent(&chg);
    int vol_level = (g_current_volume <= 60) ? 1 : ((g_current_volume >= 95) ? 3 : 2);
    int rssi = -55; // 默认良好

    ESP_LOGI(TAG, "Rendering screen locally! unread=%d, batt=%d%%, vol=%d", g_unread_count, pct, vol_level);
    canvas_render_ui(epd_framebuffer, g_unread_count, pct, chg, vol_level, rssi);
    epd_refresh_display(epd_framebuffer);
}

static QueueHandle_t epd_task_queue = NULL;

static void epd_refresh_task(void *pvParameters) {
    uint8_t dummy;
    while (1) {
        if (xQueueReceive(epd_task_queue, &dummy, portMAX_DELAY) == pdTRUE) {
            ESP_LOGI(TAG, "Worker task refreshing screen from local canvas...");
            refresh_screen_locally();
        }
    }
}

static void trigger_screen_refresh(void) {
    if (epd_task_queue) {
        uint8_t dummy = 1;
        xQueueSend(epd_task_queue, &dummy, 0);
    }
}

// ── 电源与基础 GPIO 初始化 ─────────────────────────────────────
static void bsp_power_init(void) {
    gpio_config_t pwr_cfg = {
        .pin_bit_mask = (1ULL << VBAT_PWR_PIN) | (1ULL << AUDIO_PWR_PIN) | (1ULL << PA_CTRL_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&pwr_cfg);
    gpio_hold_dis(VBAT_PWR_PIN);
    gpio_set_level(VBAT_PWR_PIN, 1);   // 自锁开机供电
    gpio_hold_en(VBAT_PWR_PIN);

    gpio_hold_dis(AUDIO_PWR_PIN);
    gpio_set_level(AUDIO_PWR_PIN, 1);  // 开启音频及I2C供电
    gpio_hold_en(AUDIO_PWR_PIN);

    gpio_hold_dis(PA_CTRL_PIN);
    gpio_set_level(PA_CTRL_PIN, 0);    // 默认关闭功放静音
    gpio_hold_en(PA_CTRL_PIN);

    gpio_config_t led_cfg = {
        .pin_bit_mask = (1ULL << LED_PIN),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&led_cfg);
    gpio_set_level(LED_PIN, 1); // 灭

    gpio_config_t key_cfg = {
        .pin_bit_mask = (1ULL << KEY_ENTER_PIN) | (1ULL << KEY_UP_PIN) | (1ULL << KEY_DOWN_PIN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&key_cfg);

    ESP_LOGI(TAG, "BSP Power initialized (VBAT locked, Audio rail ON)");
}

// ── 音频子系统初始化 (ES8311 + I2S) ───────────────────────────
static void bsp_audio_init(void) {
    i2c_master_bus_config_t i2c_bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = I2C_SDA_PIN,
        .scl_io_num = I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t i2c_bus = NULL;
    ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus));

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear_after_cb = true;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &tx_handle, &rx_handle));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_MCLK_PIN,
            .bclk = I2S_BCLK_PIN,
            .ws   = I2S_WS_PIN,
            .dout = I2S_DOUT_PIN,
            .din  = I2S_DIN_PIN,
        },
    };
    std_cfg.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx_handle, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx_handle, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(tx_handle));
    ESP_ERROR_CHECK(i2s_channel_enable(rx_handle));

    audio_codec_i2c_cfg_t i2c_cfg = {
        .port = I2C_NUM_0,
        .addr = ES8311_CODEC_DEFAULT_ADDR,
        .bus_handle = i2c_bus,
    };
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg);

    audio_codec_i2s_cfg_t i2s_cfg = {
        .port = I2S_NUM_0,
        .rx_handle = rx_handle,
        .tx_handle = tx_handle,
    };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_cfg);

    es8311_codec_cfg_t es_cfg = {
        .ctrl_if = ctrl_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_BOTH,
        .pa_pin = -1,
        .use_mclk = true,
        .hw_gain = {
            .pa_voltage = 5.0,
            .codec_dac_voltage = 3.3,
        },
    };
    const audio_codec_if_t *codec_if = es8311_codec_new(&es_cfg);

    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN_OUT,
        .codec_if = codec_if,
        .data_if = data_if,
    };
    play_dev = esp_codec_dev_new(&dev_cfg);
    record_dev = play_dev;

    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16,
        .channel = 1,
        .sample_rate = 16000,
    };
    esp_codec_dev_open(play_dev, &fs);
    esp_codec_dev_set_out_vol(play_dev, g_current_volume);
    esp_codec_dev_set_in_gain(record_dev, 30.0);

    ESP_LOGI(TAG, "Audio ES8311 initialized successfully!");
}

// ── 本地音频预缓存体系 (PSRAM 预下载 + 重复播放支持) ─────────────
#define AUDIO_PRELOAD_MAX_BYTES (16000 * 2 * 60) // 最多支持 60 秒音频 (约 1.9MB)，Note4 具备 8MB PSRAM 绰绰有余
static uint8_t *g_preload_audio_buf = NULL;
static size_t g_preload_audio_len = 0;
static bool g_preload_is_ready = false;
static bool g_preload_in_progress = false;
static int g_current_audio_id = 0;
static TaskHandle_t g_preload_task_handle = NULL;

static void preload_audio_task(void *pvParameters) {
    g_preload_in_progress = true;
    ESP_LOGI(TAG, "Background preloading latest audio from server...");

    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d/api/audio/latest.wav", g_server_host, g_server_port);

    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 8000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Preload failed to open HTTP: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        g_preload_in_progress = false;
        vTaskDelete(NULL);
        return;
    }

    esp_http_client_fetch_headers(client);

    if (!g_preload_audio_buf) {
        g_preload_audio_buf = heap_caps_malloc(AUDIO_PRELOAD_MAX_BYTES, MALLOC_CAP_SPIRAM);
    }

    if (!g_preload_audio_buf) {
        ESP_LOGE(TAG, "Failed to allocate PSRAM for audio preload!");
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        g_preload_in_progress = false;
        vTaskDelete(NULL);
        return;
    }

    // 过滤掉 44 字节 WAV 头，直接在 PSRAM 里存纯 PCM 数据，播放时零耗时解头
    int header_skip = 44;
    char skip_buf[64];
    while (header_skip > 0) {
        int r = esp_http_client_read(client, skip_buf, header_skip > sizeof(skip_buf) ? sizeof(skip_buf) : header_skip);
        if (r <= 0) break;
        header_skip -= r;
    }

    size_t total_read = 0;
    int r = 0;
    while (total_read < AUDIO_PRELOAD_MAX_BYTES &&
           (r = esp_http_client_read(client, (char *)g_preload_audio_buf + total_read, 2048)) > 0) {
        total_read += r;
    }

    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    g_preload_audio_len = total_read;
    g_preload_is_ready = (total_read > 0);
    g_preload_in_progress = false;
    ESP_LOGI(TAG, "Audio preload finished! Size: %d bytes (ready=%d)", (int)total_read, g_preload_is_ready);

    vTaskDelete(NULL);
}

static void start_preload_latest_audio(void) {
    if (g_preload_in_progress) {
        ESP_LOGW(TAG, "Audio preload already in progress, skipping...");
        return;
    }
    g_preload_is_ready = false;
    xTaskCreatePinnedToCore(preload_audio_task, "audio_preload", 4096, NULL, 5, &g_preload_task_handle, 1);
}

// ── 播放留言 (优先毫秒级直读本地 PSRAM 缓存，支持反复聆听) ──────
static void play_latest_audio(void) {
    ESP_LOGI(TAG, "Starting audio playback (preloaded=%d, len=%d)...", g_preload_is_ready, (int)g_preload_audio_len);

    // 1. 如果后台预下载正好在进行中，稍等最多 500ms
    int wait_cycles = 0;
    while (g_preload_in_progress && wait_cycles < 25) {
        vTaskDelay(pdMS_TO_TICKS(20));
        wait_cycles++;
    }

    gpio_hold_dis(PA_CTRL_PIN);
    gpio_set_level(PA_CTRL_PIN, 1); // 开启音频功放
    gpio_hold_en(PA_CTRL_PIN);

    // 2. 命中本地缓存：0 网络延迟极速播放！
    if (g_preload_is_ready && g_preload_audio_buf && g_preload_audio_len > 0) {
        ESP_LOGI(TAG, "Playing directly from local PSRAM buffer (Instant Start)!");
        size_t offset = 0;
        while (offset < g_preload_audio_len) {
            size_t chunk = (g_preload_audio_len - offset > 2048) ? 2048 : (g_preload_audio_len - offset);
            esp_codec_dev_write(play_dev, g_preload_audio_buf + offset, chunk);
            offset += chunk;
        }
    } else {
        // 3. Fallback 回退网络边下边播
        ESP_LOGI(TAG, "Fallback to HTTP streaming playback...");
        char url[128];
        snprintf(url, sizeof(url), "http://%s:%d/api/audio/latest.wav", g_server_host, g_server_port);

        esp_http_client_config_t config = {
            .url = url,
            .timeout_ms = 5000,
        };
        esp_http_client_handle_t client = esp_http_client_init(&config);
        if (esp_http_client_open(client, 0) == ESP_OK) {
            esp_http_client_fetch_headers(client);
            char *buf = malloc(1024);
            int read_bytes = 0;
            int header_skip = 44;
            while (header_skip > 0) {
                int r = esp_http_client_read(client, buf, header_skip > 1024 ? 1024 : header_skip);
                if (r <= 0) break;
                header_skip -= r;
            }
            while ((read_bytes = esp_http_client_read(client, buf, 1024)) > 0) {
                esp_codec_dev_write(play_dev, buf, read_bytes);
            }
            free(buf);
            esp_http_client_close(client);
        }
        esp_http_client_cleanup(client);
    }

    vTaskDelay(pdMS_TO_TICKS(100));
    gpio_hold_dis(PA_CTRL_PIN);
    gpio_set_level(PA_CTRL_PIN, 0); // 关闭功放防底噪
    gpio_hold_en(PA_CTRL_PIN);

    has_new_message = false;

    // 4. 反复听逻辑支持：
    // 若当前仍有未读信封，则听完后消减一封，并向服务端上报已读；
    // 缓存 g_preload_audio_buf 并不清空，支持孩子在读完后反复按键重听！
    if (g_unread_count > 0) {
        g_unread_count--;
        trigger_screen_refresh();
        if (ws_client && esp_websocket_client_is_connected(ws_client)) {
            esp_websocket_client_send_text(ws_client, "{\"action\":\"played\"}", 19, portMAX_DELAY);
        }
    }
    ESP_LOGI(TAG, "Playback completed! Remaining unread locally: %d", g_unread_count);
}

static void play_beep_tone(int freq_hz, int duration_ms);

// ── 录音并上传 ─────────────────────────────────────────────────
static void record_and_upload(void) {
    ESP_LOGI(TAG, "Recording started...");
    gpio_set_level(LED_PIN, 0); // 亮起录音指示灯

    // 播放录音开始提示音：超清脆 40ms 单声微哔 (1000Hz 40ms)
    play_beep_tone(1000, 40);

    if (ws_client && esp_websocket_client_is_connected(ws_client)) {
        esp_websocket_client_send_text(ws_client, "{\"action\":\"recording_start\"}", 29, portMAX_DELAY);
    }

    size_t max_bytes = 16000 * 2 * 15; // 最多录制 15 秒语音 (原10秒扩展，满足完整表达)
    uint8_t *rec_buf = heap_caps_malloc(max_bytes + 44, MALLOC_CAP_SPIRAM);
    if (!rec_buf) {
        gpio_set_level(LED_PIN, 1);
        return;
    }

    size_t recorded_pcm = 0;
    while (gpio_get_level(KEY_ENTER_PIN) == 0 && recorded_pcm < max_bytes) {
        size_t chunk = 1024;
        esp_codec_dev_read(record_dev, rec_buf + 44 + recorded_pcm, chunk);
        recorded_pcm += chunk;
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    gpio_set_level(LED_PIN, 1); // 立即熄灭录音灯，体感无任何拖泥带水

    // 录音有效性防御：若录音小于 0.3 秒（纯误触），则静默放弃上传并短哔提示
    if (recorded_pcm < 16000 * 2 * 0.3) {
        ESP_LOGW(TAG, "Recording too short (<0.3s), discarded as accidental press.");
        play_beep_tone(400, 50); // 低音提示无效
        free(rec_buf);
        return;
    }

    // 播放录音成功结束提示音：清脆对讲机下行送出音 (900Hz 50ms + 600Hz 60ms)
    play_beep_tone(900, 50);
    play_beep_tone(600, 60);

    ESP_LOGI(TAG, "Recording stopped. Uploading %d bytes...", recorded_pcm);

    // 填充 44 字节 WAV 头
    uint32_t total_data_len = recorded_pcm;
    uint32_t total_file_len = total_data_len + 36;
    uint32_t byte_rate = 16000 * 1 * 2;
    memcpy(rec_buf, "RIFF", 4);
    memcpy(rec_buf + 4, &total_file_len, 4);
    memcpy(rec_buf + 8, "WAVEfmt ", 8);
    uint32_t subchunk1_size = 16;
    uint16_t audio_fmt = 1, num_channels = 1;
    uint32_t sample_rate = 16000;
    uint16_t block_align = 2, bits_per_sample = 16;
    memcpy(rec_buf + 16, &subchunk1_size, 4);
    memcpy(rec_buf + 20, &audio_fmt, 2);
    memcpy(rec_buf + 22, &num_channels, 2);
    memcpy(rec_buf + 24, &sample_rate, 4);
    memcpy(rec_buf + 28, &byte_rate, 4);
    memcpy(rec_buf + 32, &block_align, 2);
    memcpy(rec_buf + 34, &bits_per_sample, 2);
    memcpy(rec_buf + 36, "data", 4);
    memcpy(rec_buf + 40, &total_data_len, 4);

    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d/api/upload_audio", g_server_host, g_server_port);
    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 10000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_http_client_set_header(client, "Content-Type", "audio/wav");
    esp_http_client_set_post_field(client, (const char *)rec_buf, recorded_pcm + 44);

    esp_http_client_perform(client);
    esp_http_client_cleanup(client);
    free(rec_buf);
}

static void send_device_status(void) {
    if (ws_client && esp_websocket_client_is_connected(ws_client)) {
        bool chg = false;
        int pct = read_battery_percent(&chg);
        int vol_level = 2; // 默认中
        if (g_current_volume <= 60) vol_level = 1;
        else if (g_current_volume >= 95) vol_level = 3;
        else vol_level = 2;

        char msg[128];
        snprintf(msg, sizeof(msg), "{\"action\":\"status_report\",\"battery\":%d,\"charging\":%s,\"volume\":%d}",
                 pct, chg ? "true" : "false", vol_level);
        esp_websocket_client_send_text(ws_client, msg, strlen(msg), portMAX_DELAY);
    }
}

// 电池定时检测与低电量自动休眠保护任务 (超低功耗设计：5分钟心跳 + 电量突变即时上报)
static void battery_monitor_task(void *pvParameters) {
    int check_interval_sec = 300; // 5 分钟 (300s) 定时检测一次，大幅降低 CPU 唤醒与 Wi-Fi 发包频次
    int last_reported_pct = -1;
    bool last_reported_chg = false;

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(check_interval_sec * 1000));

        bool chg = false;
        int pct = read_battery_percent(&chg);

        // 仅当电量变化达到 2% 以上、或充电状态发生切换、或首次启动时，才发包上报
        if (last_reported_pct < 0 || abs(pct - last_reported_pct) >= 2 || chg != last_reported_chg) {
            send_device_status();
            last_reported_pct = pct;
            last_reported_chg = chg;
        }

        // 终极防过放保护：当电量 <= 5% 且没有在充电时
        if (pct <= 5 && !chg) {
            ESP_LOGW(TAG, "Battery CRITICAL (%d%%)! Rendering shutdown screen and entering deep sleep...", pct);
            refresh_screen_locally();

            vTaskDelay(pdMS_TO_TICKS(3000)); // 等待墨水屏全刷物理完成

            // 彻底关断全部外设供电
            gpio_set_level(PA_CTRL_PIN, 0);
            gpio_set_level(AUDIO_PWR_PIN, 0);
            gpio_set_level(LED_PIN, 1);

            // 进入深度睡眠保护电池（功耗仅约 10μA）
            esp_deep_sleep_start();
        }
    }
}

// ── WebSocket 监听与分发 ───────────────────────────────────────
static void websocket_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data) {
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;
    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI(TAG, "Connected to Feishu Bridge WebSocket!");
            send_device_status();
            trigger_screen_refresh(); // 连上时刷新默认就绪画面
            break;
        case WEBSOCKET_EVENT_DATA:
            if (data->op_code == 0x01) {
                char *json_str = malloc(data->data_len + 1);
                memcpy(json_str, data->data_ptr, data->data_len);
                json_str[data->data_len] = '\0';

                cJSON *root = cJSON_Parse(json_str);
                if (root) {
                    cJSON *type = cJSON_GetObjectItem(root, "type");
                    cJSON *unread = cJSON_GetObjectItem(root, "unread_count");
                    if (unread && cJSON_IsNumber(unread)) {
                        g_unread_count = unread->valueint;
                    }
                    if (type) {
                        if (strcmp(type->valuestring, "new_audio") == 0) {
                            has_new_message = true;
                            // 保持克制与极低功耗：来消息时不常亮 LED 灯，仅由墨水屏画面呈现信封
                            trigger_screen_refresh();
                            // 收到新语音通知时，立即在后台异步预下载音频到本地 PSRAM 缓存！
                            start_preload_latest_audio();
                        } else if (strcmp(type->valuestring, "refresh_screen") == 0) {
                            trigger_screen_refresh();
                        }
                    }
                    cJSON_Delete(root);
                }
                free(json_str);
            }
            break;
        case WEBSOCKET_EVENT_DISCONNECTED:
            ESP_LOGW(TAG, "WebSocket Disconnected!");
            break;
        default:
            break;
    }
}

// ── NVS 凭据加载与存储 ─────────────────────────────────────────
static void load_credentials(void) {
    nvs_handle_t my_handle;
    if (nvs_open("nvs_config", NVS_READWRITE, &my_handle) == ESP_OK) {
        size_t required_size = sizeof(g_wifi_ssid);
        if (nvs_get_str(my_handle, "ssid", g_wifi_ssid, &required_size) == ESP_OK) {
            ESP_LOGI(TAG, "Loaded SSID from NVS: %s", g_wifi_ssid);
        }
        required_size = sizeof(g_wifi_pass);
        if (nvs_get_str(my_handle, "pass", g_wifi_pass, &required_size) == ESP_OK) {
            ESP_LOGI(TAG, "Loaded Password from NVS");
        }
        required_size = sizeof(g_server_host);
        if (nvs_get_str(my_handle, "srv", g_server_host, &required_size) == ESP_OK) {
            ESP_LOGI(TAG, "Loaded Server host from NVS: %s", g_server_host);
        }
        nvs_close(my_handle);
    }
}

static void save_credentials(const char *ssid, const char *pass, const char *srv) {
    nvs_handle_t my_handle;
    if (nvs_open("nvs_config", NVS_READWRITE, &my_handle) == ESP_OK) {
        if (ssid && strlen(ssid) > 0) nvs_set_str(my_handle, "ssid", ssid);
        if (pass) nvs_set_str(my_handle, "pass", pass);
        if (srv && strlen(srv) > 0) nvs_set_str(my_handle, "srv", srv);
        nvs_commit(my_handle);
        nvs_close(my_handle);
        ESP_LOGI(TAG, "Credentials committed to NVS!");
    }
}

// ── Web 配网服务 ────────────────────────────────────────────────
static const char *PROV_HTML = 
"<!DOCTYPE html><html><head><meta charset='utf-8'>"
"<meta name='viewport' content='width=device-width,initial-scale=1,user-scalable=no'>"
"<title>留言机 Wi-Fi 配置</title>"
"<style>"
"body{font-family:-apple-system,sans-serif;background:#f4f6f8;margin:0;padding:20px;color:#333;}"
".card{background:#fff;border-radius:12px;padding:24px;box-shadow:0 4px 16px rgba(0,0,0,0.06);max-width:360px;margin:10px auto;}"
"h2{text-align:center;margin-top:0;font-size:20px;color:#1f2329;}"
".sub{text-align:center;color:#646a73;font-size:13px;margin-bottom:20px;}"
"label{display:block;font-size:14px;font-weight:500;margin-bottom:6px;}"
"input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #dee0e3;border-radius:8px;font-size:15px;margin-bottom:16px;outline:none;}"
"input:focus{border-color:#3370ff;}"
"button{width:100%;padding:14px;background:#3370ff;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;}"
".tip{font-size:12px;color:#8f959e;text-align:center;margin-top:16px;line-height:1.5;}"
"</style></head><body><div class='card'>"
"<h2>💌 留言机联网配置</h2>"
"<div class='sub'>请输入家里的 Wi-Fi 账号密码</div>"
"<form action='/save' method='POST'>"
"<label>Wi-Fi 名称 (SSID)</label><input type='text' name='ssid' placeholder='输入 WiFi 名字' required>"
"<label>Wi-Fi 密码</label><input type='password' name='pass' placeholder='无密码可留空'>"
"<label>服务器地址</label><input type='text' name='srv' value='192.168.8.13' required>"
"<button type='submit'>保存并重启连接</button>"
"</form>"
"<div class='tip'>点击后设备将自动重启并连上 Wi-Fi<br>屏幕会自动恢复为纯白板</div>"
"</div></body></html>";

static esp_err_t prov_root_handler(httpd_req_t *req) {
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send(req, PROV_HTML, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

static void delayed_restart_task(void *pvParameters) {
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
}

static esp_err_t prov_save_handler(httpd_req_t *req) {
    char buf[256] = {0};
    int ret, remaining = req->content_len;
    if (remaining >= sizeof(buf)) remaining = sizeof(buf) - 1;
    ret = httpd_req_recv(req, buf, remaining);
    if (ret <= 0) {
        httpd_resp_send_500(req);
        return ESP_FAIL;
    }
    buf[ret] = '\0';
    ESP_LOGI(TAG, "Received form post: %s", buf);

    // 简单解析 application/x-www-form-urlencoded
    char ssid[64] = {0}, pass[64] = {0}, srv[64] = {0};
    char *p = strtok(buf, "&");
    while (p) {
        char *eq = strchr(p, '=');
        if (eq) {
            *eq = '\0';
            char *key = p;
            char *val = eq + 1;
            if (strcmp(key, "ssid") == 0) strncpy(ssid, val, sizeof(ssid) - 1);
            else if (strcmp(key, "pass") == 0) strncpy(pass, val, sizeof(pass) - 1);
            else if (strcmp(key, "srv") == 0) strncpy(srv, val, sizeof(srv) - 1);
        }
        p = strtok(NULL, "&");
    }

    if (strlen(ssid) > 0) {
        save_credentials(ssid, pass, srv);
        const char *resp = "<html><body style='text-align:center;padding:40px;font-family:sans-serif;'><h2>✅ 保存成功！</h2><p>留言机正在重启连接新 Wi-Fi...</p></body></html>";
        httpd_resp_set_type(req, "text/html; charset=utf-8");
        httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);

        xTaskCreate(delayed_restart_task, "restart_task", 2048, NULL, 5, NULL);
        return ESP_OK;
    }

    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "SSID is required");
    return ESP_FAIL;
}

static httpd_handle_t start_webserver(void) {
    httpd_handle_t server = NULL;
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = 8;
    if (httpd_start(&server, &config) == ESP_OK) {
        httpd_uri_t root_uri = {
            .uri = "/",
            .method = HTTP_METHOD_GET,
            .handler = prov_root_handler,
        };
        httpd_register_uri_handler(server, &root_uri);

        httpd_uri_t save_uri = {
            .uri = "/save",
            .method = HTTP_METHOD_POST,
            .handler = prov_save_handler,
        };
        httpd_register_uri_handler(server, &save_uri);
    }
    return server;
}

static void start_provisioning_mode(void) {
    g_in_prov_mode = true;
    ESP_LOGW(TAG, "Entering Wi-Fi Provisioning (SoftAP) Mode...");

    // 屏幕显示配网提示页面
    epd_refresh_display(PROV_SCREEN_BITMAP);

    esp_wifi_stop();
    esp_netif_create_default_wifi_ap();

    wifi_config_t ap_config = {
        .ap = {
            .ssid = "Note4-Voice",
            .ssid_len = strlen("Note4-Voice"),
            .channel = 1,
            .password = "",
            .max_connection = 4,
            .authmode = WIFI_AUTH_OPEN,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    start_webserver();
    ESP_LOGI(TAG, "SoftAP 'Note4-Voice' started at 192.168.4.1. Waiting for user setup...");

    // 阻塞保持 SoftAP 运行
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// ── Wi-Fi ──────────────────────────────────────────────────────
static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        g_wifi_retry_count++;
        ESP_LOGW(TAG, "WiFi disconnected (attempt %d/5)...", g_wifi_retry_count);
        if (g_wifi_retry_count < 5) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            esp_wifi_connect();
        } else {
            ESP_LOGE(TAG, "Cannot connect to WiFi! Fallback to SoftAP provisioning...");
            start_provisioning_mode();
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        g_wifi_retry_count = 0;
        xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void) {
    load_credentials();

    wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &instance_got_ip));

    wifi_config_t wifi_config = {0};
    strncpy((char *)wifi_config.sta.ssid, g_wifi_ssid, sizeof(wifi_config.sta.ssid) - 1);
    strncpy((char *)wifi_config.sta.password, g_wifi_pass, sizeof(wifi_config.sta.password) - 1);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    // 启用 Wi-Fi DTIM Modem-sleep 省电模式 (与 AP 协商 Beacon 唤醒，待机电流立降 70%)
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
}

// 播放一段温和的提示音 (简单正弦波 PCM)
static void play_beep_tone(int freq_hz, int duration_ms) {
    if (!play_dev) return;
    int samples = (16000 * duration_ms) / 1000;
    int16_t *beep_buf = malloc(samples * 2);
    if (!beep_buf) return;
    for (int i = 0; i < samples; i++) {
        // 简单三角波/方波拟合温和提示音
        beep_buf[i] = ((i * freq_hz / 16000) % 2 == 0) ? 4000 : -4000;
    }
    gpio_hold_dis(PA_CTRL_PIN);
    gpio_set_level(PA_CTRL_PIN, 1);
    gpio_hold_en(PA_CTRL_PIN);

    esp_codec_dev_write(play_dev, beep_buf, samples * 2);

    vTaskDelay(pdMS_TO_TICKS(50));
    gpio_hold_dis(PA_CTRL_PIN);
    gpio_set_level(PA_CTRL_PIN, 0);
    gpio_hold_en(PA_CTRL_PIN);
    free(beep_buf);
}

// ── 按键任务 ───────────────────────────────────────────────────
static void button_task(void *pvParameters) {
    while (1) {
        // 1. 中间大主键：短按听留言，长按录音 (极速响应：20ms消抖 + 280ms长按判定 = 300ms)
        if (gpio_get_level(KEY_ENTER_PIN) == 0) {
            vTaskDelay(pdMS_TO_TICKS(20));
            if (gpio_get_level(KEY_ENTER_PIN) == 0) {
                int hold_time = 20;
                while (gpio_get_level(KEY_ENTER_PIN) == 0) {
                    vTaskDelay(pdMS_TO_TICKS(20));
                    hold_time += 20;
                    if (hold_time >= 300) {
                        break;
                    }
                }

                if (hold_time >= 300) {
                    record_and_upload();
                } else {
                    while (gpio_get_level(KEY_ENTER_PIN) == 0) {
                        vTaskDelay(pdMS_TO_TICKS(20));
                    }
                    // 智能播放与反复聆听支持：
                    // 只要当前有未读信封，或者本地依然保留着上一条语音的缓存，孩子短按都能反复重听！
                    if (g_unread_count > 0 || (g_preload_is_ready && g_preload_audio_len > 0)) {
                        play_latest_audio();
                    } else {
                        // 真正无任何留言时轻点短按，播放一声轻柔提示音
                        play_beep_tone(600, 40);
                    }
                }
            }
        }

        // 2. 侧上键 (KEY_UP_PIN, GPIO 39): 音量加 (上限 100%)
        if (gpio_get_level(KEY_UP_PIN) == 0) {
            vTaskDelay(pdMS_TO_TICKS(50));
            if (gpio_get_level(KEY_UP_PIN) == 0) {
                if (g_current_volume < 100) {
                    g_current_volume += 20;
                    if (g_current_volume > 100) g_current_volume = 100;
                    esp_codec_dev_set_out_vol(play_dev, g_current_volume);
                    ESP_LOGI(TAG, "Volume UP -> %d%%", g_current_volume);
                    play_beep_tone(800, 100); // 800Hz 提示音
                    send_device_status();
                }
                while (gpio_get_level(KEY_UP_PIN) == 0) {
                    vTaskDelay(pdMS_TO_TICKS(20));
                }
            }
        }

        // 3. 侧下键 (KEY_DOWN_PIN, GPIO 18): 音量减 (安全下限死锁 60%, 屏蔽长按关机)
        if (gpio_get_level(KEY_DOWN_PIN) == 0) {
            vTaskDelay(pdMS_TO_TICKS(50));
            if (gpio_get_level(KEY_DOWN_PIN) == 0) {
                if (g_current_volume > 60) {
                    g_current_volume -= 20;
                    if (g_current_volume < 60) g_current_volume = 60; // 绝对不准低于 60%
                    esp_codec_dev_set_out_vol(play_dev, g_current_volume);
                    ESP_LOGI(TAG, "Volume DOWN -> %d%% (Safety Floor)", g_current_volume);
                    play_beep_tone(500, 100); // 500Hz 低沉提示音
                    send_device_status();
                }
                // 彻底吃掉长按，直到松手才退出，杜绝误触长按
                while (gpio_get_level(KEY_DOWN_PIN) == 0) {
                    vTaskDelay(pdMS_TO_TICKS(20));
                }
            }
        }

        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void app_main(void) {
    ESP_LOGI(TAG, "=== ESP32 Feishu Voice Terminal Starting ===");

    bsp_power_init();

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // 初始化电池监测 ADC
    bsp_battery_init();

    // 初始化 EPD 墨水屏硬件驱动
    epd_bsp_init();

    // 初始化音频
    bsp_audio_init();

    // 启用动态电源管理 (Dynamic Frequency Scaling): 
    // 平时空闲时 CPU 从 240MHz 自动降至 40MHz，大幅降低芯片底噪与发热
    esp_pm_config_t pm_config = {
        .max_freq_mhz = 240,
        .min_freq_mhz = 40,
        .light_sleep_enable = false // 维持 APB 和 WebSocket 时钟
    };
    esp_pm_configure(&pm_config);

    // 连接 Wi-Fi
    wifi_init_sta();
    xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT, false, true, portMAX_DELAY);
    ESP_LOGI(TAG, "WiFi connected!");

    // 创建墨水屏刷新独立任务
    epd_task_queue = xQueueCreate(4, sizeof(uint8_t));
    xTaskCreate(epd_refresh_task, "epd_task", 8192, NULL, 3, NULL);

    // 启动 WebSocket
    char ws_url[128];
    snprintf(ws_url, sizeof(ws_url), "ws://%s:%d/ws/device", g_server_host, g_server_port);
    esp_websocket_client_config_t ws_cfg = {
        .uri = ws_url,
    };
    ws_client = esp_websocket_client_init(&ws_cfg);
    esp_websocket_register_events(ws_client, WEBSOCKET_EVENT_ANY, websocket_event_handler, (void *)ws_client);
    esp_websocket_client_start(ws_client);

    xTaskCreate(button_task, "button_task", 4096, NULL, 5, NULL);
    xTaskCreate(battery_monitor_task, "battery_monitor_task", 4096, NULL, 2, NULL);
}
