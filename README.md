# <img src="docs/source/cremalink.png" alt="Cremalink Logo" height="45" style="vertical-align: middle; margin-right: 10px;"> cremalink

**A high-performance Python library and local API server for monitoring and controlling IoT coffee machines.**

[![PyPI version](https://img.shields.io/pypi/v/cremalink.svg?style=for-the-badge&color=blue)](https://pypi.org/project/cremalink/)
[![Python Version](https://img.shields.io/pypi/pyversions/cremalink.svg?style=for-the-badge&color=FFE169&labelColor=3776AB)](https://pypi.org/project/cremalink/)
[![License](https://img.shields.io/github/license/lodzen/cremalink?style=for-the-badge&color=success)](LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/cremalink.svg?style=for-the-badge&color=orange)](https://pypi.org/project/cremalink/)
[![Source Code](https://img.shields.io/badge/Source-GitHub-black?style=for-the-badge&logo=github)](https://github.com/lodzen/cremalink)

---

## ✨ Overview

Cremalink provides a unified interface to interact with smart coffee machines via **Local LAN control** or **Cloud API**. It allows for real-time state monitoring and precise command execution.

> [!TIP]
> For detailed guides, advanced configuration, and developer deep-dives, please visit our **[Project Wiki](https://github.com/lodzen/cremalink/wiki)**.

> [!NOTE] 
> This project was developed with a result-oriented approach, primarily optimized for the De'Longhi PrimaDonna Soul. While the architecture is designed to be extensible, some logic may currently be tightly coupled to this specific model and might not work seamlessly with others yet.
>The goal is to make the library fully generic. If you notice parts that are too specific to the PrimaDonna Soul or encounter issues with other machines, we highly encourage contributions! Refactoring and generalizations are very welcome to improve support for a wider range of devices.

---

## 🚀 Installation

Install the package via `pip` (Cremalink requires **Python 3.13+**):

```bash
pip install cremalink

```

### Optional Dependencies

To include tools for development or testing:

```bash
pip install "cremalink[dev]"   # For notebooks and kernel support
pip install "cremalink[test]"  # For running pytest suites

```

---

## 🛠 Usage

### Integrated API Server

Cremalink includes an aiohttp-based server for headless environments:

```bash
# Start the server
cremalink-server --ip 0.0.0.0 --port 10280 --settings_path "conf.json"
```
> More information: [Local Server Setup](https://github.com/lodzen/cremalink/wiki/3.-Local-Server-Setup)

### Python API (Local Control)

Connect to your machine directly via your local network for the lowest latency.

> More information: [Local Device Usage](https://github.com/lodzen/cremalink/wiki/4.-Local-Device-Usage)

### ECAM protocol layer (`cremalink.ecam`)

The `cremalink.ecam` package exposes the native ECAM protocol used by
PrimaDonna/Eletta-class machines — CRC-verified command builders and
answer parsers, transport-independent (LAN or cloud):

```python
from cremalink.ecam import builder, MachineProfile

profile = MachineProfile.from_map_name("ECAM610")
frame = builder.build_power(builder.PowerCommand.TURN_ON, profile)
wire = builder.encode_for_transport(frame, profile)  # adds the timestamp
```

High-level `Device` methods built on it:

| Method | What it does |
|---|---|
| `brew(beverage_id, recipe=None, ...)` | Parametric `0x83` brew with the machine's recipe bytes |
| `stop_brew()` / `wake()` / `standby()` / `session_refresh()` | Power/session control (`0x83`/`0x84`) |
| `get_statistics()` | Native `0xA2` statistics pager (LAN) or cloud counters per the device map's `statistics_source` |
| `get_profiles()` / `select_profile(index)` | Read occupied profile slots; session-gated `0xA9` selection |
| `get_settings()` / `set_setting(key, index)` | Read/write machine settings (auto-off, water hardness) via `0x95`/`0x90` |
| `read_catalog()` | Parse the recipe catalogue (`b0f0`/`a6f0`/`aaf0`/`a8f0`/`baf0` blobs) |

**Writes are session-gated**: the device announces a ~300 s session
(`device_connected` property write) automatically before any `0x90`/`0xA9`
frame. Callers never manage this themselves — but it means writes take a
round-trip and are LAN-only. Statistics via `0xA2` are likewise LAN-only;
`cloud_counters` maps resolve their counters from the cloud datapoint
snapshot instead.

---

## 🛠 Development

### Testing

Run the comprehensive test suite using `pytest`:

```bash
pytest tests/

```

### Contributing

Contributions are welcome! If you have a machine profile not yet supported, please check the [Wiki: 5. Adding Custom Devices](https://github.com/lodzen/cremalink/wiki/) on how to add new `.json` device definitions.

Currently supported devices:

- `De'Longhi PrimaDonna Soul (ECAM612)`
- `De'Longhi Eletta Explore (ECAM452) (not tested yet)`
---

## ☕ Credits

This project stands on the shoulders of giants. The reverse engineering and implementation is a community effort. A special thanks to the following projects and individuals for their pioneering work and documentation:

### Technical Foundations, Protocol Research & Inspiration
* **[ECAMpy](https://github.com/duckwc/ECAMpy)**
* **[delonghi-comfort-client](https://github.com/rtfpessoa/delonghi-comfort-client)**
* **[Hacking Bluetooth to Brew Coffee](https://grack.com/blog/2022/12/02/hacking-bluetooth-to-brew-coffee-on-github-actions-part-2/)**
* **[delonghi-coffee-link-python](https://github.com/otto-dev/delonghi-coffee-link-python.git)**
* **[DlghIoT](https://framagit.org/mattgk/dlghiot)**
* **[home_assistant_delonghi_primadonna](https://github.com/Arbuzov/home_assistant_delonghi_primadonna)**
* **[longshot](https://github.com/mmastrac/longshot)**

*Is a project missing or do you have suggestions for improvement? Feel free to open a PR or an issue!*

---

## 💫 Star History

[![Star History Chart](https://api.star-history.com/svg?repos=lodzen/cremalink&type=date&logscale&legend=top-left)](https://www.star-history.com/#lodzen/cremalink&type=date&logscale&legend=top-left)

## 📄 License

Distributed under the **AGPL-3.0-or-later** License. See `LICENSE` for more information.

---

*Developed by [Midian Tekle Elfu](mailto:developer@midian.tekleelfu.de). Supported by the community.*
