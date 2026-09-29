# Licensing scope / third-party material

Everything in this repository that **we** authored is licensed under **GPL-3.0** (see
`LICENSE`):

* the Klipper modules in `printer/` (`ace_ext_rh.py`, `ace_ext_raw.py`, their CFG files),
* the BLE feeder service in `feeder/`,
* the ESP32 firmware in `esp32-rh/` (including `lib/rhcore/`),
* the Home Assistant package in `homeassistant/`,
* the scripts in `scripts/` and the tests in `tests/`,
* the documentation, including the design/plan documents.

No vendor firmware images are redistributed in this repository — the ACE firmware is
not part of it; only checksums, offsets and patch recipes are described.

Third-party components this work talks to keep their own terms:

* **multiACE** — GPL-3.0. The patch scripts under `scripts/` modify its installed
  `ace.py` / web frontend on the printer; the upstream project and its licence are
  unchanged by that.
* **Klipper**, **Moonraker** — GPL-3.0.
* **OpenCubic CFW** (the firmware base used with this setup) — its authors publish no
  licence file; we claim nothing over it.
* **Xiaomi LYWSD03MMC** — hardware; BLE readings are taken from its stock GATT
  characteristic.
