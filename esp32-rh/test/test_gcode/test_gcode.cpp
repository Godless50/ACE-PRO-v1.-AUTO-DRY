#include <unity.h>
#include <string.h>

#include "gcode.h"

using rhcore::Reading;
using rhcore::build_set_rh;
using rhcore::kGcodeBufSize;
using rhcore::round_centi_to_deci;

void setUp(void) {}
void tearDown(void) {}

static void test_typical_command(void) {
    // Ровно та строка, которую сейчас шлёт серверный курьер и которую
    // принимает ace_ext_rh.py на принтере.
    Reading r{58, 2360, 3033};
    char buf[kGcodeBufSize];
    const size_t n = build_set_rh(r, 0, 1800, buf, sizeof(buf));
    TEST_ASSERT_GREATER_THAN_size_t(0, n);
    TEST_ASSERT_EQUAL_STRING("ACE_SET_RH ACE=0 RH=58.0 TTL=1800 TEMP=23.6", buf);
    TEST_ASSERT_EQUAL_size_t(strlen(buf), n);
}

static void test_rounding_half_away_from_zero(void) {
    // Ключевое место: округление считается на целых сотых, поэтому
    // «половина» уходит от нуля предсказуемо, без зависимости от того, как
    // double хранит 26.95.
    TEST_ASSERT_EQUAL_INT16(270, round_centi_to_deci(2695));
    TEST_ASSERT_EQUAL_INT16(269, round_centi_to_deci(2694));
    TEST_ASSERT_EQUAL_INT16(237, round_centi_to_deci(2365));
    TEST_ASSERT_EQUAL_INT16(236, round_centi_to_deci(2364));
    TEST_ASSERT_EQUAL_INT16(-270, round_centi_to_deci(-2695));
    TEST_ASSERT_EQUAL_INT16(-237, round_centi_to_deci(-2365));
    TEST_ASSERT_EQUAL_INT16(0, round_centi_to_deci(0));
    TEST_ASSERT_EQUAL_INT16(1, round_centi_to_deci(5));
    TEST_ASSERT_EQUAL_INT16(-1, round_centi_to_deci(-5));
    TEST_ASSERT_EQUAL_INT16(0, round_centi_to_deci(-4));
}

static void test_live_sensor_value_rounds_up(void) {
    // 26.95 C с живого датчика: должно стать 27.0, а не 26.9. Именно здесь
    // наивное printf("%.1f", 26.95) дало бы 26.9.
    Reading r{57, 2695, 3033};
    char buf[kGcodeBufSize];
    TEST_ASSERT_GREATER_THAN_size_t(0, build_set_rh(r, 0, 1800, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_STRING("ACE_SET_RH ACE=0 RH=57.0 TTL=1800 TEMP=27.0", buf);
}

static void test_negative_temperature_formatting(void) {
    Reading r{40, -1234, 3000};
    char buf[kGcodeBufSize];
    TEST_ASSERT_GREATER_THAN_size_t(0, build_set_rh(r, 0, 1800, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_STRING("ACE_SET_RH ACE=0 RH=40.0 TTL=1800 TEMP=-12.3", buf);
}

static void test_negative_temperature_below_one_degree_keeps_sign(void) {
    // -0.4 C: целая часть равна нулю и знака не несёт, минус печатается
    // отдельно. Без этого получилось бы "0.4" — ошибка в 0.8 градуса.
    Reading r{40, -40, 3000};
    char buf[kGcodeBufSize];
    TEST_ASSERT_GREATER_THAN_size_t(0, build_set_rh(r, 0, 1800, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_STRING("ACE_SET_RH ACE=0 RH=40.0 TTL=1800 TEMP=-0.4", buf);
}

static void test_ace_index_used(void) {
    Reading r{58, 2360, 3033};
    char buf[kGcodeBufSize];
    TEST_ASSERT_GREATER_THAN_size_t(0, build_set_rh(r, 3, 600, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_STRING("ACE_SET_RH ACE=3 RH=58.0 TTL=600 TEMP=23.6", buf);
}

static void test_bad_arguments_produce_nothing(void) {
    Reading r{58, 2360, 3033};
    char buf[kGcodeBufSize];
    // Индекс вне 0..3 — модуль на принтере такой отвергнет.
    TEST_ASSERT_EQUAL_size_t(0, build_set_rh(r, -1, 1800, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_size_t(0, build_set_rh(r, 4, 1800, buf, sizeof(buf)));
    // Срок годности вне 60..7200 — те же границы, что в ace_ext_rh.py.
    TEST_ASSERT_EQUAL_size_t(0, build_set_rh(r, 0, 59, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_size_t(0, build_set_rh(r, 0, 7201, buf, sizeof(buf)));
    // Негодная влажность.
    Reading bad{101, 2360, 3033};
    TEST_ASSERT_EQUAL_size_t(0, build_set_rh(bad, 0, 1800, buf, sizeof(buf)));
    // Нулевой буфер.
    TEST_ASSERT_EQUAL_size_t(0, build_set_rh(r, 0, 1800, nullptr, 16));
    TEST_ASSERT_EQUAL_size_t(0, build_set_rh(r, 0, 1800, buf, 0));
}

static void test_short_buffer_refused_not_truncated(void) {
    // Обрезанная команда была бы хуже отсутствия команды: принтер мог бы
    // принять её с другим сроком годности.
    Reading r{58, 2360, 3033};
    char buf[20];
    TEST_ASSERT_EQUAL_size_t(0, build_set_rh(r, 0, 1800, buf, sizeof(buf)));
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_typical_command);
    RUN_TEST(test_rounding_half_away_from_zero);
    RUN_TEST(test_live_sensor_value_rounds_up);
    RUN_TEST(test_negative_temperature_formatting);
    RUN_TEST(test_negative_temperature_below_one_degree_keeps_sign);
    RUN_TEST(test_ace_index_used);
    RUN_TEST(test_bad_arguments_produce_nothing);
    RUN_TEST(test_short_buffer_refused_not_truncated);
    return UNITY_END();
}
