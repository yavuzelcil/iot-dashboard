# Import required libraries, drivers, and manager classes
import os, io, time, machine, socket, sys
from machine import SPI, Pin
from managers.DisplayManager import DisplayManager
from managers.FileManager import FileManager
from managers.StationManager import StationManager
from managers.TimeManager import TimeManager
from managers.WlanManager import WlanManager
from managers.WeatherManager import WeatherManager
from drivers.xglcd_font import XglcdFont
from drivers.XPT2046 import Touch
from updater import UpdateManager

# Configuration constants
WLAN_TIMEOUT = 30           # Timeout in seconds for WLAN connection attempts
REQUEST_TIMEOUT = 5         # Timeout in seconds for network requests
UPDATE_HOUR = 3             # Hour of the day (24-hour format) when automatic updates are checked
LOOP_DELAY = 0.2            # Delay in seconds for the main loop iteration

# Time tuple indices for readability
T_DAY = 2
T_HOUR = 3
T_MINUTE = 4
T_SECOND = 5

# Set a global timeout for socket operations to prevent indefinite blocking
socket.socket().settimeout(REQUEST_TIMEOUT)

# Initialize manager instances
fmgr = FileManager()
dspm = DisplayManager(XglcdFont("fonts/ILIFont10x19.c", 10, 19), XglcdFont("fonts/PriceFont15x33.c", 15, 33))
upmr = UpdateManager()

def exit_if_process_fails(error_code, error_text, display_manager, file_manager, wlan_manager=None):
    """
    Handles critical errors by displaying an error screen and restarting the device.
    If the error code starts with '1', it waits for a touch input before restarting.
    """
    if error_code is not "OK":
        # Get QR code image for the specific error and display it
        qr_code = file_manager.get_image_file("error", error_code)
        display_manager.draw_error(error_code, error_text, qr_code)
        
        # Close file and WLAN managers to clean up resources
        file_manager.close()
        if wlan_manager != None:
            wlan_manager.close()

        # If error code indicates a user-recoverable error (e.g., config issue), wait for touch
        if error_code[0] == "1":
            # Initialize touch screen for user interaction
            touch_spi = SPI(1, baudrate=2000000, polarity=0, phase=0, sck=Pin(7), mosi=Pin(5), miso=Pin(4))
            touch_manager = Touch(touch_spi, Pin(6), Pin(3), 2)
            while not touch_manager.is_touched():
                pass # Wait indefinitely until screen is touched
        
        # Reset the device after handling the error
        machine.reset()

def update_firmware(display_manager, update_manager, file_manager, wlan_manager):
    """
    Manages the firmware update process, including checking for updates, downloading, verifying, and installing.
    """
    current_version, update_version = update_manager.update_available()
    if current_version != update_version: # Check if a new version is available
        # Display update screen and progress
        display_manager.draw_update_screen(file_manager.get_image_file("symbol", "update"), current_version, update_version)
        
        display_manager.draw_update_action("Downloading update...")
        exit_if_process_fails(*update_manager.download_update(), display_manager, file_manager, wlan_manager)
        
        display_manager.draw_update_action("Verifying update...")
        exit_if_process_fails(*update_manager.verify_update(), display_manager, file_manager, wlan_manager)
        
        display_manager.draw_update_action("Installing update...")
        # Rename current main.py and updater.py to perform update
        os.rename("main.py", "main_OLD.py")
        os.rename("updater.py", "main.py") # New updater.py becomes main.py to handle the actual update
        machine.reset() # Reboot to run the the updater script

def main():
    """
    Main function to initialize the system, connect to WLAN, synchronize time, fetch data, and run the display loop.
    """
    # Initial display: "Please wait..."
    dspm.draw_waiting_screen()

    # SD card and configuration validation
    exit_if_process_fails(*fmgr.open_sd_card(), dspm, fmgr)
    exit_if_process_fails(*fmgr.validate_sd_card_contents(), dspm, fmgr)

    # WLAN connection
    wlnm = WlanManager()
    wlnm.connect(fmgr.get_configuration_value("wlan_ssid"), fmgr.get_configuration_value("wlan_psk"))
    dspm.draw_waiting_for_wlan(fmgr.get_image_file("symbol", "wlan"), fmgr.get_configuration_value("wlan_ssid"))
    for i in range(WLAN_TIMEOUT + 1):
        dspm.draw_wlan_waiting_time(WLAN_TIMEOUT - i)
        if wlnm.is_connected_boolean():
            break
        time.sleep(1)
    
    # Check for successful WLAN connection and internet access
    exit_if_process_fails(*wlnm.is_connected(), dspm, fmgr, wlnm)
    exit_if_process_fails(*wlnm.device_online(), dspm, fmgr, wlnm)

    # Time synchronization and timezone setup
    tmgr = TimeManager()
    exit_if_process_fails(*tmgr.sync_time(), dspm, fmgr, wlnm)
    tmgr.set_timezone()

    # Initialize data managers
    wmgr = WeatherManager(fmgr.get_configuration_value("weather_lat"), fmgr.get_configuration_value("weather_long"))
    stmr = StationManager(fmgr.get_configuration_value("station_ids"),
                          fmgr.get_configuration_value("fuel_type"),
                          fmgr.get_configuration_value("tankerkoenig_api_key"))

    # Draw the main layout of the display
    dspm.draw_main_layout(
        [fmgr.get_image_file("station", label[0]) for label in fmgr.get_configuration_value("station_labels")],
        [fmgr.get_image_file("symbol", "thermometer"), 
        fmgr.get_image_file("symbol", "raindrop"),
        fmgr.get_image_file("symbol", "lowest-temperature"),
        fmgr.get_image_file("symbol", "highest-temperature")],
        fmgr.get_configuration_value("station_labels"),
        fmgr.get_configuration_value("fuel_type")
    )
    
    # Initial data fetch and display
    dspm.draw_weekday_date_time(tmgr.get_timedate())
    weather_data, weather_icon_name = wmgr.get_weather_data(tmgr.get_timestamp(), tmgr.get_tz_identifier())
    dspm.draw_weather_data(weather_data, weather_icon_name, fmgr.get_image_file("weather", weather_icon_name))
    dspm.draw_station_data(*stmr.get_station_data())

    # Variables for main loop control
    previous_day = -1
    previous_hour = -1
    previous_minute = -1
    data_can_be_updated = False
    perform_update_check = False

    # Main loop, runs (technically) forever until the next firmware update
    while True:
        t = tmgr.get_timestamp()

        # Daily tasks, re-enable update check for the new day
        if previous_day != t[T_DAY]:
            previous_day = t[T_DAY]
            perform_update_check = True

        # Hourly tasks, sync NTP clock, set timezone (relevant for summer/winter time switching)
        if previous_hour != t[T_HOUR]:
            previous_hour = t[T_HOUR]
            exit_if_process_fails(*wlnm.is_connected(), dspm, fmgr, wlnm)
            exit_if_process_fails(*wlnm.device_online(), dspm, fmgr, wlnm)
            exit_if_process_fails(*tmgr.sync_time(), dspm, fmgr, wlnm)
            tmgr.set_timezone()

        # Minute-by-minute tasks, update time and date on display
        if previous_minute != t[T_MINUTE]:
            previous_minute = t[T_MINUTE]
            dspm.draw_weekday_date_time(tmgr.get_timedate())

        # Control flag to allow data updates once every 5 minutes
        if (t[T_MINUTE] - 1) % 5 != 0 and not data_can_be_updated:
            data_can_be_updated = True

        # Data update logic, runs every 5 minutes at XX:01, XX:06, XX:11, etc.
        # This is because of the station opening times, which get precise updates at these times.
        # Example: A station closes at 23:00. When fetching data from tankerkeonig API at 23:00,
        #          the station appears to be open. When fetching at 23:01, it will appear as closed.
        if (data_can_be_updated and t[T_SECOND] >= 1 and (t[T_MINUTE] - 1) % 5 == 0):
            data_can_be_updated = False
            exit_if_process_fails(*wlnm.is_connected(), dspm, fmgr, wlnm)
            exit_if_process_fails(*wlnm.device_online(), dspm, fmgr, wlnm)
            if not tmgr.get_timezone_set():
                tmgr.set_timezone()

            # Check for firmware updates if enabled and at the specified hour and perform a timezone update.
            # The timezone update ensures
            if (fmgr.get_configuration_value("automatic_updates") and perform_update_check and t[T_HOUR] == UPDATE_HOUR):
                update_firmware(dspm, upmr, fmgr, wlnm)
                perform_update_check = False
            
            # Fetch and display weather data
            weather_data, weather_icon_name = wmgr.get_weather_data(t, tmgr.get_tz_identifier())
            if(dspm.currently_displayed.get("weather_icon_name") != weather_icon_name):
                dspm.draw_weather_data(weather_data, weather_icon_name, fmgr.get_image_file("weather", weather_icon_name))
            else:
                dspm.draw_weather_data(weather_data, weather_icon_name)
            
            # Fetch and display station data
            dspm.draw_station_data(*stmr.get_station_data())

        # Take a short nap    
        time.sleep(LOOP_DELAY)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Generic error handler for unexpected exceptions
        # For better debugging, extract human-readable error message,
        # format it for 1-2 lines (max. 84 characters error message)
        # and print corresponding most recent call file name and line number
        string_io = io.StringIO()
        sys.print_exception(e, string_io)
        traceback_info = string_io.getvalue().split('\n')[-3].replace('"', ',').replace(',', ',').split(',')
        err_file_name = traceback_info[1].strip().split("/")[-1]
        err_line_number = traceback_info[3].strip().replace('line ', '')
        err_text = str(e)[0].upper() + str(e)[1:84]
        err_lines = [err_text[i:i + 42] for i in range(0, len(err_text), 42)]
        dspm.draw_waiting_screen()
        exit_if_process_fails(*fmgr.open_sd_card(), dspm, fmgr)
        exit_if_process_fails("1000", ["An unexpected error occured:"] +
                                       err_lines +
                                       [f"In {err_file_name}, line {err_line_number}",
                                       "Please attempt to reproduce this error",
                                       "and report it on the GitHub page!"], 
                                       dspm, fmgr)