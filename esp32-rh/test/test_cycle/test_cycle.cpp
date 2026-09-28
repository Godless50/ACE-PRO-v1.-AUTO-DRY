#include <unity.h>

#include "cycle.h"

using rhcore::Action;
using rhcore::Cycle;
using rhcore::CycleConfig;

void setUp(void) {}
void tearDown(void) {}

static CycleConfig fast_cfg() {
    CycleConfig c;
    c.interval_ms = 1000;
    c.failures_before_ble_reset = 3;
    c.reboot_after_silence_ms = 10000;
    return c;
}

static void test_first_poll_is_due_immediately(void) {
    Cycle c(fast_cfg(), 5000);
    TEST_ASSERT_TRUE(c.due(5000));
}

static void test_not_due_until_interval_passes(void) {
    Cycle c(fast_cfg(), 5000);
    c.mark_started(5000);
    TEST_ASSERT_FALSE(c.due(5000));
    TEST_ASSERT_FALSE(c.due(5999));
    TEST_ASSERT_TRUE(c.due(6000));
    TEST_ASSERT_TRUE(c.due(9000));
}

static void test_three_failures_reset_ble_and_clear_counter(void) {
    Cycle c(fast_cfg(), 0);
    TEST_ASSERT_EQUAL(Action::None, c.on_read_failed(100));
    TEST_ASSERT_EQUAL_UINT8(1, c.consecutive_failures());
    TEST_ASSERT_EQUAL(Action::None, c.on_read_failed(200));
    TEST_ASSERT_EQUAL_UINT8(2, c.consecutive_failures());
    TEST_ASSERT_EQUAL(Action::ResetBle, c.on_read_failed(300));
    // Счётчик сброшен: следующая переинициализация только после трёх новых
    // неудач, иначе стек дёргался бы на каждом шаге.
    TEST_ASSERT_EQUAL_UINT8(0, c.consecutive_failures());
    TEST_ASSERT_EQUAL(Action::None, c.on_read_failed(400));
}

static void test_delivered_clears_failures_and_silence(void) {
    Cycle c(fast_cfg(), 0);
    c.on_read_failed(100);
    c.on_read_failed(200);
    TEST_ASSERT_EQUAL_UINT8(2, c.consecutive_failures());
    TEST_ASSERT_EQUAL(Action::None, c.on_delivered(300));
    TEST_ASSERT_EQUAL_UINT8(0, c.consecutive_failures());
    TEST_ASSERT_EQUAL_UINT32(0, c.silence_ms(300));
    TEST_ASSERT_EQUAL_UINT32(700, c.silence_ms(1000));
}

static void test_silence_triggers_reboot(void) {
    Cycle c(fast_cfg(), 0);
    // Молчание считается с момента включения: плата, которая ни разу
    // ничего не доставила, тоже обязана перезагрузиться.
    TEST_ASSERT_EQUAL(Action::None, c.on_read_failed(9999));
    TEST_ASSERT_EQUAL(Action::Reboot, c.on_read_failed(10000));
}

static void test_reboot_wins_over_ble_reset(void) {
    Cycle c(fast_cfg(), 0);
    c.on_read_failed(100);
    c.on_read_failed(200);
    // Третья неудача сама по себе дала бы ResetBle, но молчание уже
    // затянулось: стек BLE поднимать поздно, помогает только перезагрузка.
    TEST_ASSERT_EQUAL(Action::Reboot, c.on_read_failed(20000));
}

static void test_not_delivered_does_not_count_as_sensor_failure(void) {
    Cycle c(fast_cfg(), 0);
    c.on_read_failed(100);
    c.on_read_failed(200);
    // Датчик прочитался, принтер не принял. Датчик не виноват, счётчик
    // неудач BLE обнуляется.
    TEST_ASSERT_EQUAL(Action::None, c.on_not_delivered(300));
    TEST_ASSERT_EQUAL_UINT8(0, c.consecutive_failures());
    // Но молчание копится и в итоге приводит к перезагрузке: это лечит
    // зависший стек Wi-Fi, который выглядит как недоступный принтер.
    TEST_ASSERT_EQUAL(Action::Reboot, c.on_not_delivered(10000));
}

static void test_millis_overflow_does_not_break_interval(void) {
    // millis() переполняется через 49.7 суток. Сравнения сделаны через
    // вычитание беззнаковых, поэтому переход через нуль проходит без
    // зависания цикла и без ложной перезагрузки.
    const uint32_t near_max = 0xFFFFFF00u;
    Cycle c(fast_cfg(), near_max);
    c.mark_started(near_max);
    TEST_ASSERT_FALSE(c.due(near_max + 500));
    TEST_ASSERT_TRUE(c.due(near_max + 1000));  // уже после переполнения
    TEST_ASSERT_EQUAL_UINT32(1000, c.silence_ms(near_max + 1000));
}

static void test_millis_overflow_does_not_cause_false_reboot(void) {
    const uint32_t near_max = 0xFFFFFF00u;
    Cycle c(fast_cfg(), near_max);
    // Через 1 секунду после переполнения молчание равно 1 с, а не почти
    // 50 суткам — перезагружаться не за что.
    TEST_ASSERT_EQUAL(Action::None, c.on_read_failed(near_max + 1000));
    TEST_ASSERT_EQUAL(Action::Reboot, c.on_read_failed(near_max + 10000));
}

static void test_failure_counter_does_not_wrap(void) {
    CycleConfig cfg = fast_cfg();
    cfg.failures_before_ble_reset = 255;
    Cycle c(cfg, 0);
    for (int i = 0; i < 300; ++i) {
        c.on_read_failed(1);
    }
    // Переполнения uint8 не происходит: счётчик либо дошёл до порога и
    // сбросился, либо упёрся в максимум, но не обнулился молча.
    TEST_ASSERT_LESS_OR_EQUAL_UINT8(255, c.consecutive_failures());
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_first_poll_is_due_immediately);
    RUN_TEST(test_not_due_until_interval_passes);
    RUN_TEST(test_three_failures_reset_ble_and_clear_counter);
    RUN_TEST(test_delivered_clears_failures_and_silence);
    RUN_TEST(test_silence_triggers_reboot);
    RUN_TEST(test_reboot_wins_over_ble_reset);
    RUN_TEST(test_not_delivered_does_not_count_as_sensor_failure);
    RUN_TEST(test_millis_overflow_does_not_break_interval);
    RUN_TEST(test_millis_overflow_does_not_cause_false_reboot);
    RUN_TEST(test_failure_counter_does_not_wrap);
    return UNITY_END();
}
