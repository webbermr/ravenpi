import socket
import time
import sys
import requests
import os
import math
import csv
from datetime import datetime
from geopy.distance import great_circle

# --- NEW: Web interface imports ---
from flask import Flask, render_template_string
from threading import Thread
from collections import deque


# --- Configuration ---
DUMP1090_HOST = '127.0.0.1'
DUMP1090_PORT = 30003
NTFY_TOPIC = 'ADSB-ALERTS'
ALERT_TIMEOUT = 600

# --- Set your home location ---
HOME_LAT = 38.95
HOME_LON = -77.38

# --- Path configuration ---
CONFIG_DIR = '~/sigint/adsb/mil_alerts'
expanded_config_dir = os.path.expanduser(CONFIG_DIR)
ICAO_FILES = [
    os.path.join(expanded_config_dir, 'icao_ranges.csv'),
    os.path.join(expanded_config_dir, 'local_interest.csv')
]
CALLSIGNS_FILE = os.path.join(expanded_config_dir, 'military_callsigns.txt')
LOG_FILE = os.path.join(expanded_config_dir, 'alert_log.csv')


# --- NEW: Global variable to store recent alerts for the web UI ---
# A deque is a special list that has a maximum size.
recent_alerts = deque(maxlen=25)


# --- NEW: HTML Template for the web page ---
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

# --- NEW: Function to run the Flask web server ---
def run_web_server():
    app = Flask(__name__)

    @app.route('/')
    def index():
        # Pass the list of recent alerts to the HTML template
        return render_template_string(HTML_TEMPLATE, alerts=list(recent_alerts))

    print("Starting web server on http://0.0.0.0:5001")
    # Host on 0.0.0.0 to make it accessible from other devices on your network
    app.run(host='0.0.0.0', port=5001)


# --- Helper Functions (no changes to these) ---

def log_alert_to_csv(log_file, icao, callsign, service, lat, lon):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = [icao.upper(), callsign, service, timestamp, lat, lon]
    file_exists = os.path.isfile(log_file)
    try:
        with open(log_file, 'a', newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(['ICAO Address', 'Callsign', 'Service', 'Timestamp', 'Latitude', 'Longitude'])
            writer.writerow(log_entry)
    except IOError as e:
        print(f"Warning: Could not write to log file '{log_file}'. Error: {e}")

def calculate_distance_and_bearing(lat, lon):
    home_coords = (HOME_LAT, HOME_LON)
    aircraft_coords = (lat, lon)
    distance_miles = great_circle(home_coords, aircraft_coords).miles
    lat1, lon1 = math.radians(HOME_LAT), math.radians(HOME_LON)
    lat2, lon2 = math.radians(lat), math.radians(lon)
    dLon = lon2 - lon1
    x = math.sin(dLon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(dLon))
    initial_bearing = math.degrees(math.atan2(x, y))
    bearing = (initial_bearing + 360) % 360
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    ix = round(bearing / (360. / len(dirs)))
    cardinal_dir = dirs[ix % len(dirs)]
    return f"{distance_miles:.1f} mi", f"{int(bearing)}° {cardinal_dir}"

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
    reason = f"ICAO Match ({service})" if is_of_interest else ""
    current_callsign = aircraft_data[icao].get('callsign', '')

    if not reason and is_military_callsign(current_callsign, callsigns_db):
        is_of_interest = True
        service = "Callsign Match"
        reason = "Callsign Match"

    if is_of_interest:
        required_keys = ['callsign', 'altitude', 'speed', 'lat', 'lon']
        current_data = aircraft_data[icao]

        if all(key in current_data and current_data[key] for key in required_keys):
            aircraft_data[icao]['last_alert_time'] = time.time()
            distance, bearing = calculate_distance_and_bearing(current_data['lat'], current_data['lon'])
            map_link = f"https://globe.adsbexchange.com/?lat={current_data['lat']}&lon={current_data['lon']}&zoom=8&icao={icao.upper()}"

            # --- MODIFIED: Add alert to our global list for the web UI ---
            alert_details = {
                "timestamp": datetime.now().strftime('%H:%M:%S'),
                "callsign": current_data['callsign'],
                "icao": icao.upper(),
                "service": service,
                "altitude": current_data['altitude'],
                "speed": current_data['speed'],
                "distance": distance,
                "bearing": bearing,
                "map_link": map_link
            }
            # appendleft adds the new alert to the beginning of the list
            recent_alerts.appendleft(alert_details)

            # --- Console Printing, Notifications, and Logging (No Changes Here) ---
            print("---" * 15)
            print(f"✈️  ALERT: Aircraft of Interest Detected! ✈️ ")
            print(f"  ICAO:      {icao.upper()} ({service})")
            print(f"  Callsign:  {current_data['callsign']}")
            print(f"  Position:  {distance} away, bearing {bearing}")
            print(f"  Altitude:  {current_data['altitude']} ft | Speed: {current_data['speed']} kts")
            print(f"  Map Link:  {map_link}")
            print("---" * 15 + "\n")

            ntfy_title = f"Alert: {current_data['callsign']} ({distance})"
            ntfy_message = (
                f"{current_data['altitude']} ft | {current_data['speed']} kts | {bearing}\n"
                f"ICAO: {icao.upper()} ({service})"
            )
            ntfy_actions = f"view, open, {map_link}"
            send_ntfy_alert(ntfy_title, ntfy_message, actions=ntfy_actions)
            log_alert_to_csv(LOG_FILE, icao, current_data['callsign'], service, current_data['lat'], current_data['lon'])

def main():
    print("--- Aircraft Alert Monitor ---")
    
    # --- NEW: Start the web server in a background thread ---
    # The 'daemon=True' flag means the web server thread will exit
    # automatically when the main script stops.
    web_thread = Thread(target=run_web_server, daemon=True)
    web_thread.start()

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

