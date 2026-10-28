import serial, time
s = serial.Serial('/dev/cu.usbmodem101', 115200, timeout=0.1)
s.dtr = True
s.rts = False
time.sleep(0.5)
t0 = time.time()
while time.time() - t0 < 6:
    d = s.read(1024)
    if d:
        print(d.decode('utf-8', errors='ignore'), end='')
s.close()
