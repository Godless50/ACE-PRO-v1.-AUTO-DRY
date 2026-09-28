#pragma once
#include <stddef.h>
#include <stdint.h>
#include "reading.h"

namespace rhcore {

// Разбор уведомления характеристики ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6.
//
// Пять байт: температура int16 LE в сотых долях градуса, влажность uint8
// в процентах, напряжение батареи uint16 LE в милливольтах.
//
// Порт функции decode из feeder/ace_rh_feeder/sensor.py. Возвращает false
// и НЕ трогает out при негодном пакете: короче пяти байт или влажность
// больше 100. Отказ — штатная ситуация, вызывающий просто ничего не
// отправит на принтер.
bool parse_lywsd(const uint8_t* data, size_t len, Reading& out);

}  // namespace rhcore
