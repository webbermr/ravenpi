# ADS-B Military Aircraft Alert Monitor

This Python script actively monitors ADS-B data from a `dump1090` feed, identifies military aircraft or other aircraft of interest, and sends multi-channel alerts. It's designed to be run on a Raspberry Pi or other Linux-based machine connected to an RTL-SDR.

---

## 🚀 Features

* **Real-time Monitoring**: Connects directly to the SBS feed (port 30003) of `dump1090`.
* **Flexible Alerting**: Triggers alerts based on ICAO hex address ranges and specific callsign prefixes.
* **Multi-Channel Notifications**:
    * **Console**: Detailed printout in the terminal.
    * **Web Interface**: A simple, auto-refreshing web page to view the last 25 alerts.
    * **Text-to-Speech**: Spoken alerts with phonetic callsigns using `espeak-ng`.
    * **Push Notifications**: Sends alerts to your phone via [ntfy.sh](https://ntfy.sh/).
* **GPS Integration**: Automatically uses a connected USB GPS dongle for your home location, with a fallback to manually configured coordinates.
* **Persistent Logging**: Logs all triggered alerts to a CSV file for later review.
* **Test Mode**: Includes a `--test` flag to verify all alert channels are working correctly without needing a live aircraft.

---

## 📋 Prerequisites

### Hardware
* A Raspberry Pi (or other Linux computer).
* An RTL-SDR dongle configured to receive ADS-B signals.
* (Optional) A USB GPS dongle for automatic location tracking.

### Software
* **Python 3.9+**
* **dump1090**: Must be running and accessible, providing an SBS feed on port 30003.
* **espeak-ng**: The command-line speech synthesizer.

---

## ⚙️ Installation & Setup

Follow these steps in your terminal to get the script up and running.

**1. Clone the Repository**
```bash
git clone <your-github-repo-url>
cd <your-repo-name>
```

**2. Install System Dependencies**
The script requires the `espeak-ng` package for text-to-speech.
```bash
sudo apt update
sudo apt install espeak-ng -y
```

**3. Create and Activate a Python Virtual Environment**
This keeps the project's dependencies isolated from your system.
```bash
# Create the virtual environment folder named 'venv'
python3 -m venv venv

# Activate it (you must do this every time you open a new terminal)
source venv/bin/activate
```
Your terminal prompt should now start with `(venv)`.

**4. Install Python Libraries**
First, create a file named `requirements.txt` in your project directory with the following content:

```
# requirements.txt
requests
geopy
Flask
pyserial
pynmea2
```

Now, install these libraries using `pip`:
```bash
pip install -r requirements.txt
```

---

## 🔧 Configuration

Before running the script, you need to set up your configuration files and adjust a few variables inside the script itself.

**1. Create Configuration Directory and Files**
The script looks for configuration files in `~/sigint/adsb/mil_alerts/`.
```bash
# Create the directory
mkdir -p ~/sigint/adsb/mil_alerts

# Create the empty config files
touch ~/sigint/adsb/mil_alerts/icao_ranges.csv
touch ~/sigint/adsb/mil_alerts/local_interest.csv
touch ~/sigint/adsb/mil_alerts/military_callsigns.txt
```

**2. Populate Configuration Files**

* **`icao_ranges.csv` and `local_interest.csv`**: Add ICAO hex ranges to these files. The format is `Service/Country,START_HEX,END_HEX`.
    ```csv
    # Example for icao_ranges.csv
    US Air Force,AE0100,AE0847
    US Navy,AE0848,AE098F
    ```

* **`military_callsigns.txt`**: Add callsign prefixes, one per line. The script will alert if an aircraft's callsign starts with any of these.
    ```txt
    # Example for military_callsigns.txt
    RCH
    PAT
    SLAM
    ```

**3. Edit In-Script Variables**
Open the Python script and adjust these variables at the top of the file to match your setup:
```python
# --- GPS and Home Location Configuration ---
FALLBACK_HOME_LAT = 38.95
FALLBACK_HOME_LON = -77.38
GPS_SERIAL_PORT = '/dev/ttyACM0' # On Windows, this might be 'COM3'
```

---

## ▶️ Usage

Make sure you have activated the virtual environment before running the script:
```bash
source venv/bin/activate
```

**To run in normal monitoring mode:**
```bash
python your_script_name.py
```

**To run a one-time test to check all alert systems:**
```bash
python your_script_name.py --test
```

**To view the web interface:**
Open a web browser on another device on your network and go to `http://<your-pi-ip-address>:5001`.
