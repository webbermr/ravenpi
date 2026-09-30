import socket
import time
import sys
import requests
import os
import math
import csv
from datetime import datetime
from geopy.distance import great_circle
from flask import Flask, render_template_string
from threading import Thread, Lock
from collections import deque
import subprocess
import argparse

try:
    import serial
    import pynmea2
    GPS_ENABLED = True
except ImportError:
    GPS_ENABLED = False
    print("Warning: 'pyserial' or 'pynmea2' not found. GPS functionality disabled.")


# --- Configuration ---
DUMP1090_HOST = '127.0.0.1'
DUMP1090_PORT = 30003
NTFY_TOPIC = 'ADSB-ALERTS'
ALERT_TIMEOUT = 600

# --- GPS and Home Location Configuration ---
FALLBACK_HOME_LAT = 38.95
FALLBACK_HOME_LON = -77.38
GPS_SERIAL_PORT = '/dev/ttyUSB0' 
GPS_UPDATE_INTERVAL = 600 

# --- Global variables ---
HOME_LAT = FALLBACK_HOME_LAT
HOME_LON = FALLBACK_HOME_LON
location_lock = Lock() 
GPS_IS_LIVE = False
recent_alerts = deque(maxlen=25)

# --- Path configuration ---
CONFIG_DIR = '~/sigint/adsb/mil_alerts'
expanded_config_dir = os.path.expanduser(CONFIG_DIR)
ICAO_FILES = [
    os.path.join(expanded_config_dir, 'icao_ranges.csv'),
    os.path.join(expanded_config_dir, 'local_interest.csv')
]
CALLSIGNS_FILE = os.path.join(expanded_config_dir, 'military_callsigns.txt')
LOG_FILE = os.path.join(expanded_config_dir, 'alert_log.csv')

# --- Live aircraft lookup ---
# When an alert fires we query hexdb.io for the aircraft's CURRENT registration,
# owner and type. This keeps the alert accurate even when the static ICAO files
# carry a stale label (aircraft get sold/re-registered over time).
AIRCRAFT_DB_URL = "https://hexdb.io/api/v1/aircraft/"
ENABLE_HEX_LOOKUP = True     # set False to disable live lookups entirely
LOOKUP_TIMEOUT = 4           # seconds; short so a slow/down API can't stall the feed for long
LOOKUP_CACHE_TTL = 86400     # re-query a given hex at most once per day

# hex (uppercase) -> (fetched_at_epoch, info_dict_or_None)
_aircraft_info_cache = {}

# --- Dictionaries for TTS ---
PHONETIC_ALPHABET = {
    'A': 'Alpha', 'B': 'Bravo', 'C': 'Charlie', 'D': 'Delta', 'E': 'Echo',
    'F': 'Foxtrot', 'G': 'Golf', 'H': 'Hotel', 'I': 'India', 'J': 'Juliett',
    'K': 'Kilo', 'L': 'Lima', 'M': 'Mike', 'N': 'November', 'O': 'Oscar',
    'P': 'Papa', 'Q': 'Quebec', 'R': 'Romeo', 'S': 'Sierra', 'T': 'Tango',
    'U': 'Uniform', 'V': 'Victor', 'W': 'Whiskey', 'X': 'X-ray', 'Y': 'Yankee',
    'Z': 'Zulu', '0': 'Zero', '1': 'One', '2': 'Two', '3': 'Three', '4': 'Four',
    '5': 'Five', '6': 'Six', '7': 'Seven', '8': 'Eight', '9': 'Nine'
}
CARDINAL_FULL_NAMES = {
    "N": "North", "NNE": "North-North-East", "NE": "North-East", "ENE": "East-North-East",
    "E": "East", "ESE": "East-South-East", "SE": "South-East", "SSE": "South-South-East",
    "S": "South", "SSW": "South-South-West", "SW": "South-West", "WSW": "West-South-West",
    "W": "West", "WNW": "West-North-West", "NW": "North-West", "NNW": "North-North-West"
}

# --- HTML Template ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>ADS-B Alerts</title>
    <meta http-equiv="refresh" content="10">
    <style>
        body { font-family: sans-serif; background-color: #1a1a1a; color: #e6e6e6; }
        h1 { color: #0099ff; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 8px; text-align: left; border-bottom: 1px solid #444; }
        th { background-color: #0099ff; color: #1a1a1a; }
        tr:nth-child(even) { background-color: #2a2a2a; }
        a { color: #57aeff; }
    </style>
</head>
<body>
    <h1>Recent Aircraft Alerts</h1>
    <p>This page automatically refreshes every 10 seconds.</p>
    <table>
        <thead>
            <tr>
                <th>Timestamp</th>
                <th>Callsign</th>
                <th>ICAO</th>
                <th>Registration</th>
                <th>Owner / Type</th>
                <th>Service/Reason</th>
                <th>Altitude</th>
                <th>Speed</th>
                <th>Position</th>
                <th>Map</th>
            </tr>
        </thead>
        <tbody>
            {% for alert in alerts %}
            <tr>
                <td>{{ alert.timestamp }}</td>
                <td><b>{{ alert.callsign }}</b></td>
                <td>{{ alert.icao }}</td>
                <td>{{ alert.registration }}</td>
                <td>{{ alert.owner }}{% if alert.owner and alert.actype %} &middot; {% endif %}{{ alert.actype }}</td>
                <td>{{ alert.service }}</td>
                <td>{{ alert.altitude }} ft</td>
                <td>{{ alert.speed }} kts</td>
                <td>{{ alert.distance }}, {{ alert.bearing }}</td>
                <td><a href="{{ alert.map_link }}" target="_blank">Track</a></td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</body>
</html>
"""

def run_web_server():
    app = Flask(__name__)
    @app.route('/')
    def index():
        return render_template_string(HTML_TEMPLATE, alerts=list(recent_alerts))
    print("Starting web server on http://0.0.0.0:5001")
    app.run(host='0.0.0.0', port=5001)

def speak_alert(data):
    """ Speaks an alert by calling the espeak-ng command-line tool directly. """
    try:
        callsign_to_spell = data['callsign']
        phonetic_parts = [PHONETIC_ALPHABET.get(char, char) for char in callsign_to_spell.upper()]
        phonetic_callsign = ' '.join(phonetic_parts)
        
        short_dir = data['bearing_cardinal']
        full_cardinal_dir = CARDINAL_FULL_NAMES.get(short_dir, short_dir)

        service_reason = data['service']
        owner = data.get('owner', '')
        owner_part = f"registered to {owner}, " if owner else ""

        details_part = (
            f"has been detected flying at an altitude of {data['altitude']} feet, "
            f"traveling at a speed of {data['speed']} knots. "
            f"{data['distance']} and {data['bearing_degrees']} degrees "
            f"{full_cardinal_dir} from your current location."
        )

        full_sentence = f"Aircraft, {phonetic_callsign}, {owner_part}reason {service_reason}, {details_part}"
        command = ['espeak-ng', '-a', '200', '-s', '150', full_sentence]
        
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
    except FileNotFoundError:
        print("Error: 'espeak-ng' command not found. Is it installed and in your PATH?")
    except Exception as e:
        print(f"Error in text-to-speech module: {e}")

def update_location_from_gps():
    if not GPS_ENABLED:
        return
    while True:
        try:
            with serial.Serial(GPS_SERIAL_PORT, 9600, timeout=5) as ser:
                print(f"Successfully connected to GPS on {GPS_SERIAL_PORT}. Waiting for fix...")
                start_time = time.time()
                while time.time() - start_time < 60:
                    line = ser.readline().decode('ascii', errors='ignore')
                    if line.startswith('$GPGGA'):
                        msg = pynmea2.parse(line)
                        if msg.is_valid:
                            with location_lock:
                                global HOME_LAT, HOME_LON, GPS_IS_LIVE
                                HOME_LAT = msg.latitude
                                HOME_LON = msg.longitude
                                GPS_IS_LIVE = True
                            print(f"GPS location updated to: Lat {HOME_LAT:.4f}, Lon {HOME_LON:.4f}")
                            break
                else:
                    print("Could not get a GPS fix within 60 seconds.")
        except serial.SerialException:
            print(f"Warning: GPS device not found at '{GPS_SERIAL_PORT}'. Using fallback location.")
        except Exception as e:
            print(f"An error occurred with the GPS device: {e}")
        time.sleep(GPS_UPDATE_INTERVAL)

def log_alert_to_csv(log_file, icao, callsign, service, lat, lon, registration="", owner=""):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = [icao.upper(), callsign, service, registration, owner, timestamp, lat, lon]
    file_exists = os.path.isfile(log_file)
    try:
        with open(log_file, 'a', newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(['ICAO Address', 'Callsign', 'Service', 'Registration', 'Owner', 'Timestamp', 'Latitude', 'Longitude'])
            writer.writerow(log_entry)
    except IOError as e:
        print(f"Warning: Could not write to log file '{log_file}'. Error: {e}")

def calculate_distance_and_bearing(lat, lon):
    with location_lock:
        current_home_lat = HOME_LAT
        current_home_lon = HOME_LON
    home_coords = (current_home_lat, current_home_lon)
    aircraft_coords = (lat, lon)
    distance_miles = great_circle(home_coords, aircraft_coords).miles
    lat1, lon1 = math.radians(current_home_lat), math.radians(current_home_lon)
    lat2, lon2 = math.radians(lat), math.radians(lon)
    dLon = lon2 - lon1
    x = math.sin(dLon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(dLon))
    initial_bearing = math.degrees(math.atan2(x, y))
    bearing_degrees = (initial_bearing + 360) % 360
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    ix = round(bearing_degrees / (360. / len(dirs)))
    cardinal_dir = dirs[ix % len(dirs)]
    display_string = f"{distance_miles:.1f} mi, {int(bearing_degrees)}° {cardinal_dir}"
    tts_data = {
        "distance": f"{distance_miles:.1f} miles",
        "bearing_degrees": int(bearing_degrees),
        "bearing_cardinal": cardinal_dir
    }
    return display_string, tts_data

def load_icao_ranges(filenames):
    ranges = {}
    total_loaded = 0
    for filename in filenames:
        try:
            with open(filename, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'): continue
                    parts = line.split(',')
                    if len(parts) != 3: continue
                    service, start, end = [p.strip() for p in parts]
                    if service not in ranges: ranges[service] = []
                    ranges[service].append((start, end))
                    total_loaded += 1
            print(f"Loaded data from {filename}")
        except FileNotFoundError:
            print(f"Error: ICAO file not found at '{filename}'")
            return None
    print(f"Successfully loaded a total of {total_loaded} ICAO ranges/addresses.")
    return ranges

def load_callsigns(filename):
    callsigns = []
    try:
        with open(filename, 'r') as f:
            for line in f:
                line = line.strip().upper()
                if line and not line.startswith('#'):
                    callsigns.append(line)
        print(f"Successfully loaded {len(callsigns)} callsigns from {filename}")
        return callsigns
    except FileNotFoundError:
        print(f"Error: Callsigns file not found at '{filename}'")
        return None

def send_ntfy_alert(title, message, actions=None):
    headers = {"Title": title}
    if actions:
        headers["Actions"] = actions
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode('utf-8'), headers=headers)
    except requests.exceptions.RequestException as e:
        print(f"Warning: Could not send ntfy notification. Error: {e}")

def lookup_aircraft_info(icao):
    """Look up an ICAO hex against hexdb.io for the CURRENT registration, owner
    and type. Returns a dict (values may be empty strings) or None when the
    aircraft isn't in the database (404) or the lookup fails. Results are cached
    with a TTL so repeated alerts for the same hex don't re-hit the network, and
    every failure path degrades gracefully to None so the alert still fires."""
    if not ENABLE_HEX_LOOKUP:
        return None
    key = icao.strip().upper()
    if not key:
        return None
    cached = _aircraft_info_cache.get(key)
    if cached and (time.time() - cached[0] < LOOKUP_CACHE_TTL):
        return cached[1]
    info = None
    try:
        resp = requests.get(f"{AIRCRAFT_DB_URL}{key}", timeout=LOOKUP_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("Registration"):
                info = {
                    "registration": (data.get("Registration") or "").strip(),
                    "owner": (data.get("RegisteredOwners") or "").strip(),
                    "type": (data.get("Type") or "").strip(),
                    "manufacturer": (data.get("Manufacturer") or "").strip(),
                    "icao_type": (data.get("ICAOTypeCode") or "").strip(),
                }
        # 404 (unknown/covert/deregistered) leaves info as None.
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"Warning: aircraft lookup failed for {key}. Error: {e}")
    _aircraft_info_cache[key] = (time.time(), info)
    return info

def format_identity(info):
    """Render a lookup result as a one-line identity string, e.g.
    'N212FX / 429 Global Ranger - Fairfax County Police Department'."""
    if not info:
        return ""
    bits = []
    if info.get("registration"):
        bits.append(info["registration"])
    type_str = info.get("type") or info.get("manufacturer")
    if type_str:
        bits.append(type_str)
    line = " / ".join(bits)
    if info.get("owner"):
        line = f"{line} - {info['owner']}" if line else info["owner"]
    return line

def is_military_icao(icao, ranges_db):
    try:
        icao_int = int(icao, 16)
        for country, ranges in ranges_db.items():
            for start, end in ranges:
                if int(start, 16) <= icao_int <= int(end, 16):
                    return True, country
    except (ValueError, TypeError):
        return False, None
    return False, None

def is_military_callsign(callsign, callsigns_db):
    callsign = callsign.strip().upper()
    if not callsign: return False
    for prefix in callsigns_db:
        if callsign.startswith(prefix):
            return True
    return False

def generate_alert(aircraft_data, icao, service, is_test=False):
    """
    Takes aircraft data and triggers all alert types.
    """
    display_pos, tts_pos_data = calculate_distance_and_bearing(aircraft_data['lat'], aircraft_data['lon'])
    map_link = f"https://globe.adsbexchange.com/?lat={aircraft_data['lat']}&lon={aircraft_data['lon']}&zoom=8&icao={icao.upper()}"

    # --- Live lookup: current registration / owner / type for this hex ---
    info = lookup_aircraft_info(icao)
    identity = format_identity(info)
    registration = info["registration"] if info else ""
    owner = info["owner"] if info else ""
    actype = (info.get("type") or info.get("manufacturer")) if info else ""

    # --- 1. Fast Alerts First ---
    alert_details = {
        "timestamp": datetime.now().strftime('%H:%M:%S'),
        "callsign": aircraft_data['callsign'],
        "icao": icao.upper(),
        "registration": registration,
        "owner": owner,
        "actype": actype,
        "service": service,
        "altitude": aircraft_data['altitude'],
        "speed": aircraft_data['speed'],
        "distance": display_pos.split(',')[0],
        "bearing": display_pos.split(',')[1].strip(),
        "map_link": map_link
    }
    recent_alerts.appendleft(alert_details)

    print("---" * 15)
    print(f"✈️  ALERT: Aircraft of Interest Detected! ✈️ ")
    print(f"  ICAO:      {icao.upper()} ({service})")
    print(f"  Callsign:  {aircraft_data['callsign']}")
    if identity:
        print(f"  Identity:  {identity}")
    print(f"  Position:  {display_pos}")
    print(f"  Altitude:  {aircraft_data['altitude']} ft | Speed: {aircraft_data['speed']} kts")
    print(f"  Map Link:  {map_link}")
    print("---" * 15 + "\n")

    ntfy_title = f"Alert: {aircraft_data['callsign']} ({display_pos.split(',')[0].strip()})"
    ntfy_message = (
        f"{aircraft_data['altitude']} ft | {aircraft_data['speed']} kts | {display_pos.split(',')[1].strip()}\n"
        f"ICAO: {icao.upper()} ({service})"
    )
    if identity:
        ntfy_message += f"\n{identity}"
    ntfy_actions = f"view, open, {map_link}"
    send_ntfy_alert(ntfy_title, ntfy_message, actions=ntfy_actions)

    log_alert_to_csv(LOG_FILE, icao, aircraft_data['callsign'], service,
                     aircraft_data['lat'], aircraft_data['lon'],
                     registration=registration, owner=owner)

    # --- 2. Slow Alert Last ---
    tts_alert_data = {**aircraft_data, **tts_pos_data, 'service': service, 'owner': owner}
    speech_thread = Thread(target=speak_alert, args=(tts_alert_data,))
    speech_thread.start()

    if is_test:
        speech_thread.join()
    
    return

def process_sbs_message(message, ranges_db, callsigns_db, aircraft_data):
    parts = message.strip().split(',')
    if len(parts) < 5 or parts[0] != 'MSG': return

    msg_type = parts[1]
    icao = parts[4]

    if icao not in aircraft_data:
        aircraft_data[icao] = {}

    if msg_type == '1' and len(parts) >= 11:
        aircraft_data[icao]['callsign'] = parts[10].strip()
    elif msg_type == '3' and len(parts) >= 16 and parts[14] and parts[15]:
        aircraft_data[icao]['altitude'] = parts[11]
        aircraft_data[icao]['lat'] = float(parts[14])
        aircraft_data[icao]['lon'] = float(parts[15])
    elif msg_type == '4' and len(parts) >= 13:
        aircraft_data[icao]['speed'] = parts[12]

    last_alert_time = aircraft_data[icao].get('last_alert_time')
    if last_alert_time and (time.time() - last_alert_time < ALERT_TIMEOUT):
        return

    is_of_interest, service = is_military_icao(icao, ranges_db)
    current_callsign = aircraft_data[icao].get('callsign', '')

    if not is_of_interest and is_military_callsign(current_callsign, callsigns_db):
        is_of_interest = True
        service = "Callsign Match"

    if is_of_interest:
        required_keys = ['callsign', 'altitude', 'speed', 'lat', 'lon']
        current_data = aircraft_data[icao]

        if all(key in current_data and current_data[key] for key in required_keys):
            aircraft_data[icao]['last_alert_time'] = time.time()
            generate_alert(current_data, icao, service)

def main():
    parser = argparse.ArgumentParser(description="Monitor ADS-B data for military aircraft and send alerts.")
    parser.add_argument('--test', action='store_true', help='Send a single test alert and exit.')
    args = parser.parse_args()

    if args.test:
        print("--- Running in Test Mode ---")
        
        if GPS_ENABLED:
            gps_thread = Thread(target=update_location_from_gps, daemon=True)
            gps_thread.start()
            print("Checking for GPS lock (waiting 5 seconds)...")
            time.sleep(5)
        
        with location_lock:
            source = "Live USB GPS" if GPS_IS_LIVE else "Fallback"
            print(f"Location Source: {source}")
            print(f"Coordinates: Lat {HOME_LAT:.4f}, Lon {HOME_LON:.4f}\n")

        fake_icao = "AE0101"
        fake_aircraft_data = {
            'callsign': 'TEST01',
            'altitude': '10000',
            'speed': '350',
            'lat': HOME_LAT + 0.1,
            'lon': HOME_LON + 0.1
        }
        
        generate_alert(fake_aircraft_data, fake_icao, "Test Trigger", is_test=True)
            
        print("--- Test alert sent. Exiting. ---")
        sys.exit(0)

    print("--- Aircraft Alert Monitor ---")
    
    web_thread = Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    gps_thread = Thread(target=update_location_from_gps, daemon=True)
    gps_thread.start()

    ICAO_RANGES = load_icao_ranges(ICAO_FILES)
    MIL_CALLSIGNS = load_callsigns(CALLSIGNS_FILE)
    if not ICAO_RANGES or not MIL_CALLSIGNS:
        sys.exit(1)

    aircraft_data = {}
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((DUMP1090_HOST, DUMP1090_PORT))
                print(f"Successfully connected to {DUMP1090_HOST}:{DUMP1090_PORT}. Waiting for data...")
                buffer = ""
                while True:
                    data = s.recv(1024).decode('utf-8', 'ignore')
                    if not data: break
                    buffer += data
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        process_sbs_message(line, ICAO_RANGES, MIL_CALLSIGNS, aircraft_data)
        except ConnectionRefusedError:
            print(f"Connection refused. Is dump1090 running?")
        except socket.error as e:
            print(f"Socket error: {e}")
        except KeyboardInterrupt:
            print("\nExiting.")
            break
        print("Connection lost. Reconnecting in 10 seconds...")
        time.sleep(10)

if __name__ == "__main__":
    main()

