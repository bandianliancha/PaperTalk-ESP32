import serial, time
try:
    s = serial.Serial('/dev/cu.usbmodem101', 115200, timeout=0.1)
    print("Opened serial. Listening for 5 seconds...")
    t0 = time.time()
    while time.time() - t0 < 5:
        data = s.read(1024)
        if data:
            print(data.decode('utf-8', errors='ignore'), end='')
    s.close()
except Exception as e:
    print("Error:", e)
