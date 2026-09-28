#include <unity.h>

#include "payload.h"

using rhcore::Reading;
using rhcore::parse_lywsd;

void setUp(void) {}
void tearDown(void) {}

// Показание, снятое с живого датчика при проектировании (см. спеку):
// 26.95 C, 57 %rH, 3033 мВ. Байты little-endian.
static const uint8_t kLive[5] = {0x87, 0x0A, 0x39, 0xD9, 0x0B};

static void test_live_payload_parsed(void) {
    Reading r{};
    TEST_ASSERT_TRUE(parse_lywsd(kLive, sizeof(kLive), r));
    TEST_ASSERT_EQUAL_INT16(2695, r.temp_centi_c);
    TEST_ASSERT_EQUAL_UINT8(57, r.rh_percent);
    TEST_ASSERT_EQUAL_UINT16(3033, r.battery_mv);
}

static void test_longer_payload_uses_first_five_bytes(void) {
    // Датчик может прислать больше: лишнее игнорируется, как и в
    // серверной реализации на Python (payload[:5]).
    const uint8_t data[8] = {0x87, 0x0A, 0x39, 0xD9, 0x0B, 0xFF, 0xFF, 0xFF};
    Reading r{};
    TEST_ASSERT_TRUE(parse_lywsd(data, sizeof(data), r));
    TEST_ASSERT_EQUAL_UINT8(57, r.rh_percent);
    TEST_ASSERT_EQUAL_UINT16(3033, r.battery_mv);
}

static void test_short_payload_rejected(void) {
    Reading r{};
    TEST_ASSERT_FALSE(parse_lywsd(kLive, 4, r));
    TEST_ASSERT_FALSE(parse_lywsd(kLive, 0, r));
}

static void test_null_payload_rejected(void) {
    Reading r{};
    TEST_ASSERT_FALSE(parse_lywsd(nullptr, 5, r));
}

static void test_humidity_above_hundred_rejected(void) {
    // Мусорное число не должно доехать до принтера и вызвать нагрев.
    uint8_t data[5] = {0x87, 0x0A, 101, 0xD9, 0x0B};
    Reading r{};
    TEST_ASSERT_FALSE(parse_lywsd(data, sizeof(data), r));
    data[2] = 0xFF;
    TEST_ASSERT_FALSE(parse_lywsd(data, sizeof(data), r));
}

static void test_humidity_exactly_hundred_accepted(void) {
    const uint8_t data[5] = {0x87, 0x0A, 100, 0xD9, 0x0B};
    Reading r{};
    TEST_ASSERT_TRUE(parse_lywsd(data, sizeof(data), r));
    TEST_ASSERT_EQUAL_UINT8(100, r.rh_percent);
}

static void test_negative_temperature(void) {
    // -12.34 C = -1234 = 0xFB2E, little-endian 2E FB.
    const uint8_t data[5] = {0x2E, 0xFB, 0x2A, 0x00, 0x00};
    Reading r{};
    TEST_ASSERT_TRUE(parse_lywsd(data, sizeof(data), r));
    TEST_ASSERT_EQUAL_INT16(-1234, r.temp_centi_c);
    TEST_ASSERT_EQUAL_UINT8(42, r.rh_percent);
}

static void test_rejected_payload_leaves_output_untouched(void) {
    Reading r{};
    r.rh_percent = 7;
    r.temp_centi_c = 111;
    r.battery_mv = 222;
    TEST_ASSERT_FALSE(parse_lywsd(kLive, 3, r));
    TEST_ASSERT_EQUAL_UINT8(7, r.rh_percent);
    TEST_ASSERT_EQUAL_INT16(111, r.temp_centi_c);
    TEST_ASSERT_EQUAL_UINT16(222, r.battery_mv);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_live_payload_parsed);
    RUN_TEST(test_longer_payload_uses_first_five_bytes);
    RUN_TEST(test_short_payload_rejected);
    RUN_TEST(test_null_payload_rejected);
    RUN_TEST(test_humidity_above_hundred_rejected);
    RUN_TEST(test_humidity_exactly_hundred_accepted);
    RUN_TEST(test_negative_temperature);
    RUN_TEST(test_rejected_payload_leaves_output_untouched);
    return UNITY_END();
}
